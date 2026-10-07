"""Read-only offline rescoring of stored Sales sessions.

The report uses the same ledger scorer as turn rating and completion. It never
writes a session, run draft or database record and never calls a model.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.sales_concerns import review_overrides, score_ledger
from app.sales_openrouter import PIPELINE_VERSION, RUBRIC_VERSION

REPORT_VERSION = "sales-rescore-v1"
SUPPORTED_RUBRICS = (RUBRIC_VERSION,)
REQUIRED_ENTRY_FIELDS = ("turnId", "labels", "factsKnownBefore", "challengeShownBefore")
OUTCOME_FIELDS = ("score", "rawScore", "policyViolationPenalty", "trustState", "customerRating",
                  "emotionalHandling", "causeIdentification", "solutionSuitability", "trustRebuilding")


def _skip(session: Mapping, reason: str) -> dict:
    return {"sessionId": session.get("sessionId"), "rubricVersion": session.get("rubricVersion"), "reason": reason}


def rescore_session(session: Mapping, rubric_version: str = RUBRIC_VERSION) -> tuple[dict | None, dict | None]:
    """Return (record, None) for a scored session or (None, skip) with a reason."""
    if rubric_version not in SUPPORTED_RUBRICS:
        raise ValueError(f"unsupported rubric version: {rubric_version}")
    ledger = session.get("evidenceLedger")
    if session.get("pipelineMode") != "openrouter":
        return None, _skip(session, "not_openrouter_pipeline")
    if not isinstance(ledger, list) or not ledger:
        return None, _skip(session, "missing_ledger")
    for field in REQUIRED_ENTRY_FIELDS:
        if any(not isinstance(entry, Mapping) or field not in entry for entry in ledger):
            return None, _skip(session, "missing_ledger_field:" + field)
    if session.get("assessmentReviewItems") or session.get("assessmentStatus") == "needs-review":
        return None, _skip(session, "unresolved_review")
    overrides = review_overrides(session)
    scored = score_ledger(ledger, overrides, session.get("endingReason"))
    notes = []
    ended = next((index for index, turn in enumerate(scored["turns"]) if turn["endingReason"]), None)
    if ended is not None and ended < len(ledger) - 1:
        # Under these rules the dialogue would have stopped here; later turns
        # answered replies Lan would never have said.
        scored = score_ledger(ledger[:ended + 1], overrides, session.get("endingReason"))
        notes.append(f"rules_end_dialogue_at_turn_{ended + 1}")
    completed = session.get("completionStatus") == "completed"
    stored = {field: session.get(field) for field in OUTCOME_FIELDS} if completed else None
    rescored = dict(scored["outcome"])
    if any("disclosedFactIds" not in entry for entry in ledger):
        notes.append("disclosure_derived_from_labels")
    if session.get("diagnosticsDeleted"):
        notes.append("diagnostics_deleted_review_decisions_unavailable")
    record = {
        "sessionId": session.get("sessionId"),
        "storedVersions": {"pipelineVersion": session.get("pipelineVersion"), "rubricVersion": session.get("rubricVersion"),
                           "promptVersion": session.get("promptVersion")},
        # Older sessions replay their stored labels under the new rules. Lan's
        # replies, and therefore later utterances, might have differed.
        "hypothetical": session.get("rubricVersion") != rubric_version,
        "completionStatus": session.get("completionStatus"),
        "turns": len(ledger),
        "storedEndingReason": session.get("endingReason"),
        "rescoredEndingReason": scored["endingReason"],
        "storedOutcome": stored,
        "rescoredOutcome": rescored,
        "rescoredComponents": sorted(scored["rubricComponents"]),
        "rescoredConcerns": scored["resolvedConcerns"],
        "notes": notes,
    }
    if stored is not None and isinstance(stored.get("score"), (int, float)):
        record["delta"] = {"score": rescored["score"] - stored["score"],
                           "trustStateChanged": rescored["trustState"] != stored.get("trustState"),
                           "customerRatingChanged": rescored["customerRating"] != stored.get("customerRating")}
    return record, None


def rescore_directory(directory: Path | str, rubric_version: str = RUBRIC_VERSION) -> dict:
    if rubric_version not in SUPPORTED_RUBRICS:
        raise ValueError(f"unsupported rubric version: {rubric_version}")
    directory = Path(directory)
    sessions, skipped = [], []
    for path in sorted(directory.glob("sales-session-*.json")):
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append({"file": path.name, "reason": "unreadable_json"})
            continue
        # Turn records share the file prefix; sessions own completedTurns.
        if not isinstance(data, dict) or "sessionId" not in data or "completedTurns" not in data:
            continue
        record, skip = rescore_session(data, rubric_version)
        if record is not None:
            sessions.append(record)
        else:
            skipped.append(skip)
    changed = [item for item in sessions if item.get("delta") and (item["delta"]["score"] or item["delta"]["trustStateChanged"])]
    return {"reportVersion": REPORT_VERSION, "rubricVersion": rubric_version, "pipelineVersion": PIPELINE_VERSION,
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(), "recordingsDirectory": str(directory.resolve()),
            "summary": {"scored": len(sessions), "hypothetical": sum(1 for item in sessions if item["hypothetical"]),
                        "changed": len(changed), "skipped": len(skipped)},
            "sessions": sessions, "skipped": skipped}


def write_report(report: Mapping, recordings: Path | str, output: Path | str | None = None) -> Path:
    """Write into a new evaluations folder; never into an existing report folder."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = Path(output) if output else Path(recordings) / "evaluations" / f"rescore-{report['rubricVersion']}-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    path = target / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
