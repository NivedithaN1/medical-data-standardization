"""
main.py — Schema Engine Entry Point
====================================
Usage:
    python main.py                          # uses .env for API key
    python main.py --gemini-key AIza...     # pass key directly
    python main.py --no-gemini              # skip Tier 3 entirely
    python main.py --input ./my_folder --output result.xlsx
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ── Load .env before anything else ────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Local modules ──────────────────────────────────────────────────────────────
from normalizer import init_gemini
from parser import process_input_folder
from excel_writer import write_excel


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schema-engine",
        description="Medical JSON → Standardised Excel (3-Tier NLP Pipeline)",
    )
    p.add_argument(
        "--input", "-i",
        default="./input",
        help="Path to the folder containing input .json files  [default: ./input]",
    )
    p.add_argument(
        "--output", "-o",
        default="",
        help=(
            "Path for the output .xlsx file. "
            "Defaults to output/clinical_records_<timestamp>.xlsx"
        ),
    )
    p.add_argument(
        "--gemini-key", "-k",
        default="",
        help=(
            "Google Gemini API key for Tier-3 fallback. "
            "Can also be set via GEMINI_API_KEY in .env"
        ),
    )
    p.add_argument(
        "--no-gemini",
        action="store_true",
        help="Disable Tier-3 Gemini fallback (run Tier 1 + Tier 2 only)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    return p


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = _build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Resolve output path ──
    if args.output:
        output_path = Path(args.output)
    else:
        out_dir = Path("./output")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = out_dir / f"clinical_records_{ts}.xlsx"

    # ── Initialise Gemini (Tier 3) ──
    if not args.no_gemini:
        api_key = args.gemini_key or os.getenv("GEMINI_API_KEY", "")
        if api_key:
            log.info("Initialising Gemini Tier-3 fallback …")
            init_gemini(api_key)
        else:
            log.warning(
                "No Gemini API key found.  "
                "Tier-3 fallback is disabled.\n"
                "  → Set GEMINI_API_KEY in your .env  OR  pass --gemini-key <key>"
            )
    else:
        log.info("Tier-3 (Gemini) disabled via --no-gemini flag.")

    # ── Banner ──
    log.info("=" * 60)
    log.info("  MEDICAL SCHEMA ENGINE  —  3-Tier Normalisation Pipeline")
    log.info("=" * 60)
    log.info("  Input  : %s", Path(args.input).resolve())
    log.info("  Output : %s", output_path.resolve())
    log.info("-" * 60)

    # ── Phase 1 · Parse JSON files ──
    log.info("Phase 1 › Parsing JSON files …")
    try:
        rows = process_input_folder(args.input)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        sys.exit(1)

    if not rows:
        log.error("No records extracted — check your input files.")
        sys.exit(1)

    log.info("Phase 1 complete › %d clinical records extracted.", len(rows))
    log.info("-" * 60)

    # ── Phase 2 · Write Excel ──
    log.info("Phase 2 › Writing Excel workbook …")
    try:
        final_path = write_excel(rows, output_path)
    except ImportError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Excel write failed: %s", exc, exc_info=True)
        sys.exit(1)

    log.info("-" * 60)
    log.info("Done!  Output saved to:")
    log.info("    %s", final_path.resolve())
    log.info("=" * 60)

    # ── Quick tier breakdown ──
    from collections import Counter
    methods = Counter(r.get("normalization_method", "Unresolved") for r in rows)
    log.info("Normalization breakdown:")
    for method, count in sorted(methods.items()):
        pct = count / len(rows) * 100
        log.info("  %-22s %4d  (%5.1f%%)", method, count, pct)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
