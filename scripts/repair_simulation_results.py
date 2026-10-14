"""Repair local simulation results and upload completed runs to MongoDB.

Run the script while the backend is stopped. Each round it scans the results
directory, prints the planned repairs and offers a menu: apply the plan and
upload pending runs, re-upload every completed run, re-evaluate a sale or
lawyer draft from its recordings, or quit. Nothing changes until an action is
chosen. For a sales session marked needs-review, re-evaluation can re-run every
turn's stored audio through the current STT, Jev, Luna and writer pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.run_results import (  # noqa: E402
    GAME_IDS,
    RUN_ID,
    RunResultStore,
    atomic_json,
    completion_problem,
    configured_mongo,
    is_test_participant,
    project_lawyer_record,
    project_sales_part1,
    project_sales_part2,
    safe_sync_error,
    utc_now,
)
from app.ai_servers import AIServerManager  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.lawyer_assessment import LLMLawyerAssessor, assessment_fields  # noqa: E402
from app.llm_service import LLMService, create_llm_service  # noqa: E402
from app.sales_persuasion import LLMSalesAssessor, SalesAudio  # noqa: E402
from app.sales_pipeline import (  # noqa: E402
    apply_completed_outcome,
    complete as sales_pipeline_complete,
    process_turn,
)
from app.sales_returning_customer import (  # noqa: E402
    CaptureEvidence,
    CompletionRequest,
    LLMSalesAnalyzer,
    ReturningSessionStore,
    ReturningTurnRequest,
    legacy_completed_outcome,
)
from app.sales_rubric import VERSIONS as SALES_VERSIONS  # noqa: E402
from app.sherpa_stt import SherpaOnnxTranscriber  # noqa: E402


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
# Model timeouts per re-run turn; each retry resumes from the turn checkpoint.
TURN_ATTEMPTS = 3
YES = {"", "y", "yes"}


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


@dataclass
class Plan:
    """Everything one scan decided; applying it is a separate step."""

    results_dir: Path
    summary: Summary = field(default_factory=Summary)
    writes: dict[Path, dict[str, Any]] = field(default_factory=dict)
    deletions: set[Path] = field(default_factory=set)
    aggregates: dict[str, dict[str, Any]] = field(default_factory=dict)
    sync_documents: dict[str, dict[str, Any]] = field(default_factory=dict)

    def aggregate_path(self, run_id: str) -> Path:
        return self.results_dir / f"run-{run_id}.json"

    @property
    def pending_uploads(self) -> list[str]:
        """Runs whose aggregate changed or whose sidecar is not synchronized."""
        return sorted(
            run_id for run_id in self.aggregates
            if self.aggregate_path(run_id) in self.writes
            or self.sync_documents.get(run_id, {}).get("synchronized") is not True
        )

    @property
    def has_file_changes(self) -> bool:
        return bool(self.writes or self.deletions)


@dataclass
class SalesPipeline:
    """Live turn pipeline components; None picks the production adapter."""

    transcriber: Any = None
    classifier: Any = None
    writer: Any = None
    arbitrator: Any = None


# --- Scanning and planning -------------------------------------------------


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
    if game_id not in COMPLETION_FIELDS:
        return "aggregate gameId is invalid"
    problem = completion_problem({
        "gameId": game_id, "data": document.get("data"), "requiredFields": COMPLETION_FIELDS[game_id],
    }, published=True)
    return f"aggregate is incomplete: {problem}" if problem else None


def _draft_problem(draft: Mapping[str, Any]) -> str | None:
    if draft.get("completionRequested") is not True:
        return "completion was not requested"
    return completion_problem(draft)


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
        "completedAtUtc": _stored_completed_at(draft) or str(draft.get("updatedAtUtc") or utc_now()),
        "data": deepcopy(draft.get("data", {})),
    }


def _reject(plan: Plan, artifact: Artifact, reason: str, by_kind: Mapping[str, Artifact]) -> None:
    plan.summary.invalid_files += 1
    plan.deletions.add(artifact.path)
    plan.summary.actions.append(f"DELETE invalid {artifact.path.name}: {reason}")
    # A sidecar describes its aggregate; it cannot outlive it.
    if artifact.kind == "aggregate" and "sync" in by_kind:
        plan.deletions.add(by_kind["sync"].path)


def plan_maintenance(results_dir: Path) -> Plan:
    """Scan the results directory and decide every repair without touching it."""
    results_dir = results_dir.resolve()
    plan = Plan(results_dir)
    summary = plan.summary
    artifacts: list[Artifact] = []
    if results_dir.exists():
        for path in sorted(results_dir.glob("run-*.json")):
            parsed = _artifact_name(path)
            if parsed is not None:
                artifacts.append(_read_artifact(path, *parsed))
    summary.scanned_files = len(artifacts)

    groups: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
        groups.setdefault(artifact.run_id, []).append(artifact)

    finalized_drafts: dict[str, dict[str, Any]] = {}
    incomplete_drafts: dict[str, tuple[Path, dict[str, Any]]] = {}
    for run_id, family in sorted(groups.items()):
        if any(artifact.document is not None and is_test_participant(artifact.document.get("participantName"))
               for artifact in family):
            summary.test_runs += 1
            plan.deletions.update(artifact.path for artifact in family)
            summary.actions.append(f"DELETE test run {run_id} ({len(family)} files)")
            continue

        by_kind = {artifact.kind: artifact for artifact in family}
        for artifact in family:
            if artifact.error:
                _reject(plan, artifact, artifact.error, by_kind)
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
                _reject(plan, artifact, error, by_kind)
                continue

            if artifact.kind == "sync":
                plan.sync_documents[run_id] = document
                continue

            if changed:
                plan.writes[artifact.path] = document
                summary.repaired_files += 1
                summary.actions.append(f"REPAIR {artifact.path.name}")

            if artifact.kind == "draft":
                ready = _draft_problem(document) is None
                if document.get("status") == "finalized" and ready:
                    finalized_drafts[run_id] = document
                elif document.get("gameId") in {"sale", "lawyer"} and not ready:
                    incomplete_drafts[run_id] = (artifact.path, document)
                elif document.get("status") in {"draft", "aborted"} and ready:
                    aggregate = _aggregate_from_draft(document)
                    document["status"] = "finalized"
                    plan.writes[artifact.path] = document
                    plan.writes[plan.aggregate_path(run_id)] = aggregate
                    plan.aggregates[run_id] = aggregate
                    summary.finalized_runs += 1
                    summary.actions.append(f"FINALIZE run {run_id}")
                continue

            if not changed:
                # A valid aggregate already on disk takes precedence over a
                # reconstruction scheduled from an inconsistent paired draft.
                plan.writes.pop(artifact.path, None)
            plan.aggregates[run_id] = document

    for run_id, draft in finalized_drafts.items():
        if run_id in plan.aggregates:
            continue
        aggregate = _aggregate_from_draft(draft)
        aggregate_path = plan.aggregate_path(run_id)
        plan.writes[aggregate_path] = aggregate
        plan.aggregates[run_id] = aggregate
        summary.repaired_files += 1
        summary.actions.append(f"REBUILD {aggregate_path.name} from finalized draft")

    for run_id, (path, draft) in sorted(incomplete_drafts.items()):
        if run_id not in plan.aggregates:
            summary.actions.append(f"WARNING {path.name} needs re-evaluation: {_draft_problem(draft)}.")

    plan.deletions.difference_update(plan.writes)
    summary.upload_ready = len(plan.aggregates)
    return plan


# --- Applying a plan -------------------------------------------------------


def _sync_record(run_id: str, previous: Mapping[str, Any] | None, error: Exception | None) -> dict[str, Any]:
    attempts = int((previous or {}).get("attemptCount", 0))
    return {
        "runId": run_id,
        "attemptCount": attempts if error is None else attempts + 1,
        "synchronized": error is None,
        "lastError": None if error is None else safe_sync_error(error),
        "updatedAtUtc": utc_now(),
        "nextAttemptAtUtc": None,
    }


def apply_plan(plan: Plan, mongo: MongoWriter, *, upload_all: bool = False) -> Summary:
    """Delete, write and upload what the plan decided.

    Only pending runs are uploaded unless ``upload_all`` re-uploads every
    completed aggregate, for example after documents were removed in MongoDB.
    """
    summary = plan.summary
    for path in sorted(plan.deletions):
        try:
            path.unlink(missing_ok=True)
            summary.deleted_files += 1
        except OSError as exc:
            summary.operation_failed += 1
            summary.actions.append(f"ERROR deleting {path.name}: {exc}")

    uploads = sorted(plan.aggregates) if upload_all else plan.pending_uploads
    for path, document in sorted(plan.writes.items(), key=lambda item: str(item[0])):
        try:
            atomic_json(path, document)
        except OSError as exc:
            summary.operation_failed += 1
            summary.actions.append(f"ERROR writing {path.name}: {exc}")
            uploads = [run_id for run_id in uploads if plan.aggregate_path(run_id) != path]

    for run_id in uploads:
        sync_path = plan.results_dir / f"run-{run_id}.sync.json"
        previous = plan.sync_documents.get(run_id)
        try:
            mongo.upsert(plan.aggregates[run_id])
            atomic_json(sync_path, _sync_record(run_id, previous, None))
            summary.uploaded += 1
        except Exception as exc:
            summary.upload_failed += 1
            summary.actions.append(f"ERROR uploading run {run_id}: {safe_sync_error(exc)}")
            try:
                atomic_json(sync_path, _sync_record(run_id, previous, exc))
            except OSError as write_error:
                summary.operation_failed += 1
                summary.actions.append(f"ERROR writing {sync_path.name}: {write_error}")
    return summary


def connect_mongo(factory: Callable[[], Any] = configured_mongo) -> Any:
    """Return a pinged MongoDB writer, or raise before any local change."""
    mongo = factory()
    if mongo is None:
        raise RuntimeError("MONGODB_URI is not configured.")
    try:
        client = getattr(mongo, "client", None)
        if client is None:
            raise RuntimeError("configured MongoDB client is unavailable")
        client.admin.command("ping")
        mongo.ensure_indexes()
    except Exception as exc:
        raise RuntimeError(safe_sync_error(exc)) from exc
    return mongo


# --- Re-evaluation ---------------------------------------------------------


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


def sales_version_changes(session: Mapping[str, Any]) -> dict[str, tuple[Any, str]]:
    """Recorded versus current versions for an OpenRouter sales session."""
    if session.get("pipelineMode") != "openrouter":
        return {}
    return {
        name: (session.get(name), version)
        for name, version in SALES_VERSIONS.items()
        if session.get(name) != version
    }


def sales_needs_review(session: Mapping[str, Any]) -> bool:
    return session.get("pipelineMode") == "openrouter" and (
        session.get("assessmentStatus") == "needs-review" or bool(session.get("assessmentReviewItems"))
    )


def _stored_turn_audio(recordings_dir: Path, session_id: str, turn_id: str) -> SalesAudio:
    if SESSION_ID.fullmatch(turn_id) is None:
        raise RuntimeError(f"Turn ID '{turn_id}' has an invalid format.")
    path = recordings_dir.resolve() / f"sales-session-{session_id}-turn-{turn_id}.wav"
    if not path.is_file():
        raise FileNotFoundError(f"No stored audio for sales turn {turn_id} at '{path}'.")
    return SalesAudio(
        mimeType="audio/wav", encoding="pcm_s16le", sampleRateHz=16000, channels=1,
        dataBase64=base64.b64encode(path.read_bytes()).decode("ascii"),
    )


async def _rerun_sales_session(
    session: Mapping[str, Any],
    part1_record: Mapping[str, Any],
    recordings_dir: Path,
    pipeline: SalesPipeline,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay every accepted turn's stored audio through the live turn pipeline.

    A fresh session in a temporary store runs STT, Jev, Luna and the writer
    with the current versions, then sales_pipeline.complete. The recorded
    session is never modified.
    """
    if get_settings().pipeline_mode != "openrouter":
        raise RuntimeError("PIPELINE_MODE must be openrouter to re-run a sales session.")
    if pipeline.transcriber is None:
        raise RuntimeError("A speech transcriber is required to re-run the sales pipeline.")
    session_id = str(session["sessionId"])
    attempt_id = str(part1_record["attemptId"])
    stored_turns = [
        turn for turn in session.get("turns", [])
        if isinstance(turn, Mapping) and turn.get("accepted") and turn.get("turnId")
    ]
    audio = {str(turn["turnId"]): _stored_turn_audio(recordings_dir, session_id, str(turn["turnId"]))
             for turn in stored_turns}
    identity = session.get("customerIdentity") or {}

    with tempfile.TemporaryDirectory(prefix="sales-rerun-") as temporary:
        store = ReturningSessionStore(Path(temporary))
        atomic_json(Path(temporary) / f"sales-persuasion-{attempt_id}.json", dict(part1_record))
        await store.create_or_resume(
            session_id, attempt_id, session.get("runId"), session.get("participantName"),
            customer_id=identity.get("customerId", "Lan"), continues_part1_customer=True,
        )
        replayed = 0
        for turn in stored_turns:
            turn_id = str(turn["turnId"])
            capture = turn.get("captureEvidence")
            request = ReturningTurnRequest(
                turnId=turn_id, audio=audio[turn_id],
                capture=CaptureEvidence(**capture) if isinstance(capture, Mapping) else None,
            )
            for attempt in range(1, TURN_ATTEMPTS + 1):
                result = await process_turn(
                    session_id, request, store=store, transcriber=pipeline.transcriber,
                    classifier=pipeline.classifier, writer=pipeline.writer, arbitrator=pipeline.arbitrator,
                )
                # Timeouts keep the checkpoint; resubmitting resumes that stage.
                if not result.get("retryTurn") or attempt == TURN_ATTEMPTS:
                    break
            if result.get("status") != "accepted":
                code = (result.get("error") or {}).get("code", result.get("status"))
                raise RuntimeError(f"Sales turn {turn_id} could not be re-run: {code}.")
            replayed += 1
            if result.get("conversationComplete"):
                break

        await sales_pipeline_complete(
            session_id,
            CompletionRequest(completionId=f"repair-{uuid4().hex}",
                              reason=str(session.get("completionReason") or "natural")[:32]),
            store=store,
        )
        state = await store.get(session_id)
        if state is None:
            raise RuntimeError("The re-run sales session disappeared from its temporary store.")
    return state, {
        "turnsReplayed": replayed,
        "turnsSkippedAfterEnding": len(stored_turns) - replayed,
    }


async def _reevaluate_lawyer(session: Mapping[str, Any], service: LLMService) -> dict[str, Any]:
    transcript = session.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise RuntimeError("The lawyer recording session has no transcript to evaluate.")
    assessment = await LLMLawyerAssessor(service).assess(session, transcript)
    evaluated = dict(session)
    evaluated.update(assessmentStatus="completed", **assessment_fields(session, assessment),
                     updatedAtUtc=utc_now())
    lawyer_result = project_lawyer_record(evaluated)
    lawyer_result["roundId"] = session.get("roundId")
    return {"lawyer": lawyer_result}


async def _reevaluate_sales_part1(part1_record: Mapping[str, Any], service: LLMService) -> dict[str, Any]:
    transcript = part1_record.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise RuntimeError("The sales part 1 recording has no transcript to evaluate.")
    assessment = await LLMSalesAssessor(service).assess(part1_record, transcript)
    evaluated = dict(part1_record)
    evaluated.update(score=assessment.score, feedbackVi=assessment.feedback_vi,
                     assessmentStatus="completed", updatedAtUtc=utc_now())
    return project_sales_part1(evaluated)


async def _openrouter_part2(
    session: Mapping[str, Any],
    part1_record: Mapping[str, Any],
    recordings_dir: Path,
    pipeline: SalesPipeline | None,
) -> dict[str, Any]:
    """Score an OpenRouter session from its evidence ledger, never by the
    legacy whole-dialogue analyzer. A pipeline re-runs a needs-review session."""
    if pipeline is not None and sales_needs_review(session):
        evaluated, replay = await _rerun_sales_session(session, part1_record, recordings_dir, pipeline)
        method = "full_pipeline_rerun"
    else:
        evaluated, replay = deepcopy(dict(session)), {}
        method = "ledger_replay"
    unresolved_review = 0
    if evaluated.get("completionStatus") != "completed":
        # Still under review: uncertain labels earn no credit.
        unresolved_review = len(evaluated.get("assessmentReviewItems") or [])
        apply_completed_outcome(evaluated, completion_id=f"repair-{uuid4().hex}",
                                reason=session.get("completionReason") or "manual_repair")
        evaluated["updatedAtUtc"] = utc_now()
    part2 = project_sales_part2(evaluated)
    part2["repair"] = {
        "method": method,
        "recordedVersions": {name: session.get(name) for name in SALES_VERSIONS},
        "scoringRubricVersion": evaluated.get("rubricVersion")
        if method == "full_pipeline_rerun" else SALES_VERSIONS["rubricVersion"],
        "unresolvedReviewItems": unresolved_review,
        "repairedAtUtc": utc_now(),
        **replay,
    }
    return part2


async def _legacy_part2(session: Mapping[str, Any], service: LLMService) -> dict[str, Any]:
    analysis = await LLMSalesAnalyzer(service).analyze(session)
    evaluated = dict(session)
    evaluated.update(
        status="finished", completionStatus="completed", assessmentStatus="completed",
        completionReason=session.get("completionReason") or "manual_repair",
        **legacy_completed_outcome(session, analysis),
        acceptedTurnCount=int(session.get("acceptedTurnCount", len(session["turns"]))),
        updatedAtUtc=utc_now(),
    )
    return project_sales_part2(evaluated)


async def _reevaluate_sale(
    session: Mapping[str, Any],
    draft: Mapping[str, Any],
    recordings_dir: Path,
    service: LLMService,
    pipeline: SalesPipeline | None,
) -> dict[str, Any]:
    part1_record = _sales_part1_recording(recordings_dir, session, draft)
    part1 = await _reevaluate_sales_part1(part1_record, service)
    turns = session.get("turns")
    if not isinstance(turns, list) or not turns:
        raise RuntimeError("The sales session has no turn transcripts to evaluate.")
    if any(not isinstance(turn, Mapping) or not str(turn.get("transcript", "")).strip() for turn in turns):
        raise RuntimeError("Every sales turn must contain a transcript.")
    if session.get("pipelineMode") == "openrouter":
        part2 = await _openrouter_part2(session, part1_record, recordings_dir, pipeline)
    else:
        part2 = await _legacy_part2(session, service)
    return {"part1": part1, "turns": part2.pop("turns"), "part2": part2}


async def reevaluate_draft(
    draft: Mapping[str, Any],
    recordings_dir: Path,
    session_id: str,
    service: LLMService,
    *,
    pipeline: SalesPipeline | None = None,
) -> dict[str, Any]:
    """Rebuild one sale or lawyer draft from its recordings and fresh scores.

    Pass ``pipeline`` to re-run a needs-review OpenRouter sales session from
    its stored audio instead of replaying the recorded evidence ledger.
    """
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
        results = await _reevaluate_lawyer(session, service)
    else:
        results = await _reevaluate_sale(session, repaired, recordings_dir, service, pipeline)
    data.update(results)

    prior_required = repaired.get("requiredFields", [])
    if not isinstance(prior_required, list):
        prior_required = []
    required = {"lawyer"} if game_id == "lawyer" else {"part1", "part2"}
    repaired["requiredFields"] = sorted(set(prior_required) | required)
    repaired["completionRequested"] = True
    repaired["updatedAtUtc"] = utc_now()
    return repaired


# --- Interactive CLI -------------------------------------------------------


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


def _choose(prompt: str, input_fn: Callable[[str], str]) -> bool:
    return input_fn(prompt).strip().casefold() in YES


def _select_draft(
    candidates: list[tuple[Path, dict[str, Any]]],
    input_fn: Callable[[str], str],
) -> tuple[Path, dict[str, Any]] | None:
    print("Drafts available for transcript re-evaluation:")
    for index, (path, draft) in enumerate(candidates, start=1):
        print(
            f"  {index}. {path.name} [{_draft_display_date(path, draft)}] "
            f"({draft['gameId']}, {draft.get('participantName', 'unknown')})"
        )
    raw_selection = input_fn("Select a draft number, or press Enter to cancel: ").strip()
    if not raw_selection:
        return None
    try:
        selection = int(raw_selection)
        if selection < 1:
            raise IndexError(selection)
        return candidates[selection - 1]
    except (ValueError, IndexError):
        print("Invalid draft selection.", file=sys.stderr)
        return None


def _ask_rerun(session: Mapping[str, Any], input_fn: Callable[[str], str]) -> bool:
    if not sales_needs_review(session):
        return False
    print("This sales session is marked needs-review.")
    for name, (recorded, current) in sales_version_changes(session).items():
        print(f"  {name}: {recorded} -> {current}")
    rerun = _choose(
        "Re-run every turn from its stored audio (STT, Jev, Luna, writer) "
        "with the current pipeline? [Y/n]: ",
        input_fn,
    )
    if not rerun:
        print("Keeping the recorded turns; the stored evidence ledger will be replayed.")
    return rerun


def _store_reevaluation(draft_path: Path, repaired: dict[str, Any]) -> None:
    """Write the re-evaluated draft and, when complete, replace its aggregate.

    An aggregate already on disk would otherwise win over the new draft. The
    sidecar is marked unsynchronized so the next apply uploads the new result.
    """
    run_id = str(repaired["runId"])
    if _draft_problem(repaired) is None:
        repaired["status"] = "finalized"
        atomic_json(draft_path.with_name(f"run-{run_id}.json"), _aggregate_from_draft(repaired))
        sync_path = draft_path.with_name(f"run-{run_id}.sync.json")
        try:
            previous = _read_json_object(sync_path, "sync sidecar") if sync_path.exists() else None
        except RuntimeError:
            previous = None
        atomic_json(sync_path, {**_sync_record(run_id, previous, None), "synchronized": False})
    atomic_json(draft_path, repaired)


def _report_reevaluation(draft_path: Path, repaired: Mapping[str, Any]) -> None:
    print(f"Re-evaluated and updated {draft_path.name}.")
    part2 = repaired.get("data", {}).get("part2")
    repair = part2.get("repair") if isinstance(part2, Mapping) else None
    if isinstance(repair, Mapping):
        print(
            f"  sales part 2: {repair['method']}, rubric {repair['scoringRubricVersion']}, "
            f"score {part2.get('score')} ({part2.get('customerRating')})"
        )
        if repair.get("turnsSkippedAfterEnding"):
            print(f"  {repair['turnsSkippedAfterEnding']} later turn(s) skipped: "
                  "the current rules end the dialogue earlier.")
        if repair.get("unresolvedReviewItems"):
            print(f"  WARNING {repair['unresolvedReviewItems']} review item(s) stayed uncertain "
                  "and earned no credit.")
    problem = _draft_problem(repaired)
    if problem:
        print(f"WARNING {draft_path.name} is still not ready to upload: {problem}.", file=sys.stderr)


def _interactive_reevaluation(
    results_dir: Path,
    recordings_dir: Path,
    *,
    input_fn: Callable[[str], str] = input,
    transcriber_factory: Callable[[], Any] = SherpaOnnxTranscriber,
) -> bool:
    candidates = _reevaluation_candidates(results_dir)
    if not candidates:
        print("No sale or lawyer draft files were found for re-evaluation.")
        return False
    selected = _select_draft(candidates, input_fn)
    if selected is None:
        return False
    draft_path, draft = selected

    session_id = input_fn("Enter the recording sessionId: ").strip()
    try:
        session_path = _recording_path(recordings_dir, str(draft["gameId"]), session_id)
        session = _read_json_object(session_path, f"{draft['gameId']} recording session")
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"Re-evaluation failed: {exc}", file=sys.stderr)
        return False
    print(f"Found recording session: {session_path}")
    rerun = draft["gameId"] == "sale" and _ask_rerun(session, input_fn)

    manager = AIServerManager()
    service = create_llm_service()
    # PIPELINE_MODE=openrouter has no local server to start.
    local = service.provider != "openrouter"
    reachable = _llm_endpoint_reachable(service)
    try:
        prompt = (
            "The LLM service is reachable. Start re-evaluation now? [Y/n]: "
            if reachable
            else "The LLM service is not reachable. Start it and continue? [Y/n]: "
            if local
            else "OpenRouter is not reachable. Continue anyway? [Y/n]: "
        )
        if not _choose(prompt, input_fn):
            asyncio.run(service.close())
            print("Re-evaluation cancelled.")
            return False

        async def run() -> dict[str, Any]:
            pipeline = SalesPipeline(transcriber=transcriber_factory()) if rerun else None
            try:
                if not reachable and local:
                    await manager.start_detached(
                        "hybrid" if service.settings.llm_use_amd_hybrid else "llama"
                    )
                if pipeline is not None and not await pipeline.transcriber.initialize():
                    raise RuntimeError(
                        "Speech recognition could not start: "
                        f"{getattr(pipeline.transcriber, 'last_error', None) or 'unknown error'}"
                    )
                return await reevaluate_draft(draft, recordings_dir, session_id, service, pipeline=pipeline)
            finally:
                if pipeline is not None:
                    await pipeline.transcriber.close()
                await service.close()

        repaired = asyncio.run(run())
        _store_reevaluation(draft_path, repaired)
        _report_reevaluation(draft_path, repaired)
        return True
    except Exception as exc:
        print(f"Re-evaluation failed: {exc}", file=sys.stderr)
        return False


def _print_plan(plan: Plan) -> None:
    summary = plan.summary
    print(f"\nSimulation results in {plan.results_dir}")
    for action in summary.actions:
        print(f"  {action}")
    print(
        f"Scanned {summary.scanned_files} files: {len(plan.writes)} to write, "
        f"{len(plan.deletions)} to delete, {summary.upload_ready} completed runs, "
        f"{len(plan.pending_uploads)} pending upload."
    )


def _print_summary(summary: Summary) -> None:
    for action in summary.actions:
        if action.startswith("ERROR"):
            print(f"  {action}")
    print(
        "Applied: "
        f"repaired={summary.repaired_files}, finalized={summary.finalized_runs}, "
        f"test_runs={summary.test_runs}, invalid={summary.invalid_files}, "
        f"deleted={summary.deleted_files}, uploaded={summary.uploaded}, "
        f"upload_failed={summary.upload_failed}, operation_failed={summary.operation_failed}"
    )


def run_interactive(
    results_dir: Path,
    recordings_dir: Path,
    *,
    input_fn: Callable[[str], str] = input,
    mongo_factory: Callable[[], Any] = configured_mongo,
    transcriber_factory: Callable[[], Any] = SherpaOnnxTranscriber,
) -> int:
    """Scan, show the plan and run the chosen action until the user quits.

    Returns 1 when the last apply failed, otherwise 0.
    """
    exit_code = 0
    while True:
        plan = plan_maintenance(results_dir)
        _print_plan(plan)
        pending = len(plan.pending_uploads)
        options: list[tuple[str, str]] = []
        if plan.has_file_changes or pending:
            options.append(("apply", f"Apply repairs and upload {pending} pending run(s)"))
        if plan.aggregates:
            options.append(("upload_all", f"Apply repairs and re-upload all {len(plan.aggregates)} completed run(s)"))
        if _reevaluation_candidates(results_dir):
            options.append(("reevaluate", "Re-evaluate a sale or lawyer draft"))
        print("Choose an action:")
        for index, (_, label) in enumerate(options, start=1):
            print(f"  {index}. {label}")
        print("  q. Quit")
        raw = input_fn("> ").strip().casefold()
        if raw in {"q", "quit", ""}:
            return exit_code
        try:
            action = options[int(raw) - 1][0] if int(raw) >= 1 else None
        except (ValueError, IndexError):
            action = None
        if action is None:
            print("Invalid choice.", file=sys.stderr)
            continue
        if action == "reevaluate":
            _interactive_reevaluation(results_dir, recordings_dir, input_fn=input_fn,
                                      transcriber_factory=transcriber_factory)
            continue
        if not _choose("The backend must be stopped while files change. Continue? [Y/n]: ", input_fn):
            continue
        try:
            mongo = connect_mongo(mongo_factory)
        except RuntimeError as exc:
            print(f"Cannot apply before MongoDB is ready: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        summary = apply_plan(plan, mongo, upload_all=action == "upload_all")
        _print_summary(summary)
        exit_code = 1 if summary.failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        type=Path,
        help="simulation-results directory; defaults to the configured RECORDINGS_DIR",
    )
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        help="recordings directory used for re-evaluation; defaults to the parent of the results directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, **interactive: Any) -> int:
    args = parse_args(argv)
    results_dir = args.results_dir.resolve() if args.results_dir else RunResultStore().directory
    recordings_dir = args.recordings_dir.resolve() if args.recordings_dir else results_dir.parent
    return run_interactive(results_dir, recordings_dir, **interactive)


if __name__ == "__main__":
    raise SystemExit(main())
