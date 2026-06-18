"""
normalizer.py — Three-Tier Normalization Engine
================================================
Tier 1 : Canonical Registry exact/near-exact lookup  (threshold ≥ 85 %)
Tier 2 : NLP — Jaccard bigram similarity             (threshold ≥ 85 %)
Tier 3 : Gemini semantic fallback                     (when Tiers 1 & 2 fail)
"""

from __future__ import annotations

import os
import re
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Optional deps (graceful degradation) ──────────────────────────────────────
try:
    from rapidfuzz import process as rf_process, fuzz as rf_fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False
    log.warning("rapidfuzz not installed — Tier 2 falls back to pure Jaccard only.")

try:
    from google import genai as _genai_sdk
    _HAS_GENAI = True
except ImportError:
    try:
        import google.generativeai as _genai_sdk  # legacy fallback
        _HAS_GENAI = True
        log.warning("Using deprecated google-generativeai — upgrade to google-genai.")
    except ImportError:
        _HAS_GENAI = False
        _genai_sdk = None
        log.warning("No Gemini SDK installed — Tier 3 (Gemini) disabled.")

from mappings import TEST_MAPPING, UNIT_MAPPING, IGNORE_TESTS

# ── Thresholds ─────────────────────────────────────────────────────────────────
TIER1_EXACT_CONFIDENCE   = 1.00
TIER2_THRESHOLD          = 0.85   # Jaccard / fuzzy minimum to accept
TIER3_CONFIDENCE         = 0.85   # Assigned when Gemini resolves

# ── Gemini singleton ───────────────────────────────────────────────────────────
_gemini_model = None

def init_gemini(api_key: str) -> None:
    """Call once from main.py after the user supplies the key."""
    global _gemini_model
    if not _HAS_GENAI:
        log.error("No Gemini SDK installed — Tier 3 unavailable. Run: pip install google-genai")
        return
    try:
        # New SDK: google-genai
        if hasattr(_genai_sdk, 'Client'):
            _gemini_model = _genai_sdk.Client(api_key=api_key)
            log.info("✓ Gemini Tier-3 initialised via google-genai SDK")
        else:
            # Legacy fallback: google-generativeai
            _genai_sdk.configure(api_key=api_key)
            _gemini_model = _genai_sdk.GenerativeModel("gemini-1.5-flash-latest")
            log.info("✓ Gemini Tier-3 initialised via legacy SDK (gemini-1.5-flash-latest)")
    except Exception as exc:
        log.error("Gemini init failed: %s", exc)
        _gemini_model = None


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY — Pre-processing: strip methodology annotations
# ══════════════════════════════════════════════════════════════════════════════

# Patterns that appear after the core test name and add no semantic value:
# "(whole blood/photometric method)", "(light microscopy)/hpf", etc.
_METHOD_STRIP_RE = re.compile(
    r"""\s*
    (?:
        \(\s*(?:whole\s*blood|serum|plasma|urine|automated|manual|calculated|spectropho\S*
            |impedence|photometric|flow\s*cytometry|bencidine|rotheras|fouchets
            |ehrlichs|multistrips|colour\s*reaction|visual|impedenco|light\s*microscopy
            |p-nitro|pka|benedict|colour|double\s*indicator|ph\s*indicator
            |sulphosalicylic|cyanmethemoglobin|bcg|bromo\S*|uv\s*with
            |electrical|calculated)[^)]*\)
        |/\s*(?:hpf|lpf|cmm|cumm|cu\.mm)
        |,\s*serum\s*$
        |,\s*plasma\s*$
        |,\s*urine\s*$
        |,\s*whole\s*blood\s*$
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _clean_name(raw: str) -> str:
    """
    Strip common methodology/specimen suffixes so core name can be looked up.
    E.g. "Haemoglobin (whole blood/photometric method)" → "HAEMOGLOBIN"
         "PUS CELLS(light microscopy)/hpf"              → "PUS CELLS"
         "LIPASE,SERUM"                                  → "LIPASE"
    """
    if not raw:
        return ""
    
    cleaned = raw.strip()
    upper = cleaned.upper()
    
    # Direct normalization of malformed automation strings
    if upper.startswith("PROTEIN (ALBUMIN)"):
        return "PROTEIN (ALBUMIN)"
    if upper.startswith("GLUCOSE(AUTOMATED"):
        return "GLUCOSE"
        
    # Standard methodology strip
    cleaned = _METHOD_STRIP_RE.sub("", cleaned).strip()
    
    # Strip trailing specimen details like ", WHOLE BLOOD EDTA"
    cleaned = re.sub(r",\s*WHOLE\s*BLOOD\s*EDTA\s*$", "", cleaned, flags=re.I).strip()
    
    # Remove spaces around operators/symbols (e.g. "RDW - CV" -> "RDW-CV", "Na +" -> "Na+")
    cleaned = re.sub(r"\s*([+\-/])\s*", r"\1", cleaned)
    
    # Remove trailing punctuation leftovers
    cleaned = re.sub(r"[,;/\s]+$", "", cleaned).strip()
    return cleaned.upper() if cleaned else raw.upper()


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY — Jaccard bigram similarity
# ══════════════════════════════════════════════════════════════════════════════

def _bigrams(text: str) -> set[str]:
    t = text.lower().strip()
    return {t[i:i+2] for i in range(len(t) - 1)} if len(t) > 1 else set()


def jaccard(a: str, b: str) -> float:
    """Character-level bigram Jaccard similarity ∈ [0, 1]."""
    if not a or not b:
        return 0.0
    a_l, b_l = a.lower().strip(), b.lower().strip()
    if a_l == b_l:
        return 1.0
    s1, s2 = _bigrams(a_l), _bigrams(b_l)
    if not s1 or not s2:
        return 1.0 if a_l == b_l else 0.0
    inter = len(s1 & s2)
    union = len(s1 | s2)
    score = inter / union if union else 0.0
    # Prefix boost — e.g. "Haemoglobin" vs "Haemoglobin (Hb)"
    if (a_l.startswith(b_l) or b_l.startswith(a_l)) and min(len(a_l), len(b_l)) > 4:
        score = max(score, 0.80)
    return score


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 1 — Canonical Registry Lookup
# ══════════════════════════════════════════════════════════════════════════════

def _tier1_lookup(cleaned: str, mapping: dict[str, str]) -> tuple[Optional[str], float]:
    """
    Returns (canonical, confidence).
    1a. Exact key match
    1b. Exact value match (already canonical)
    1c. Normalised key (strip punctuation noise)
    1d. Methodology-stripped key (e.g. 'Haemoglobin (whole blood/photometric)' → 'HAEMOGLOBIN')
    """
    # 1a — Exact key
    if cleaned in mapping:
        return mapping[cleaned], TIER1_EXACT_CONFIDENCE

    # 1b — Already canonical (value match)
    for v in mapping.values():
        if cleaned == v.upper():
            return v, TIER1_EXACT_CONFIDENCE

    # 1c — Normalised key: remove trailing/embedded punctuation noise
    norm = re.sub(r"[()*/\\]", " ", cleaned)
    norm = re.sub(r"\s{2,}", " ", norm).strip()
    if norm != cleaned and norm in mapping:
        return mapping[norm], 0.98

    # 1d — Strip methodology annotations and retry
    stripped = _clean_name(cleaned)
    if stripped and stripped != cleaned:
        if stripped in mapping:
            return mapping[stripped], 0.97
        # Also try with punctuation normalised
        norm2 = re.sub(r"[()*/\\]", " ", stripped)
        norm2 = re.sub(r"\s{2,}", " ", norm2).strip()
        if norm2 in mapping:
            return mapping[norm2], 0.96

    return None, 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 2 — NLP Fuzzy Match (Jaccard + RapidFuzz token-sort)
# ══════════════════════════════════════════════════════════════════════════════

def _tier2_nlp(cleaned: str, mapping: dict[str, str]) -> tuple[Optional[str], float]:
    """
    Jaccard bigram similarity over all keys.
    Falls back to RapidFuzz token_sort_ratio if available.
    Accepts match only when score ≥ TIER2_THRESHOLD.
    """
    keys = list(mapping.keys())
    best_key: Optional[str] = None
    best_score: float = 0.0

    # Jaccard sweep
    for k in keys:
        score = jaccard(cleaned, k)
        if score > best_score:
            best_score = score
            best_key = k
            if score > 0.97:          # Early exit — effectively certain
                break

    if best_score >= TIER2_THRESHOLD and best_key:
        return mapping[best_key], round(best_score, 4)

    # RapidFuzz token-sort as secondary scorer
    if _HAS_RAPIDFUZZ:
        result = rf_process.extractOne(cleaned, keys, scorer=rf_fuzz.token_sort_ratio)
        if result:
            matched_key, fuzz_score, _ = result
            fuzz_norm = fuzz_score / 100.0
            if fuzz_norm >= TIER2_THRESHOLD and fuzz_norm > best_score:
                return mapping[matched_key], round(fuzz_norm, 4)

    return None, 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  TIER 3 — Gemini Semantic Fallback
# ══════════════════════════════════════════════════════════════════════════════

_GEMINI_CACHE: dict[str, tuple[Optional[str], float]] = {}

def _tier3_gemini(raw: str, canonical_targets: list[str]) -> tuple[Optional[str], float]:
    """
    Ask Gemini to map a raw name to the closest canonical target.
    Results are cached in-process to avoid duplicate API calls.
    """
    if not _gemini_model:
        return None, 0.0

    cache_key = raw.upper()
    if cache_key in _GEMINI_CACHE:
        log.debug("Gemini cache hit for '%s'", raw)
        return _GEMINI_CACHE[cache_key]

    prompt = (
        "You are a professional medical data standardization engine.\n"
        "Map the raw laboratory test name below to the single best-matching "
        "canonical name from the provided list.\n\n"
        f'Raw Test Name: "{raw}"\n\n'
        "Canonical List:\n"
        + "\n".join(f"  - {t}" for t in canonical_targets)
        + "\n\nRules:\n"
        "1. Reply with ONLY the exact canonical name from the list.\n"
        "2. If no name matches medically, reply exactly: UNKNOWN\n"
        "3. Do not add explanation or punctuation.\n"
    )

    try:
        # Support both new (google-genai) and legacy (google-generativeai) SDKs
        if hasattr(_gemini_model, 'models'):  # new google-genai Client
            resp = _gemini_model.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            text = resp.text.strip()
        else:  # legacy GenerativeModel
            resp = _gemini_model.generate_content(prompt)
            text = resp.text.strip()

        for target in canonical_targets:
            if target.upper() in text.upper():
                result = (target, TIER3_CONFIDENCE)
                _GEMINI_CACHE[cache_key] = result
                return result
        _GEMINI_CACHE[cache_key] = (None, 0.0)
        return None, 0.0
    except Exception as exc:
        log.warning("Gemini API error for '%s': %s", raw, exc)
        return None, 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def normalize_test_name(
    raw: str,
    test_mapping: dict[str, str] | None = None,
) -> tuple[str, str, float]:
    """
    Returns (canonical_name, normalization_method, confidence).
    normalization_method ∈ {"Tier1-Exact", "Tier2-NLP", "Tier3-Gemini", "Unresolved"}
    """
    if not raw or not raw.strip():
        return "", "Unresolved", 0.0

    mapping = test_mapping or TEST_MAPPING
    # Use the stripped version as the primary lookup key
    cleaned = _clean_name(raw)

    # ── Tier 1 ──
    canon, conf = _tier1_lookup(cleaned, mapping)
    if canon:
        return canon, "Tier1-Exact", conf

    # ── Tier 2 ──
    canon, conf = _tier2_nlp(cleaned, mapping)
    if canon:
        return canon, "Tier2-NLP", conf

    # ── Tier 3 ──
    targets = sorted(set(mapping.values()))
    canon, conf = _tier3_gemini(cleaned, targets)
    if canon:
        return canon, "Tier3-Gemini", conf

    # Unresolved — return raw name
    return raw.strip(), "Unresolved", 0.0


def normalize_unit(
    raw: str,
    unit_mapping: dict[str, str] | None = None,
) -> str:
    """
    Returns canonical unit string.
    Falls back to Jaccard similarity against known units.
    """
    if not raw or not raw.strip():
        return ""

    mapping = unit_mapping or UNIT_MAPPING
    cleaned = raw.strip()
    upper  = cleaned.upper()

    # Direct lookup
    if upper in mapping:
        return mapping[upper]

    # Regex patterns for common variants
    _UNIT_PATTERNS = [
        (r"g\s*/\s*dl",                     "g/dL"),
        (r"g\s*%",                           "g/dL"),
        (r"mg\s*/\s*dl",                     "mg/dL"),
        (r"u\s*/\s*l|iu\s*/\s*l",           "U/L"),
        (r"mm\s*hg",                         "mmHg"),
        (r"mill?ion\s*/\s*(cmm|cu\.?mm?|mcl)", "million/mcL"),
        (r"cells?\s*/\s*(cmm|cu\.?mm?|mcl)", "cells/mcL"),
        (r"(thou|k)\s*/\s*(cmm|mcl|ul)",    "10³/mcL"),
        (r"/\s*(cmm|cu\.?mm?|mcl)",          "/mcL"),
        (r"meq\s*/\s*l",                     "mEq/L"),
        (r"mmol\s*/\s*l",                    "mmol/L"),
        (r"\bfl\b",                          "fL"),
        (r"\bpg\b",                          "pg"),
        (r"degree\s*[fc]|°[fc]",             lambda m: "°F" if "f" in m.group().lower() else "°C"),
    ]
    lower = cleaned.lower()
    for pat, canon in _UNIT_PATTERNS:
        if callable(canon):
            m = re.search(pat, lower, re.I)
            if m:
                return canon(m)
        elif re.search(pat, lower, re.I):
            return canon

    # Jaccard fuzzy over known unit keys
    best, best_score = "", 0.0
    for k, v in mapping.items():
        score = jaccard(upper, k)
        if score > best_score:
            best_score = score
            best = v
    if best_score >= 0.80:
        return best

    return cleaned  # Return as-is if unknown


def is_ignorable(test_name: str) -> bool:
    """True if the test name is a panel header or non-quantifiable entry."""
    return test_name.strip().upper() in IGNORE_TESTS
