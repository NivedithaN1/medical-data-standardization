"""
parser.py — JSON → Flat Clinical Record Extractor
==================================================
Reads every .json file from the input/ folder and produces a list of
flat row-dicts conforming to the output schema.

Output schema columns (matching the BigQuery / Excel target):
    document_id, source_file,
    patient_name, uhid, age, gender, hospital_name,
    bill_date, reports_date, admission_date, discharge_date,
    test_name_original, test_name_canonical,
    result_value (FLOAT), result_text (STRING),
    unit_original, unit_canonical,
    range_low (FLOAT), range_high (FLOAT), range_text,
    test_analytics,
    normalization_method, normalization_confidence
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from normalizer import normalize_test_name, normalize_unit, is_ignorable

log = logging.getLogger(__name__)

# ─── Regex helpers ─────────────────────────────────────────────────────────────

_AGE_RE = re.compile(
    r"(\d{1,3})\s*(?:year|yr|y)\b"      # "27 years" / "27Y"
    r"|(\d{1,3})Y\s*\d+M"               # "27Y6M13D"
    r"|^(\d{1,3})$",                    # plain integer
    re.I,
)
_DECOMMA = re.compile(r"(\d),(\d{2,3})")
_NUMBER  = re.compile(r"[\d,]+\.?\d*")
_RANGE_RE = re.compile(r"([\d.]+)\s*[-–]\s*([\d.]+)")
_LT_GT   = re.compile(r"([<>])\s*([\d.]+)")

# Qualitative tokens mapped to 0 / None
_QUALITATIVE = {"nil", "negative", "absent", "not detected", "none",
                "normal", "positive", "trace", "n/a"}

# Panel-embedded tests: "LFT: SGOT - 38, SGPT -14, ALP - 127"
_PANEL_KV  = re.compile(r"([A-Z][A-Z\+\.\s]{1,30})\s*[-:]\s*([\d.]+)", re.I)
_PANEL_PAR = re.compile(r"([A-Z\+\.\s]{2,30})\s*\(([\d.]+)\)", re.I)


# ─── Pure helpers ──────────────────────────────────────────────────────────────

def _parse_age(raw) -> Optional[str]:
    if not raw:
        return None
    text = str(raw).strip()
    m = _AGE_RE.search(text)
    if m:
        age_val = m.group(1) or m.group(2) or m.group(3)
        return age_val
    m = re.search(r"\d+", text)
    return m.group() if m else text


def _parse_date(raw) -> str:
    if not raw:
        return ""
    for fmt in ("%d/%b/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
                "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return str(raw).strip()


def _extract_numeric(raw) -> tuple[Optional[float], str]:
    """Returns (float_value | None, raw_text_as_str)."""
    if raw is None:
        return None, ""
    text = str(raw).strip()
    lower = text.lower()
    if lower in _QUALITATIVE:
        return None, text
    # De-Indian-comma: 1,20,000 → 120000
    de = _DECOMMA.sub(r"\1\2", text)
    m = _NUMBER.search(de)
    if m:
        try:
            return float(m.group().replace(",", "")), text
        except ValueError:
            pass
    return None, text


def _parse_range(raw) -> tuple[Optional[float], Optional[float], str]:
    """Returns (low, high, range_text)."""
    if not raw:
        return None, None, ""
    text = str(raw).strip()
    m = _RANGE_RE.search(text)
    if m:
        try:
            return float(m.group(1)), float(m.group(2)), text
        except ValueError:
            pass
    m = _LT_GT.match(text.strip())
    if m:
        val = float(m.group(2))
        if m.group(1) == "<":
            return None, val, text
        else:
            return val, None, text
    return None, None, text


def _panel_split(name: str, result_str: str) -> list[tuple[str, float]]:
    """
    Try to extract sub-tests from embedded strings like:
      test_name : "LFT ( SGOT - 38, SGPT -14, ALP - 127)"
      result    : "Neutrophil - 72.4, Lymphocyte - 23.5"
    Returns [(sub_name, value), ...]
    """
    found: dict[str, float] = {}
    for src in (name, result_str or ""):
        for m in _PANEL_KV.finditer(src):
            key = m.group(1).strip()
            if len(key) > 1:
                try:
                    found[key] = float(m.group(2))
                except ValueError:
                    pass
        for m in _PANEL_PAR.finditer(src):
            key = m.group(1).strip()
            if len(key) > 1 and key not in found:
                try:
                    found[key] = float(m.group(2))
                except ValueError:
                    pass
    return list(found.items())


# ─── Record builders ───────────────────────────────────────────────────────────

def _make_base(
    document_id: str,
    source_file: str,
    patient_name: str,
    uhid: str,
    age: str,
    gender: str,
    hospital_name: str,
    bill_date: str,
    reports_date: str,
    admission_date: str = "",
    discharge_date: str = "",
) -> dict:
    return {
        "document_id":      document_id,
        "source_file":      source_file,
        "patient_name":     patient_name,
        "uhid":             uhid,
        "age":              age or "",
        "gender":           gender or "",
        "hospital_name":    hospital_name or "",
        "bill_date":        bill_date or "",
        "reports_date":     reports_date or "",
        "admission_date":   admission_date or "",
        "discharge_date":   discharge_date or "",
    }


def _build_test_row(
    base: dict,
    raw_name: str,
    result_raw,
    unit_raw: str,
    range_raw: str,
    analytics: str,
) -> Optional[dict]:
    """Build one output row for a single test entry."""
    if not raw_name or not raw_name.strip():
        return None

    if is_ignorable(raw_name):
        return None

    canon, method, confidence = normalize_test_name(raw_name)
    unit_canon = normalize_unit(unit_raw)
    result_val, result_text = _extract_numeric(result_raw)
    range_lo, range_hi, range_text = _parse_range(range_raw)

    return {
        **base,
        "test_name_original":       raw_name.strip(),
        "test_name_canonical":      canon,
        "result_value":             result_val,
        "result_text":              result_text,
        "unit_original":            (unit_raw or "").strip(),
        "unit_canonical":           unit_canon,
        "range_low":                range_lo,
        "range_high":               range_hi,
        "range_text":               range_text,
        "test_analytics":           (analytics or "").strip(),
        "normalization_method":     method,
        "normalization_confidence": round(confidence, 4),
    }


# ─── File-level processor ──────────────────────────────────────────────────────

def process_json_data(doc: dict, filename: str) -> list[dict]:
    """
    Parse loaded JSON data dict and return a list of flat clinical record dicts.
    """
    rows: list[dict] = []
    document_id = Path(filename).stem
    source_file  = filename

    data = doc.get("data", {})
    response_details = data.get("responseDetails", [])

    # ── Pull discharge summary context first ─────────────────────────────────
    discharge_context: dict = {}
    for entry in response_details:
        classifier = (entry.get("classifier") or "").lower()
        if classifier == "discharge_summary":
            ed = entry.get("data", {})
            discharge_context = {
                "patient_name":   ed.get("patientName", ""),
                "age":            _parse_age(ed.get("age")),
                "gender":         ed.get("gender", ""),
                "hospital_name":  ed.get("hospitalName", ""),
                "admission_date": _parse_date(ed.get("admissionDate")),
                "discharge_date": _parse_date(ed.get("dischargeDate")),
                "uhid":           ed.get("uhid", ""),
                "bill_date":      "",
                "reports_date":   "",
            }
            break   # one discharge summary per file is typical

    # ── Process each responseDetail entry ────────────────────────────────────
    for entry in response_details:
        classifier = (entry.get("classifier") or "").lower()
        entry_data = entry.get("data", {})

        if classifier == "lab_report":
            basic = entry_data.get("basic_info", {})
            base = _make_base(
                document_id  = document_id,
                source_file  = source_file,
                patient_name = basic.get("patient_name") or discharge_context.get("patient_name", ""),
                uhid         = basic.get("uhid") or discharge_context.get("uhid", ""),
                age          = _parse_age(basic.get("age")) or discharge_context.get("age", ""),
                gender       = basic.get("gender") or discharge_context.get("gender", ""),
                hospital_name= basic.get("lab_or_hospital_name") or discharge_context.get("hospital_name", ""),
                bill_date    = _parse_date(basic.get("bill_date")) or discharge_context.get("bill_date", ""),
                reports_date = _parse_date(basic.get("reports_date")) or discharge_context.get("reports_date", ""),
                admission_date = discharge_context.get("admission_date", ""),
                discharge_date = discharge_context.get("discharge_date", ""),
            )

            for test in entry_data.get("report_details", []):
                raw_name   = test.get("test_name", "")
                result_raw = test.get("result")
                unit_raw   = test.get("unit", "")
                range_raw  = test.get("range", "")
                analytics  = test.get("test_analytics", "")

                # ── Panel splitter: embedded multi-test strings ──
                panel = _panel_split(raw_name, str(result_raw or ""))
                # If there are multiple embedded tests, or a single test where the result is empty (so it was embedded)
                has_embedded_result = len(panel) == 1 and not _extract_numeric(result_raw)[0] and not _extract_numeric(result_raw)[1]
                if panel and (len(panel) > 1 or has_embedded_result):
                    for sub_name, sub_val in panel:
                        row = _build_test_row(
                            base, sub_name, sub_val, "", "", ""
                        )
                        if row:
                            row["test_analytics"] = analytics
                            rows.append(row)
                    continue

                # ── Normal single-test entry ──
                row = _build_test_row(base, raw_name, result_raw, unit_raw, range_raw, analytics)
                if row:
                    rows.append(row)

        elif classifier == "discharge_summary":
            # Discharge summary rows — vitals and observations only
            pass

    return rows


def process_json_file(path: Path) -> list[dict]:
    """
    Parse one JSON file and return a list of flat clinical record dicts.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as exc:
        log.error("Cannot read %s: %s", path, exc)
        return []
    
    rows = process_json_data(doc, path.name)
    log.info("  ✓ %s → %d rows", path.name, len(rows))
    return rows


# ─── Batch processor ───────────────────────────────────────────────────────────

def process_input_folder(input_dir: str | Path) -> list[dict]:
    """
    Process all .json files in input_dir.
    Returns a combined list of flat clinical record dicts.
    """
    folder = Path(input_dir)
    if not folder.exists():
        raise FileNotFoundError(f"Input folder not found: {folder}")

    json_files = sorted(folder.glob("*.json"))
    if not json_files:
        log.warning("No .json files found in %s", folder)
        return []

    log.info("Found %d JSON file(s) in '%s'", len(json_files), folder)
    all_rows: list[dict] = []
    for jf in json_files:
        all_rows.extend(process_json_file(jf))

    log.info("Total rows extracted: %d", len(all_rows))
    return all_rows
