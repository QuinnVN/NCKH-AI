"""Rescore stored Sales sessions offline with the shared evidence-ledger scorer.

Read-only: sessions, run drafts and MongoDB are never modified and no model is called.

    python scripts/rescore_sales_sessions.py --rubric sales-rubric-v3
    python scripts/rescore_sales_sessions.py --recordings D:/data/recordings --rubric sales-rubric-v3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.sales_rescore import SUPPORTED_RUBRICS, rescore_directory, write_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recordings", default=get_settings().recordings_dir, help="Recordings directory with sales-session JSON files")
    parser.add_argument("--rubric", required=True, choices=SUPPORTED_RUBRICS, help="Rubric version to score with")
    parser.add_argument("--output", help="New report folder (default: recordings/evaluations/rescore-<rubric>-<time>)")
    args = parser.parse_args()
    report = rescore_directory(args.recordings, args.rubric)
    path = write_report(report, args.recordings, args.output)
    print(json.dumps({"report": str(path), **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
