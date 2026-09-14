"""Participant-linked simulation run results.

This module deliberately keeps the research aggregate separate from the
game-specific recording stores.  The aggregate is a small, append-by-key
draft which is atomically replaced after every accepted fragment.  A
sidecar carries synchronization state so the JSON sent to MongoDB remains
the exact local finalized document.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any, Mapping, Protocol
from uuid import uuid4

from app.config import BACKEND_ROOT, get_settings

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
GAME_IDS = frozenset({"clinic", "doctor", "lawyer", "sale"})
FRAGMENT_TYPES = frozenset({
    "clinic.patient_resolved", "clinic.round_finished",
    "doctor.case_submitted", "doctor.round_finished",
    "lawyer.defense_completed", "lawyer.defense_recording",
    "sales.persuasion_recording", "sales.part1_recording_uploaded",
    "sales.part2.turn_accepted", "sales.part2.completed",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_sync_error(error: Exception) -> str:
    message = re.sub(r"(mongodb(?:\+srv)?://[^:/\s]+:)[^@\s]+@", r"\1<redacted>@", str(error), flags=re.IGNORECASE)
    return message[:500]


def normalize_participant_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("participant name must be text")
    value = unicodedata.normalize("NFC", value).strip()
    value = " ".join(value.split())
    if not 1 <= len(value) <= 100:
        raise ValueError("participant name must contain 1 through 100 characters")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


_FORBIDDEN_AGGREGATE_KEYS = {
    "audio", "audiobytes", "audiofile", "audiopath", "wav", "wavpath",
    "machinepath", "localpath", "requesthash", "rawerror", "exception",
    "model", "provider", "rubric", "schemaversion", "telemetry",
}


def _approved_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        def allowed(key: Any) -> bool:
            normalized = str(key).replace("_", "").lower()
            return normalized not in _FORBIDDEN_AGGREGATE_KEYS and not any(
                token in normalized for token in ("modelmetadata", "providermetadata", "rubricmetadata", "localmachine")
            )
        return {str(key): _approved_data(item) for key, item in value.items() if allowed(key)}
    if isinstance(value, list):
        return [_approved_data(item) for item in value]
    return value


def _payload_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    return _approved_data(dict(payload))


def translate_unity_result_fragment(fragment: Mapping[str, Any]) -> dict[str, Any]:
    """Translate Unity ``ResultFragmentDto`` into the backend store contract."""
    required = ("fragmentId", "runId", "participantName", "participantSessionId",
                "gameId", "fragmentType", "occurredAtUtc", "payload")
    missing = [key for key in required if key not in fragment]
    if missing:
        raise ValueError("missing ResultFragmentDto fields: " + ", ".join(missing))
    fragment_type = fragment.get("fragmentType")
    game_id = fragment.get("gameId")
    if game_id not in GAME_IDS or fragment_type not in FRAGMENT_TYPES:
        raise ValueError("unknown result fragment type")
    payload = _payload_dict(fragment.get("payload"))
    run_id = fragment["runId"]
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise ValueError("runId has an invalid format")
    if not isinstance(fragment["fragmentId"], str) or not fragment["fragmentId"].strip():
        raise ValueError("fragmentId is required")
    if not isinstance(fragment["participantName"], str) or not fragment["participantName"].strip():
        raise ValueError("participantName is required")
    if not isinstance(fragment["participantSessionId"], str) or not fragment["participantSessionId"].strip():
        raise ValueError("participantSessionId is required")
    if fragment_type.startswith("clinic.") and game_id != "clinic":
        raise ValueError("clinic fragment has the wrong gameId")
    if fragment_type.startswith("doctor.") and game_id != "doctor":
        raise ValueError("doctor fragment has the wrong gameId")
    if fragment_type.startswith("lawyer.") and game_id != "lawyer":
        raise ValueError("lawyer fragment has the wrong gameId")
    if fragment_type.startswith("sales.") and game_id != "sale":
        raise ValueError("sales fragment has the wrong gameId")

    translated: dict[str, Any] = {
        "runId": fragment["runId"], "gameId": game_id,
        "fragmentId": fragment["fragmentId"], "kind": fragment_type,
        "occurredAtUtc": fragment["occurredAtUtc"],
        "participantName": fragment["participantName"],
        "participantSessionId": fragment["participantSessionId"],
        "data": {},
    }
    if fragment_type == "clinic.patient_resolved":
        translated["data"] = {"patientResults": [payload]}
    elif fragment_type == "clinic.round_finished":
        translated["data"] = {"round": payload, "roundCompletedAtUtc": payload.get("roundCompletedAtUtc") or fragment["occurredAtUtc"]}
        translated["requiredFields"] = ["patientResults", "round"]
        translated["complete"] = True
    elif fragment_type == "doctor.case_submitted":
        translated["data"] = {"cases": [payload]}
    elif fragment_type == "doctor.round_finished":
        translated["data"] = {"round": payload, "roundCompletedAtUtc": payload.get("roundCompletedAtUtc") or fragment["occurredAtUtc"]}
        translated["requiredFields"] = ["cases", "round"]
        translated["complete"] = True
    elif fragment_type in {"lawyer.defense_completed", "lawyer.defense_recording"}:
        translated["data"] = {"lawyer": payload}
        if fragment_type == "lawyer.defense_completed":
            translated["requiredFields"] = ["lawyer"]
            translated["complete"] = True
    elif fragment_type in {"sales.persuasion_recording", "sales.part1_recording_uploaded"}:
        translated["data"] = {"part1": payload}
    elif fragment_type == "sales.part2.turn_accepted":
        translated["data"] = {"turns": [payload]}
    elif fragment_type == "sales.part2.completed":
        translated["data"] = {"part2": payload}
        translated["requiredFields"] = ["part1", "part2"]
        translated["complete"] = True
    return translated


def _merge_keyed(current: Any, incoming: Any, key: str) -> list[Any]:
    values = list(current) if isinstance(current, list) else []
    by_key = {item.get(key): index for index, item in enumerate(values)
              if isinstance(item, Mapping) and item.get(key) is not None}
    for item in incoming if isinstance(incoming, list) else [incoming]:
        if not isinstance(item, Mapping):
            continue
        item_key = item.get(key)
        if item_key in by_key:
            values[by_key[item_key]] = dict(item)
        else:
            by_key[item_key] = len(values)
            values.append(dict(item))
    return values


def merge_result_data(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key, value in incoming.items():
        if key == "patientResults":
            result[key] = _merge_keyed(result.get(key), value, "patientId")
        elif key == "cases":
            result[key] = _merge_keyed(result.get(key), value, "caseId")
        elif key == "turns":
            result[key] = _merge_keyed(result.get(key), value, "turnId")
        else:
            result[key] = value
    return result


@dataclass(frozen=True)
class Participant:
    name: str
    session_id: str


class ParticipantManager:
    """In-memory participant ownership and run-start gate."""

    def __init__(self) -> None:
        self.active: Participant | None = None
        self.activity: str = "idle"

    def assign(self, name: str) -> tuple[Participant, bool]:
        normalized = normalize_participant_name(name)
        if self.active is not None and self.active.name == normalized:
            return self.active, False
        if self.activity in {"gameplay", "assessment"}:
            raise RuntimeError("participant cannot change during active gameplay or assessment")
        self.active = Participant(normalized, uuid4().hex)
        return self.active, True

    def clear(self, *, force: bool = False) -> Participant | None:
        if self.activity in {"gameplay", "assessment"} and not force:
            raise RuntimeError("participant cannot be cleared during active gameplay or assessment")
        old, self.active = self.active, None
        return old

    def snapshot(self) -> dict[str, str] | None:
        if self.active is None:
            return None
        return {"participantName": self.active.name, "participantSessionId": self.active.session_id}

    def require(self) -> Participant:
        if self.active is None:
            raise RuntimeError("an active participant is required before starting a game")
        return self.active


class MongoBoundary(Protocol):
    def upsert(self, aggregate: Mapping[str, Any]) -> None: ...
    def ensure_indexes(self) -> None: ...


class PyMongoBoundary:
    def __init__(self, uri: str, database: str, collection: str) -> None:
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - deployment-only branch
            raise RuntimeError("pymongo is required when MONGODB_URI is configured") from exc
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.collection = self.client[database][collection]

    def ensure_indexes(self) -> None:
        self.collection.create_index("participantName")
        self.collection.create_index("participantSessionId")
        self.collection.create_index("gameId")
        self.collection.create_index("completedAtUtc")

    def upsert(self, aggregate: Mapping[str, Any]) -> None:
        self.collection.replace_one({"_id": aggregate["runId"]},
                                    {"_id": aggregate["runId"], **dict(aggregate)},
                                    upsert=True)


def configured_mongo() -> MongoBoundary | None:
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        return None
    database = os.environ.get("MONGODB_DATABASE", "desmap").strip() or "desmap"
    collection = os.environ.get("MONGODB_RESULTS_COLLECTION", "game_results").strip() or "game_results"
    return PyMongoBoundary(uri, database, collection)


def project_lawyer_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only approved Lawyer result fields out of the processing record."""
    context = record.get("assessmentContext", {}) if isinstance(record.get("assessmentContext"), Mapping) else {}
    clues = []
    for item in context.get("orderedEvidence", []):
        if not isinstance(item, Mapping) or item.get("strength") not in {"strong", "weak"}:
            continue
        clues.append({k: item[k] for k in ("evidenceId", "title", "strength") if k in item})
    criteria = record.get("criteria") or {}
    return {"caseId": record.get("caseId"), "interviewRestartCount": record.get("interviewRestartCount"),
            "finalClueSet": clues, "completedEvidenceLinks": record.get("completedEvidenceLinks", []),
            "transcript": record.get("transcript") or "", "criterionScores": {
                "evidenceUse": criteria.get("evidenceUse", criteria.get("evidence_use", 0)),
                "logicalConnections": criteria.get("logicalConnections", criteria.get("logical_connections", 0)),
                "conclusionFidelity": criteria.get("conclusionFidelity", criteria.get("conclusion_fidelity", 0)),
                "clarityAndPersuasiveness": criteria.get("clarityAndPersuasiveness", criteria.get("clarity_and_persuasiveness", 0)),
            }, "rawScore": record.get("rawScore", 0), "restartPenaltyPercent": record.get("restartPenaltyPercent", 0),
            "finalScore": record.get("finalScore", 0), "feedbackVi": record.get("feedbackVi") or "",
            "recordingAtUtc": record.get("createdAtUtc"), "assessmentCompletedAtUtc": record.get("updatedAtUtc"),
            "completionStatus": record.get("assessmentStatus")}


def project_sales_part1(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in ("attemptId", "selectedShoeId", "bestFitShoeId", "transcript", "score", "feedbackVi", "createdAtUtc", "updatedAtUtc")}


def project_sales_part2(session: Mapping[str, Any]) -> dict[str, Any]:
    turns = []
    for turn in session.get("turns", []):
        if isinstance(turn, Mapping):
            turns.append({k: turn.get(k) for k in ("turnId", "transcript", "customerText", "activeObjective", "objectiveActiveDuringTurn", "createdAtUtc", "timestampUtc")})
    return {"turns": turns, "trustState": session.get("trustState"),
            "emotionalHandling": session.get("emotionalHandling", False),
            "causeIdentification": session.get("causeIdentification", False),
            "solutionSuitability": session.get("solutionSuitability", False),
            "trustRebuilding": session.get("trustRebuilding", False),
            "completionReason": session.get("completionReason"),
            "acceptedTurnCount": session.get("acceptedTurnCount", 0),
            "silenceCount": session.get("silenceCount", 0),
            "completedAtUtc": session.get("updatedAtUtc")}


class RunResultStore:
    """Atomic local drafts and finalized results with idempotent fragments."""

    def __init__(self, directory: Path | None = None, *, mongo: MongoBoundary | None = None,
                 clock=utc_now) -> None:
        base = Path(directory) if directory is not None else Path(get_settings().recordings_dir)
        if not base.is_absolute():
            base = BACKEND_ROOT / base
        self.directory = (base / "simulation-results").resolve()
        self.mongo = mongo
        self.clock = clock
        self._lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()

    def _path(self, run_id: str, suffix: str) -> Path:
        if RUN_ID.fullmatch(run_id) is None:
            raise ValueError("runId has an invalid format")
        return self.directory / f"run-{run_id}{suffix}"

    def _read(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("simulation result record is invalid")
        return value

    async def begin(self, run_id: str, game_id: str, participant: Participant,
                    started_at_utc: str | None = None) -> dict[str, Any]:
        if RUN_ID.fullmatch(run_id) is None or game_id not in GAME_IDS:
            raise ValueError("invalid runId or gameId")
        async with self._lock:
            existing = self._read(self._path(run_id, ".draft.json")) or self._read(self._path(run_id, ".json"))
            if existing is not None:
                if existing.get("status") == "aborted":
                    raise RuntimeError("run is aborted")
                return existing
            draft = {"runId": run_id, "participantName": participant.name,
                     "participantSessionId": participant.session_id, "gameId": game_id,
                     "status": "draft", "startedAtUtc": started_at_utc or self.clock(),
                     "data": {}, "fragments": {}, "updatedAtUtc": self.clock()}
            _atomic_json(self._path(run_id, ".draft.json"), draft)
            return draft

    async def accept_fragment(self, fragment: Mapping[str, Any], *, participant: Participant | None = None) -> dict[str, Any]:
        if not isinstance(fragment, Mapping):
            raise ValueError("fragment must be an object")
        run_id = fragment.get("runId")
        game_id = fragment.get("gameId")
        fragment_id = fragment.get("fragmentId", fragment.get("eventId"))
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise ValueError("runId has an invalid format")
        if game_id not in GAME_IDS:
            raise ValueError("gameId is invalid")
        if not isinstance(fragment_id, str) or not fragment_id or len(fragment_id) > 128:
            raise ValueError("fragmentId is required")
        async with self._lock:
            draft = self._read(self._path(run_id, ".draft.json"))
            if draft is None:
                if participant is None:
                    raise RuntimeError("run has no participant snapshot")
                draft = {"runId": run_id, "participantName": participant.name,
                         "participantSessionId": participant.session_id, "gameId": game_id,
                         "status": "draft", "startedAtUtc": fragment.get("startedAtUtc", self.clock()),
                         "data": {}, "fragments": {}, "updatedAtUtc": self.clock()}
            elif draft.get("status") == "aborted":
                raise RuntimeError("run is aborted")
            elif draft.get("status") == "finalized":
                return self._read(self._path(run_id, ".json")) or draft
            if draft.get("gameId") != game_id:
                raise ValueError("runId cannot change gameId")
            if fragment.get("participantName") is not None and fragment.get("participantName") != draft.get("participantName"):
                raise ValueError("participantName does not match the run snapshot")
            if fragment.get("participantSessionId") is not None and fragment.get("participantSessionId") != draft.get("participantSessionId"):
                raise ValueError("participantSessionId does not match the run snapshot")
            if fragment_id in draft.setdefault("fragments", {}):
                return draft
            data = fragment.get("data", {})
            if not isinstance(data, Mapping):
                raise ValueError("data must be an object")
            # Fragments are keyed by stable IDs.  Later retries replace nothing;
            # distinct fragments merge shallowly while preserving prior fields.
            draft["data"] = merge_result_data(draft.get("data", {}), _approved_data(dict(data)))
            required = fragment.get("requiredFields", [])
            if required is not None:
                if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                    raise ValueError("requiredFields must be a list of field names")
                draft["requiredFields"] = sorted(set(draft.get("requiredFields", [])) | set(required))
            draft["fragments"][fragment_id] = {"kind": fragment.get("kind", "result"),
                                                  "acceptedAtUtc": self.clock()}
            if fragment.get("complete") is True:
                draft["completionRequested"] = True
            draft["updatedAtUtc"] = self.clock()
            _atomic_json(self._path(run_id, ".draft.json"), draft)
            if draft.get("completionRequested") and self._ready_to_finalize(draft):
                return await self._finalize_unlocked(draft)
            return draft

    @staticmethod
    def _ready_to_finalize(draft: Mapping[str, Any]) -> bool:
        data = draft.get("data", {})
        required = draft.get("requiredFields", [])
        if not isinstance(data, Mapping):
            return False
        # A producer may declare required result/transcript fields in the
        # fragment contract.  Missing values intentionally keep the draft
        # local instead of publishing a partial aggregate.
        if not all(field in data and data[field] is not None for field in required):
            return False
        if any(field in {"patientResults", "cases", "turns"} and not data[field] for field in required):
            return False
        game_id = draft.get("gameId")
        if game_id == "clinic" and "patientResults" in required:
            fields = {"patientId", "sicknessName", "severity", "bedIndex", "resolution",
                      "actionableAtUtc", "resolvedAtUtc", "resolutionDurationSeconds", "scoreDelta", "scoreTotal"}
            if any(not fields.issubset(set(item)) for item in data["patientResults"] if isinstance(item, Mapping)):
                return False
        if game_id == "doctor" and "cases" in required:
            fields = {"caseId", "selectedQuestionsJson", "notesJson", "scoreDelta", "scoreTotal", "caseSubmittedAtUtc"}
            if any(not fields.issubset(set(item)) for item in data["cases"] if isinstance(item, Mapping)):
                return False
        if game_id == "lawyer" and "lawyer" in required:
            lawyer = data.get("lawyer")
            if not isinstance(lawyer, Mapping) or not lawyer.get("transcript") and lawyer.get("completionStatus") != "completed":
                return False
            if not isinstance(lawyer.get("criterionScores"), Mapping) or lawyer.get("rawScore") is None or lawyer.get("finalScore") is None:
                return False
        if game_id == "sale" and "part2" in required:
            part2 = data.get("part2")
            flags = {"emotionalHandling", "causeIdentification", "solutionSuitability", "trustRebuilding"}
            if not isinstance(part2, Mapping) or not part2.get("trustState") or not part2.get("completionReason") or not flags.issubset(set(part2)):
                return False
        return True

    async def finalize(self, run_id: str) -> dict[str, Any]:
        async with self._lock:
            draft = self._read(self._path(run_id, ".draft.json"))
            if draft is None:
                result = self._read(self._path(run_id, ".json"))
                if result is None:
                    raise KeyError("run not found")
                return result
            if draft.get("status") == "aborted":
                raise RuntimeError("run is aborted")
            if not self._ready_to_finalize(draft):
                raise RuntimeError("run is missing required result fragments")
            return await self._finalize_unlocked(draft)

    async def _finalize_unlocked(self, draft: dict[str, Any]) -> dict[str, Any]:
        result = {"_id": draft["runId"], "runId": draft["runId"],
                  "participantName": draft["participantName"],
                  "participantSessionId": draft["participantSessionId"],
                  "gameId": draft["gameId"], "status": "completed",
                  "startedAtUtc": draft["startedAtUtc"], "completedAtUtc": self.clock(),
                  "data": draft.get("data", {})}
        _atomic_json(self._path(draft["runId"], ".json"), result)
        draft["status"] = "finalized"
        _atomic_json(self._path(draft["runId"], ".draft.json"), draft)
        await self._schedule_sync_unlocked(result)
        return result

    async def _schedule_sync_unlocked(self, aggregate: Mapping[str, Any]) -> None:
        sidecar = self._read(self._path(aggregate["runId"], ".sync.json")) or {
            "runId": aggregate["runId"], "attemptCount": 0, "synchronized": False,
            "lastError": None, "updatedAtUtc": self.clock()}
        if sidecar.get("synchronized") or self.mongo is None:
            _atomic_json(self._path(aggregate["runId"], ".sync.json"), sidecar)
            return
        # The first automatic attempt happens immediately.  Delayed attempts
        # are explicitly driven by retry_due, avoiding an unbounded task loop.
        await self._try_sync_unlocked(aggregate, sidecar)

    async def _try_sync_unlocked(self, aggregate: Mapping[str, Any], sidecar: dict[str, Any]) -> bool:
        if self.mongo is None or sidecar.get("attemptCount", 0) >= 3:
            return bool(sidecar.get("synchronized"))
        try:
            await asyncio.to_thread(self.mongo.upsert, aggregate)
            sidecar.update(synchronized=True, lastError=None, updatedAtUtc=self.clock())
            ok = True
        except Exception as exc:  # sanitize all raw driver errors
            sidecar["attemptCount"] = int(sidecar.get("attemptCount", 0)) + 1
            delay = 5 if sidecar["attemptCount"] == 1 else 30 if sidecar["attemptCount"] == 2 else None
            sidecar.update(synchronized=False, lastError=_safe_sync_error(exc), updatedAtUtc=self.clock(),
                          nextAttemptAtUtc=(datetime.now(timezone.utc).timestamp() + delay if delay else None))
            ok = False
        _atomic_json(self._path(aggregate["runId"], ".sync.json"), sidecar)
        return ok

    async def retry_due(self) -> int:
        """Perform only scheduled automatic attempts; never loops indefinitely."""
        if self.mongo is None:
            return 0
        now = datetime.now(timezone.utc).timestamp()
        done = 0
        async with self._sync_lock:
            for sidecar_path in self.directory.glob("run-*.sync.json"):
                sidecar = self._read(sidecar_path)
                if not sidecar or sidecar.get("synchronized") or int(sidecar.get("attemptCount", 0)) >= 3:
                    continue
                due = sidecar.get("nextAttemptAtUtc")
                if (due is None and int(sidecar.get("attemptCount", 0)) > 0) or (due is not None and float(due) > now):
                    continue
                aggregate = self._read(self._path(sidecar["runId"], ".json"))
                if aggregate and await self._try_sync_unlocked(aggregate, sidecar):
                    done += 1
        return done

    async def sync_all(self) -> dict[str, int]:
        async with self._sync_lock:
            synchronized = failed = remaining = 0
            for path in sorted(self.directory.glob("run-*.json"), key=lambda p: p.stat().st_mtime):
                aggregate = self._read(path)
                if not aggregate or aggregate.get("status") != "completed":
                    continue
                sidecar = self._read(self._path(aggregate["runId"], ".sync.json")) or {"runId": aggregate["runId"], "attemptCount": 0}
                if sidecar.get("synchronized"):
                    synchronized += 1
                    continue
                if await self._try_sync_unlocked(aggregate, sidecar):
                    synchronized += 1
                else:
                    failed += 1
            remaining = sum(1 for p in self.directory.glob("run-*.sync.json")
                            if not (self._read(p) or {}).get("synchronized"))
            return {"synchronized": synchronized, "failed": failed, "remaining": remaining}

    async def mark_aborted(self, run_id: str) -> None:
        async with self._lock:
            draft = self._read(self._path(run_id, ".draft.json"))
            if draft is not None and draft.get("status") != "finalized":
                draft["status"] = "aborted"
                draft["updatedAtUtc"] = self.clock()
                _atomic_json(self._path(run_id, ".draft.json"), draft)

    async def abort_unfinished(self) -> int:
        count = 0
        async with self._lock:
            if not self.directory.exists():
                return 0
            for path in self.directory.glob("run-*.draft.json"):
                draft = self._read(path)
                if draft and draft.get("status") == "draft":
                    draft["status"] = "aborted"
                    draft["updatedAtUtc"] = self.clock()
                    _atomic_json(path, draft)
                    count += 1
        return count

    async def is_synchronized_async(self, run_id: str) -> bool:
        sidecar = self._read(self._path(run_id, ".sync.json"))
        return bool(sidecar and sidecar.get("synchronized"))

    def is_synchronized(self, run_id: str) -> bool:
        sidecar = self._read(self._path(run_id, ".sync.json"))
        return bool(sidecar and sidecar.get("synchronized"))


def build_mongo_store(directory: Path | None = None) -> RunResultStore:
    mongo = configured_mongo()
    if mongo is not None:
        try:
            mongo.ensure_indexes()
        except Exception:
            # Mongo is deliberately optional at startup; synchronization can
            # still be retried after the operator fixes the deployment.
            pass
    return RunResultStore(directory, mongo=mongo)
