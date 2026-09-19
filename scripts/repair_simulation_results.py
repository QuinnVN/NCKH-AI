"""Repair local simulation results and upload completed runs to MongoDB.

Run without arguments to inspect the planned work. Pass ``--apply`` only while
the backend is stopped to write repairs, delete rejected files, and upload the
completed aggregates. Add ``--reevaluate`` to select a sale or lawyer draft,
enter its recording session ID, and rebuild its scores from stored transcripts.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import socket
import sys
import unicodedata
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.run_results import (  # noqa: E402
    GAME_IDS,
    RUN_ID,
    RunResultStore,
    _atomic_json,
    _safe_sync_error,
    configured_mongo,
    project_lawyer_record,
    project_sales_part1,
    project_sales_part2,
    utc_now,
)
from app.ai_servers import AIServerManager  # noqa: E402
from app.lawyer_assessment import LLMLawyerAssessor  # noqa: E402
from app.llm_service import LLMService  # noqa: E402
from app.sales_persuasion import LLMSalesAssessor  # noqa: E402
from app.sales_returning_customer import (  # noqa: E402
    BAD_ENDING_RESPONSES,
    LLMSalesAnalyzer,
    _count_outcome,
    _final_customer_response,
)


LAWYER_FIELDS = (
    "roundId",
    "caseId",
    "interviewRestartCount",
    "finalClueSet",
    "completedEvidenceLinks",
    "transcript",
    "criterionScores",
    "rawScore",
    "restartPenaltyPercent",
    "finalScore",
    "feedbackVi",
    "recordingAtUtc",
    "assessmentCompletedAtUtc",
    "completionStatus",
)
REQUIRED_METADATA = (
    "runId",
    "participantName",
    "participantSessionId",
    "gameId",
    "status",
    "startedAtUtc",
    "data",
)
COMPLETION_FIELDS = {
    "clinic": ["patientResults", "round"],
    "doctor": ["cases", "round"],
    "lawyer": ["lawyer"],
    "sale": ["part1", "part2"],
}
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class MongoWriter(Protocol):
    def upsert(self, aggregate: Mapping[str, Any]) -> None: ...


@dataclass
class Artifact:
    path: Path
    run_id: str
    kind: str
    document: dict[str, Any] | None
    error: str | None = None


@dataclass
class Summary:
    scanned_files: int = 0
    repaired_files: int = 0
    finalized_runs: int = 0
    test_runs: int = 0
    invalid_files: int = 0
    deleted_files: int = 0
    upload_ready: int = 0
    uploaded: int = 0
    upload_failed: int = 0
    operation_failed: int = 0
    actions: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.upload_failed or self.operation_failed)


def _artifact_name(path: Path) -> tuple[str, str] | None:
    name = path.name
    if not name.startswith("run-") or not name.endswith(".json"):
        return None
    if name.endswith(".draft.json"):
        return name[4:-11], "draft"
    if name.endswith(".sync.json"):
        return name[4:-10], "sync"
    return name[4:-5], "aggregate"


def _read_artifact(path: Path, run_id: str, kind: str) -> Artifact:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return Artifact(path, run_id, kind, None, f"cannot read JSON: {exc}")
    if not isinstance(value, dict):
        return Artifact(path, run_id, kind, None, "JSON root must be an object")
    return Artifact(path, run_id, kind, value)


def _normalized_test_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    return normalized.casefold() == "test"


def _lawyer_payload_is_complete(data: Mapping[str, Any]) -> bool:
    return (
        data.get("completionStatus") == "completed"
        and isinstance(data.get("transcript"), str)
        and isinstance(data.get("criterionScores"), Mapping)
        and data.get("rawScore") is not None
        and data.get("finalScore") is not None
    )


def _canonicalize_lawyer(document: dict[str, Any]) -> bool:
    data = document.get("data")
    if not isinstance(data, dict):
        return False
    nested = data.get("lawyer")
    nested_complete = isinstance(nested, Mapping) and _lawyer_payload_is_complete(nested)
    legacy_complete = _lawyer_payload_is_complete(data)
    looks_like_lawyer = (
        document.get("gameId") == "lawyer"
        or "lawyer" in document.get("requiredFields", [])
        or legacy_complete
    )
    if not looks_like_lawyer:
        return False

    changed = False
    if document.get("gameId") == "completed" and legacy_complete:
        document["gameId"] = "lawyer"
        changed = True

    if legacy_complete:
        canonical = dict(nested) if isinstance(nested, Mapping) else {}
        for key in LAWYER_FIELDS:
            if key in data:
                canonical[key] = deepcopy(data[key])
        if canonical != nested:
            data["lawyer"] = canonical
            changed = True
        for key in LAWYER_FIELDS:
            if key in data:
                del data[key]
                changed = True
    elif nested_complete and document.get("gameId") == "completed":
        document["gameId"] = "lawyer"
        changed = True
    return changed


def _metadata_error(document: Mapping[str, Any], run_id: str, kind: str) -> str | None:
    if document.get("runId") != run_id:
        return "filename run ID does not match document runId"
    if RUN_ID.fullmatch(run_id) is None:
        return "runId has an invalid format"
    if kind == "sync":
        return None
    missing = [key for key in REQUIRED_METADATA if key not in document]
    if kind == "aggregate" and "completedAtUtc" not in document:
        missing.append("completedAtUtc")
    if missing:
        return "missing required fields: " + ", ".join(missing)
    if not isinstance(document.get("participantName"), str) or not document["participantName"].strip():
        return "participantName must be non-empty text"
    if not isinstance(document.get("participantSessionId"), str) or not document["participantSessionId"].strip():
        return "participantSessionId must be non-empty text"
    if document.get("gameId") not in GAME_IDS:
        return "gameId is invalid"
    if not isinstance(document.get("data"), Mapping):
        return "data must be an object"
    if kind == "draft":
        if document.get("status") not in {"draft", "aborted", "finalized"}:
            return "draft status is invalid"
        if not isinstance(document.get("fragments", {}), Mapping):
            return "fragments must be an object"
    elif document.get("status") != "completed":
        return "aggregate status must be completed"
    elif document.get("_id") != run_id:
        return "aggregate _id must match runId"
    return None


def _aggregate_completion_error(document: Mapping[str, Any]) -> str | None:
    game_id = document.get("gameId")
    data = document.get("data")
    if game_id not in COMPLETION_FIELDS or not isinstance(data, Mapping):
        return "aggregate gameId is invalid"
    if game_id == "clinic":
        if not isinstance(data.get("patientResults"), list) or not data["patientResults"] or not isinstance(data.get("round"), Mapping):
            return "aggregate does not contain a complete clinic result"
    elif game_id == "doctor":
        if not isinstance(data.get("cases"), list) or not data["cases"] or not isinstance(data.get("round"), Mapping):
            return "aggregate does not contain a complete doctor result"
    elif game_id == "lawyer":
        candidate = {"gameId": game_id, "data": data, "requiredFields": ["lawyer"]}
        if not RunResultStore._ready_to_finalize(candidate):
            return "aggregate does not contain a complete lawyer result"
    else:
        part1 = data.get("part1")
        part2 = data.get("part2")
        turns = data.get("turns", [])
        part1_fields = {
            "attemptId", "selectedShoeId", "bestFitShoeId", "transcript",
            "score", "feedbackVi", "recordingAtUtc", "assessmentCompletedAtUtc",
        }
        flags = {"emotionalHandling", "causeIdentification", "solutionSuitability", "trustRebuilding"}
        if not isinstance(part1, Mapping) or not part1_fields.issubset(part1):
            return "aggregate does not contain a complete sales part 1 result"
        if not isinstance(part2, Mapping) or not flags.issubset(part2):
            return "aggregate does not contain a complete sales part 2 result"
        if not part2.get("trustState") or not part2.get("completionReason"):
            return "aggregate does not contain a completed sales outcome"
        score = part2.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
            return "aggregate sales score is invalid"
        valid_rating = part2.get("customerRating") in {"bad", "considering", "good"}
        if not valid_rating or not isinstance(part2.get("criterionScores"), Mapping):
            return "aggregate sales rating or criterion scores are invalid"
        accepted_turn_count = part2.get("acceptedTurnCount")
        valid_turn_count = isinstance(accepted_turn_count, int) and accepted_turn_count >= 0
        if not isinstance(turns, list) or not valid_turn_count or len(turns) < accepted_turn_count:
            return "aggregate sales turns are incomplete"
    return None


def _finalized_draft_omission_reason(draft: Mapping[str, Any]) -> str:
    """Explain why a syntactically valid finalized draft cannot be rebuilt."""
    if draft.get("completionRequested") is not True:
        return "completion was not requested"

    data = draft.get("data")
    required = draft.get("requiredFields", [])
    if not isinstance(data, Mapping):
        return "result data is not an object"
    if not isinstance(required, list):
        return "requiredFields is not a list"
    missing = [field for field in required if field not in data or data[field] is None]
    if missing:
        return "missing required result fields: " + ", ".join(sorted(missing))

    if draft.get("gameId") == "sale" and "part2" in required:
        part2 = data.get("part2")
        if not isinstance(part2, Mapping):
            return "sales part 2 result is not an object"
        score = part2.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
            return "sales part 2 score must be an integer from 0 through 100"
        criterion_scores = part2.get("criterionScores")
        if not isinstance(criterion_scores, Mapping):
            return "sales part 2 criterionScores is not an object"
        criterion_values = (
            criterion_scores.get("apologyAndPolicyRemedy"),
            criterion_scores.get("adaptabilityAndDeescalation"),
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 50
               for value in criterion_values):
            return "sales part 2 criterion scores must be integers from 0 through 50"
        criterion_total = sum(criterion_values)
        raw_score = part2.get("rawScore", criterion_total)
        if not isinstance(raw_score, int) or isinstance(raw_score, bool) or raw_score != criterion_total:
            return (
                f"sales part 2 raw score ({raw_score}) does not match "
                f"criterion total ({criterion_total})"
            )
        penalty = part2.get("policyViolationPenalty", 0)
        if (
            not isinstance(penalty, int)
            or isinstance(penalty, bool)
            or not 0 <= penalty <= raw_score
        ):
            return "sales part 2 policy violation penalty is invalid"
        expected_score = raw_score - penalty
        if expected_score != score:
            return (
                f"sales part 2 score ({score}) does not match penalty-adjusted "
                f"score ({raw_score} - {penalty} = {expected_score})"
            )

    return "result data does not meet completion requirements"


def _stored_completed_at(document: Mapping[str, Any]) -> str | None:
    data = document.get("data", {})
    if isinstance(data, Mapping):
        lawyer = data.get("lawyer")
        if isinstance(lawyer, Mapping) and lawyer.get("assessmentCompletedAtUtc"):
            return str(lawyer["assessmentCompletedAtUtc"])
        part2 = data.get("part2")
        if isinstance(part2, Mapping) and part2.get("completedAtUtc"):
            return str(part2["completedAtUtc"])
        round_data = data.get("round")
        if isinstance(round_data, Mapping) and round_data.get("roundCompletedAtUtc"):
            return str(round_data["roundCompletedAtUtc"])
    return None


def _completed_at(draft: Mapping[str, Any]) -> str:
    return _stored_completed_at(draft) or str(draft.get("updatedAtUtc") or utc_now())


def _aggregate_from_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(draft["runId"])
    return {
        "_id": run_id,
        "runId": run_id,
        "participantName": draft["participantName"],
        "participantSessionId": draft["participantSessionId"],
        "gameId": draft["gameId"],
        "status": "completed",
        "startedAtUtc": draft["startedAtUtc"],
        "completedAtUtc": _completed_at(draft),
        "data": deepcopy(draft.get("data", {})),
    }


def _sync_success(run_id: str, previous: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "runId": run_id,
        "attemptCount": int((previous or {}).get("attemptCount", 0)),
        "synchronized": True,
        "lastError": None,
        "updatedAtUtc": utc_now(),
        "nextAttemptAtUtc": None,
    }


def _sync_failure(run_id: str, previous: Mapping[str, Any] | None, error: Exception) -> dict[str, Any]:
    return {
        "runId": run_id,
        "attemptCount": int((previous or {}).get("attemptCount", 0)) + 1,
        "synchronized": False,
        "lastError": _safe_sync_error(error),
        "updatedAtUtc": utc_now(),
        "nextAttemptAtUtc": None,
    }


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {description} '{path.name}': {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"The {description} '{path.name}' must contain a JSON object.")
    return value


def _recording_path(recordings_dir: Path, game_id: str, session_id: str) -> Path:
    if SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("sessionId has an invalid format")
    prefix = "sales-session" if game_id == "sale" else "lawyer-defense"
    path = (recordings_dir.resolve() / f"{prefix}-{session_id}.json").resolve()
    if path.parent != recordings_dir.resolve():
        raise ValueError("sessionId resolves outside the recordings directory")
    if not path.is_file():
        raise FileNotFoundError(f"No {game_id} recording session found at '{path}'.")
    return path


def _sales_part1_recording(
    recordings_dir: Path,
    session: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> dict[str, Any]:
    attempt_id = session.get("part1AttemptId")
    if not isinstance(attempt_id, str) or not attempt_id:
        data = draft.get("data")
        part1 = data.get("part1") if isinstance(data, Mapping) else None
        attempt_id = part1.get("attemptId") if isinstance(part1, Mapping) else None
    if not isinstance(attempt_id, str) or SESSION_ID.fullmatch(attempt_id) is None:
        raise RuntimeError("The sales session does not identify a valid part 1 attempt.")
    path = recordings_dir.resolve() / f"sales-persuasion-{attempt_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"No sales part 1 recording found at '{path}'.")
    record = _read_json_object(path, "sales part 1 recording")
    if record.get("attemptId") != attempt_id:
        raise RuntimeError(f"The attemptId in '{path.name}' does not match its filename.")
    return record


async def reevaluate_draft(
    draft: Mapping[str, Any],
    recordings_dir: Path,
    session_id: str,
    service: LLMService,
) -> dict[str, Any]:
    """Rebuild one sale or lawyer draft from stored transcripts and fresh LLM scores."""

    game_id = draft.get("gameId")
    if game_id not in {"sale", "lawyer"}:
        raise ValueError("Only sale and lawyer drafts can be re-evaluated.")
    session_path = _recording_path(recordings_dir, str(game_id), session_id)
    session = _read_json_object(session_path, f"{game_id} recording session")
    identity_field = "sessionId" if game_id == "sale" else "roundId"
    if session.get(identity_field) != session_id:
        raise RuntimeError(f"The {identity_field} in '{session_path.name}' does not match its filename.")

    repaired = deepcopy(dict(draft))
    data = repaired.setdefault("data", {})
    if not isinstance(data, dict):
        raise RuntimeError("The draft data field must be an object.")

    if game_id == "lawyer":
        transcript = session.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise RuntimeError("The lawyer recording session has no transcript to evaluate.")
        assessment = await LLMLawyerAssessor(service).assess(session, transcript)
        restart_count = int(session.get("interviewRestartCount", 0))
        penalty_percent = 50 if restart_count > 3 else 0
        raw_score = assessment.raw_score
        evaluated = dict(session)
        evaluated.update(
            assessmentStatus="completed",
            criteria={
                "evidenceUse": assessment.evidence_use,
                "logicalConnections": assessment.logical_connections,
                "conclusionFidelity": assessment.conclusion_fidelity,
                "clarityAndPersuasiveness": assessment.clarity_and_persuasiveness,
            },
            rawScore=raw_score,
            restartPenaltyPercent=penalty_percent,
            finalScore=raw_score * (1.0 - penalty_percent / 100.0),
            feedbackVi=assessment.feedback_vi,
            updatedAtUtc=utc_now(),
        )
        lawyer_result = project_lawyer_record(evaluated)
        lawyer_result["roundId"] = session.get("roundId")
        data["lawyer"] = lawyer_result
        required = {"lawyer"}
    else:
        part1_record = _sales_part1_recording(recordings_dir, session, repaired)
        transcript = part1_record.get("transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            raise RuntimeError("The sales part 1 recording has no transcript to evaluate.")
        part1_assessment = await LLMSalesAssessor(service).assess(part1_record, transcript)
        evaluated_part1 = dict(part1_record)
        evaluated_part1.update(
            score=part1_assessment.score,
            feedbackVi=part1_assessment.feedback_vi,
            assessmentStatus="completed",
            updatedAtUtc=utc_now(),
        )

        turns = session.get("turns")
        if not isinstance(turns, list) or not turns:
            raise RuntimeError("The sales session has no turn transcripts to evaluate.")
        if any(not isinstance(turn, Mapping) or not str(turn.get("transcript", "")).strip() for turn in turns):
            raise RuntimeError("Every sales turn must contain a transcript.")
        analysis = await LLMSalesAnalyzer(service).analyze(session)
        criterion_scores = analysis.criterion_scores
        raw_score = (
            criterion_scores.apology_and_policy_remedy
            + criterion_scores.adaptability_and_deescalation
        )
        violations = session.get("policyViolations", [])
        violation_count = len(violations) if isinstance(violations, list) else 0
        good_count = int(session.get("goodResponseCount", 0))
        bad_count = int(session.get("badResponseCount", 0))
        customer_rating, trust_state = _count_outcome(good_count, bad_count)
        if good_count == bad_count == 0 and (
            session.get("trustState") == "lost" or int(session.get("silenceCount", 0)) >= 2
        ):
            customer_rating, trust_state = "bad", "lost"
        final_customer_text = _final_customer_response(good_count, bad_count)
        if customer_rating == "bad" and good_count == bad_count == 0:
            final_customer_text = BAD_ENDING_RESPONSES[0]
        penalty = min(raw_score, violation_count * 10)
        evaluated_session = dict(session)
        evaluated_session.update(
            status="finished",
            completionStatus="completed",
            completionReason=session.get("completionReason") or "manual_repair",
            **analysis.model_dump(by_alias=True),
            rawScore=raw_score,
            policyViolationPenalty=penalty,
            score=raw_score - penalty,
            customerRating=customer_rating,
            trustState=trust_state,
            finalCustomerText=final_customer_text,
            acceptedTurnCount=int(session.get("acceptedTurnCount", len(turns))),
            updatedAtUtc=utc_now(),
        )
        projected = project_sales_part2(evaluated_session)
        data["part1"] = project_sales_part1(evaluated_part1)
        data["turns"] = projected.pop("turns")
        data["part2"] = projected
        required = {"part1", "part2"}

    prior_required = repaired.get("requiredFields", [])
    if not isinstance(prior_required, list):
        prior_required = []
    repaired["requiredFields"] = sorted(set(prior_required) | required)
    repaired["completionRequested"] = True
    repaired["updatedAtUtc"] = utc_now()
    return repaired


def _llm_endpoint_reachable(service: LLMService) -> bool:
    parsed = urlparse(service.base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _draft_timestamp(path: Path, draft: Mapping[str, Any]) -> float:
    for field_name in ("updatedAtUtc", "startedAtUtc"):
        value = draft.get(field_name)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _draft_display_date(path: Path, draft: Mapping[str, Any]) -> str:
    return datetime.fromtimestamp(_draft_timestamp(path, draft)).astimezone().strftime(
        "%d %b %Y, %H:%M"
    )


def _reevaluation_candidates(results_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in results_dir.glob("run-*.draft.json"):
        try:
            draft = _read_json_object(path, "draft")
        except RuntimeError:
            continue
        if draft.get("gameId") in {"sale", "lawyer"}:
            candidates.append((path, draft))
    return sorted(
        candidates,
        key=lambda candidate: (_draft_timestamp(*candidate), candidate[0].name),
        reverse=True,
    )


def _interactive_reevaluation(
    results_dir: Path,
    recordings_dir: Path,
    *,
    input_fn=input,
) -> bool:
    candidates = _reevaluation_candidates(results_dir)
    if not candidates:
        print("No sale or lawyer draft files were found for re-evaluation.")
        return False

    print("Drafts available for transcript re-evaluation:")
    for index, (path, draft) in enumerate(candidates, start=1):
        date = _draft_display_date(path, draft)
        print(
            f"  {index}. {path.name} [{date}] "
            f"({draft['gameId']}, {draft.get('participantName', 'unknown')})"
        )
    raw_selection = input_fn("Select a draft number, or press Enter to cancel: ").strip()
    if not raw_selection:
        return False
    try:
        selection = int(raw_selection)
        draft_path, draft = candidates[selection - 1]
    except (ValueError, IndexError):
        print("Invalid draft selection.", file=sys.stderr)
        return False

    session_id = input_fn("Enter the recording sessionId: ").strip()
    try:
        session_path = _recording_path(recordings_dir, str(draft["gameId"]), session_id)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Re-evaluation failed: {exc}", file=sys.stderr)
        return False
    print(f"Found recording session: {session_path}")

    manager = AIServerManager()
    service = LLMService()
    reachable = _llm_endpoint_reachable(service)
    try:
        prompt = (
            "The LLM service is reachable. Start re-evaluation now? [Y/n]: "
            if reachable
            else "The LLM service is not reachable. Start it and continue? [Y/n]: "
        )
        if input_fn(prompt).strip().casefold() not in {"", "y", "yes"}:
            asyncio.run(service.close())
            print("Re-evaluation cancelled.")
            return False

        async def run() -> dict[str, Any]:
            try:
                if not reachable:
                    await manager.start_detached("llama")
                return await reevaluate_draft(draft, recordings_dir, session_id, service)
            finally:
                await service.close()

        repaired = asyncio.run(run())
        _atomic_json(draft_path, repaired)
        print(f"Re-evaluated and updated {draft_path.name}.")
        return True
    except Exception as exc:
        print(f"Re-evaluation failed: {exc}", file=sys.stderr)
        return False


def maintain_results(results_dir: Path, *, apply: bool, mongo: MongoWriter | None = None) -> Summary:
    summary = Summary()
    results_dir = results_dir.resolve()
    artifacts: list[Artifact] = []
    if results_dir.exists():
        for path in sorted(results_dir.glob("run-*.json")):
            parsed = _artifact_name(path)
            if parsed is None:
                continue
            artifacts.append(_read_artifact(path, *parsed))
    summary.scanned_files = len(artifacts)

    groups: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
        groups.setdefault(artifact.run_id, []).append(artifact)

    writes: dict[Path, dict[str, Any]] = {}
    deletions: set[Path] = set()
    aggregates: dict[str, dict[str, Any]] = {}
    recoverable_finalized_drafts: dict[str, dict[str, Any]] = {}
    reevaluation_drafts: dict[str, tuple[Path, dict[str, Any]]] = {}
    sync_documents: dict[str, dict[str, Any]] = {}

    for run_id, family in sorted(groups.items()):
        is_test = any(
            artifact.document is not None
            and _normalized_test_name(artifact.document.get("participantName"))
            for artifact in family
        )
        if is_test:
            summary.test_runs += 1
            deletions.update(artifact.path for artifact in family)
            summary.actions.append(f"DELETE test run {run_id} ({len(family)} files)")
            continue

        by_kind = {artifact.kind: artifact for artifact in family}
        for artifact in family:
            if artifact.error:
                summary.invalid_files += 1
                deletions.add(artifact.path)
                summary.actions.append(f"DELETE invalid {artifact.path.name}: {artifact.error}")
                if artifact.kind == "aggregate" and "sync" in by_kind:
                    deletions.add(by_kind["sync"].path)
                continue

            document = deepcopy(artifact.document)
            assert document is not None
            changed = _canonicalize_lawyer(document)
            if artifact.kind == "aggregate" and "_id" not in document:
                document["_id"] = run_id
                changed = True
            if artifact.kind == "aggregate" and not document.get("completedAtUtc"):
                stored_completion = _stored_completed_at(document)
                if stored_completion:
                    document["completedAtUtc"] = stored_completion
                    changed = True
            error = _metadata_error(document, run_id, artifact.kind)
            if error is None and artifact.kind == "aggregate":
                error = _aggregate_completion_error(document)
            if error:
                summary.invalid_files += 1
                deletions.add(artifact.path)
                summary.actions.append(f"DELETE invalid {artifact.path.name}: {error}")
                if artifact.kind == "aggregate" and "sync" in by_kind:
                    deletions.add(by_kind["sync"].path)
                continue

            if artifact.kind == "sync":
                sync_documents[run_id] = document
                continue

            if artifact.kind == "draft":
                if changed:
                    writes[artifact.path] = document
                    summary.repaired_files += 1
                    summary.actions.append(f"REPAIR {artifact.path.name}")
                ready = document.get("completionRequested") is True and RunResultStore._ready_to_finalize(document)
                if document.get("status") == "finalized" and ready:
                    recoverable_finalized_drafts[run_id] = document
                elif document.get("gameId") in {"sale", "lawyer"} and not ready:
                    reevaluation_drafts[run_id] = (artifact.path, document)
                elif document.get("status") in {"draft", "aborted"} and ready:
                    aggregate = _aggregate_from_draft(document)
                    document["status"] = "finalized"
                    writes[artifact.path] = document
                    aggregate_path = results_dir / f"run-{run_id}.json"
                    writes[aggregate_path] = aggregate
                    aggregates[run_id] = aggregate
                    summary.finalized_runs += 1
                    summary.actions.append(f"FINALIZE run {run_id}")
                continue

            if changed:
                writes[artifact.path] = document
                summary.repaired_files += 1
                summary.actions.append(f"REPAIR {artifact.path.name}")
            elif artifact.path in writes:
                # A valid aggregate already on disk takes precedence over a
                # reconstruction scheduled from an inconsistent paired draft.
                del writes[artifact.path]
            aggregates[run_id] = document

    for run_id, draft in recoverable_finalized_drafts.items():
        if run_id in aggregates:
            continue
        aggregate = _aggregate_from_draft(draft)
        aggregate_path = results_dir / f"run-{run_id}.json"
        writes[aggregate_path] = aggregate
        deletions.discard(aggregate_path)
        aggregates[run_id] = aggregate
        summary.repaired_files += 1
        summary.actions.append(f"REBUILD {aggregate_path.name} from finalized draft")

    if not apply:
        for run_id, (path, draft) in sorted(reevaluation_drafts.items()):
            if run_id in aggregates:
                continue
            summary.actions.append(
                f"WARNING {path.name} needs re-evaluation: "
                f"{_finalized_draft_omission_reason(draft)}. "
                "Run again with --apply --reevaluate."
            )

    for path in writes:
        deletions.discard(path)
    summary.upload_ready = len(aggregates)

    if not apply:
        summary.deleted_files = len(deletions)
        return summary
    if mongo is None:
        summary.operation_failed += 1
        summary.actions.append("ERROR MongoDB is not configured")
        return summary

    for path in sorted(deletions):
        try:
            path.unlink(missing_ok=True)
            summary.deleted_files += 1
        except OSError as exc:
            summary.operation_failed += 1
            summary.actions.append(f"ERROR deleting {path.name}: {exc}")

    for path, document in sorted(writes.items(), key=lambda item: str(item[0])):
        try:
            _atomic_json(path, document)
        except OSError as exc:
            summary.operation_failed += 1
            summary.actions.append(f"ERROR writing {path.name}: {exc}")
            if path.name.endswith(".json") and not path.name.endswith((".draft.json", ".sync.json")):
                aggregates.pop(str(document.get("runId", "")), None)

    for run_id, aggregate in sorted(aggregates.items()):
        sync_path = results_dir / f"run-{run_id}.sync.json"
        previous = sync_documents.get(run_id)
        try:
            mongo.upsert(aggregate)
            _atomic_json(sync_path, _sync_success(run_id, previous))
            summary.uploaded += 1
        except Exception as exc:
            summary.upload_failed += 1
            summary.actions.append(f"ERROR uploading run {run_id}: {_safe_sync_error(exc)}")
            try:
                _atomic_json(sync_path, _sync_failure(run_id, previous, exc))
            except OSError as write_error:
                summary.operation_failed += 1
                summary.actions.append(f"ERROR writing {sync_path.name}: {write_error}")
    return summary


def _print_summary(summary: Summary, *, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY RUN"
    print(f"Simulation-results maintenance: {mode}")
    for action in summary.actions:
        print(f"  {action}")
    print(
        "Summary: "
        f"scanned={summary.scanned_files}, repaired={summary.repaired_files}, "
        f"finalized={summary.finalized_runs}, test_runs={summary.test_runs}, "
        f"invalid={summary.invalid_files}, deleted={summary.deleted_files}, "
        f"upload_ready={summary.upload_ready}, uploaded={summary.uploaded}, "
        f"upload_failed={summary.upload_failed}, operation_failed={summary.operation_failed}"
    )
    if not apply:
        print("No files or MongoDB documents were changed. Run again with --apply to apply these actions.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write repairs, delete rejected files, and upload results")
    parser.add_argument(
        "--reevaluate",
        action="store_true",
        help="interactively rebuild one sale or lawyer draft from recording transcripts; requires --apply",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        help="simulation-results directory; defaults to the configured RECORDINGS_DIR",
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        help="recordings directory used by --reevaluate; defaults to the parent of the results directory",
    )
    args = parser.parse_args(argv)
    if args.reevaluate and not args.apply:
        parser.error("--reevaluate requires --apply")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = RunResultStore()
    results_dir = args.results_dir.resolve() if args.results_dir else store.directory
    recordings_dir = (
        args.recordings_dir.resolve()
        if args.recordings_dir
        else results_dir.parent
    )
    mongo = None
    if args.apply:
        mongo = configured_mongo()
        if mongo is None:
            print("Maintenance failed: MONGODB_URI is not configured.", file=sys.stderr)
            return 2
        try:
            client = getattr(mongo, "client", None)
            if client is None:
                raise RuntimeError("configured MongoDB client is unavailable")
            client.admin.command("ping")
            mongo.ensure_indexes()
        except Exception as exc:
            print(f"Maintenance failed before local changes: {_safe_sync_error(exc)}", file=sys.stderr)
            return 2

    if args.reevaluate and not _interactive_reevaluation(results_dir, recordings_dir):
        return 1

    summary = maintain_results(results_dir, apply=args.apply, mongo=mongo)
    _print_summary(summary, apply=args.apply)
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
