"""FastAPI application for Unity telemetry, control, and AI responses."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Literal
from uuid import uuid4

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from app.ai_servers import AIServerError, AIServerManager, parse_ai_command
from app.career_assessment import (
    CAREER_RESPONSE_FORMAT,
    CareerAssessmentOutputError,
    CareerAssessmentRequest,
    CareerAssessmentResponse,
    build_assessment_messages,
    build_repair_messages,
    parse_assessment_response,
)
from app.config import get_settings
from app.llm_service import LLMService, LLMServiceError
from app.lawyer_assessment import (
    LLMLawyerAssessor,
    LawyerAttemptConflictError,
    LawyerAttemptStore,
    LawyerProcessingError,
    process_lawyer_attempt,
    public_lawyer_status,
    submission_from_lawyer_telemetry,
)
from app.sales_persuasion import (
    LLMSalesAssessor,
    SalesAttemptConflictError,
    SalesAttemptStore,
    SalesPersuasionSubmission,
    SalesProcessingError,
    SalesSubmissionResponse,
    SALES_RECORDING_EVENT_TYPE,
    process_sales_attempt,
    submission_from_sales_telemetry,
    submission_response,
)
from app.sales_returning_customer import (
    CompletionRequest,
    LLMSalesAnalyzer,
    LLMSalesResponder,
    ReturningSessionRequest,
    ReturningSessionStore,
    ReturningTurnRequest,
    complete_session,
    public_session,
    submit_turn,
)
from app.sherpa_setup import install_model_bundle, resolve_model_dir
from app.sherpa_stt import SherpaOnnxTranscriber
from app.run_results import (
    FRAGMENT_TYPES,
    GAME_IDS,
    ParticipantManager,
    RunResultStore,
    build_mongo_store,
    normalize_participant_name,
    project_lawyer_record,
    project_sales_part1,
    project_sales_part2,
    translate_unity_result_fragment,
)


logger = logging.getLogger("vr_backend")
server: uvicorn.Server | None = None
unity_ws: WebSocket | None = None
llm_service: LLMService | None = None
ai_server_manager = AIServerManager()
sales_attempt_store = SalesAttemptStore()
sales_transcriber = SherpaOnnxTranscriber()
sales_processing_tasks: dict[str, asyncio.Task[object]] = {}
sales_returning_store = ReturningSessionStore()
sales_returning_transcriber = sales_transcriber
lawyer_attempt_store = LawyerAttemptStore()
lawyer_processing_tasks: dict[str, asyncio.Task[object]] = {}
participant_manager = ParticipantManager()
run_result_store: RunResultStore = build_mongo_store()
active_run_id: str | None = None
participant_acknowledged_socket: WebSocket | None = None

COMMAND_TIMEOUT_SECONDS = get_settings().command_timeout_seconds
REQUEST_SETTINGS = get_settings()
VALID_SCENE_IDS = frozenset({"standby", "clinic", "doctor", "lawyer", "sale", "tutorial"})
RESET_ALL_TARGET = "all"
DEFENSE_RECORDING_EVENT_TYPE = "lawyer.defense_recording"
ACK_STATUSES = frozenset({"applied", "rejected", "failed"})
SCENE_COMMAND_TYPES = frozenset({"load_scene", "start_scene"})
LLM_SMOKE_TEST_PROMPT = (
    "Trả lời ngắn gọn bằng tiếng Việt để xác nhận mô hình ngôn ngữ đang hoạt động. "
    "/no_think"
)

@dataclass
class PendingSceneCommand:
    sequence: int
    scene_id: str
    acknowledgement: asyncio.Future[dict[str, Any]]
    owner: WebSocket


pending_scene_commands: dict[str, PendingSceneCommand] = {}
next_command_sequence = 1
session: PromptSession | None = None


async def _sales_retention_loop():
    while True:
        await asyncio.sleep(3600)
        # Research recordings and processing records are intentionally retained
        # indefinitely under the participant-linked result design.


async def _result_sync_loop():
    global active_run_id
    while True:
        await asyncio.sleep(1)
        try:
            await run_result_store.retry_due()
            if active_run_id is not None and run_result_store.is_synchronized(active_run_id):
                old = participant_manager.clear(force=True)
                if old:
                    log(f"[Server] Cleared participant {old.name} ({old.session_id}).")
                active_run_id = None
        except Exception as exception:
            logger.warning("Result synchronization retry failed: %s", str(exception)[:300])


@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_service

    llm_service = LLMService()
    await sales_transcriber.initialize()
    await run_result_store.abort_unfinished()
    retention_task = asyncio.create_task(_sales_retention_loop())
    result_sync_task = asyncio.create_task(_result_sync_loop())
    if sales_transcriber.ready:
        await _resume_sales_processing()
        await _resume_lawyer_processing()
    yield
    retention_task.cancel()
    result_sync_task.cancel()
    await asyncio.gather(retention_task, result_sync_task, return_exceptions=True)
    tasks = tuple(sales_processing_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    sales_processing_tasks.clear()
    lawyer_tasks = tuple(lawyer_processing_tasks.values())
    for task in lawyer_tasks:
        task.cancel()
    if lawyer_tasks:
        await asyncio.gather(*lawyer_tasks, return_exceptions=True)
    lawyer_processing_tasks.clear()
    if llm_service is not None:
        await llm_service.close()
    await sales_transcriber.close()
    llm_service = None


app = FastAPI(
    title="Unity VR Backend",
    description="Telemetry, recording storage, Unity control, and optional LLM responses.",
    version="0.2.0",
    lifespan=lifespan,
)


def _redact(value: str) -> str:
    """Remove high-volume or secret-looking values before they reach logs."""

    if "dataBase64" in value or len(value) > 2000:
        return "<redacted>"
    return re.sub(
        r"Authorization:\s*Bearer\s+\S+",
        "Authorization: <redacted>",
        value,
        flags=re.IGNORECASE,
    )


def log(message: Any) -> None:
    """Emit one plain-text, CLI-safe log message."""

    safe_message = _redact(str(message))
    logger.info(safe_message)
    if not logger.handlers and not logging.getLogger().handlers:
        print(safe_message)


def get_prompt_session() -> PromptSession:
    global session
    if session is None:
        session = PromptSession()
    return session


def validate_scene_id(scene_id: str) -> str:
    if not isinstance(scene_id, str):
        raise ValueError("scene ID must be a string")
    normalized = scene_id.strip().lower()
    if normalized not in VALID_SCENE_IDS:
        available = ", ".join(sorted(VALID_SCENE_IDS))
        raise ValueError(f"Unknown scene ID '{scene_id}'. Available scenes: {available}")
    return normalized


def parse_set_game_command(command: str) -> str:
    parts = command.strip().split()
    if len(parts) != 2 or parts[0].lower() != "set_game":
        raise ValueError("Usage: set_game <scene_id>")
    return validate_scene_id(parts[1])


def parse_user_command(command: str) -> str | None:
    """Return a normalized participant name, ``clear``, or ``None`` for query."""
    if not isinstance(command, str):
        raise ValueError("Usage: user [<name>|clear]")
    head, _, rest = command.strip().partition(" ")
    if head.lower() != "user":
        raise ValueError("Usage: user [<name>|clear]")
    if not rest.strip():
        return None
    if rest.strip().lower() == "clear":
        return "clear"
    return normalize_participant_name(rest)


def build_participant_request() -> dict[str, Any]:
    global next_command_sequence
    participant = participant_manager.snapshot()
    request = {
        "commandId": uuid4().hex,
        "sequence": next_command_sequence,
        "type": "assign_participant" if participant else "clear_participant",
        "participant": participant,
        "participantName": participant.get("participantName") if participant else None,
        "participantSessionId": participant.get("participantSessionId") if participant else None,
        "issuedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    next_command_sequence += 1
    return request


def validate_reset_target(scene_id: str) -> str:
    if not isinstance(scene_id, str):
        raise ValueError("reset target must be a string")
    normalized = scene_id.strip().lower()
    if normalized == RESET_ALL_TARGET:
        return normalized
    normalized = validate_scene_id(normalized)
    if normalized == "standby":
        raise ValueError("The standby scene cannot be reset.")
    return normalized


def parse_reset_command(command: str) -> str:
    parts = command.strip().split()
    if len(parts) != 2 or parts[0].lower() != "reset":
        raise ValueError("Usage: reset <scene_id|all>")
    return validate_reset_target(parts[1])


def build_scene_command_request(scene_id: str, command_type: str) -> dict[str, Any]:
    global next_command_sequence

    scene_id = validate_scene_id(scene_id)
    if command_type not in SCENE_COMMAND_TYPES:
        raise ValueError(f"Unsupported scene command type '{command_type}'.")
    request = {
        "commandId": uuid4().hex,
        "sequence": next_command_sequence,
        "type": command_type,
        "sceneId": scene_id,
        "issuedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    next_command_sequence += 1
    return request


def build_set_game_request(scene_id: str) -> dict[str, Any]:
    return build_scene_command_request(scene_id, "load_scene")


def build_reset_request(scene_id: str) -> dict[str, Any]:
    global next_command_sequence

    scene_id = validate_reset_target(scene_id)
    request = {
        "commandId": uuid4().hex,
        "sequence": next_command_sequence,
        "type": "reset_scene",
        "sceneId": scene_id,
        "issuedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    next_command_sequence += 1
    return request


def handle_unity_acknowledgement(data: Any, *, source: WebSocket | None = None) -> bool:
    if not isinstance(data, dict):
        log("[Server] Ignoring malformed Unity ACK: expected a JSON object.")
        return False

    command_id = data.get("commandId")
    sequence = data.get("sequence")
    status = data.get("status")
    if (
        not isinstance(command_id, str)
        or not command_id
        or len(command_id) > 128
        or type(sequence) is not int
        or sequence < 1
        or not isinstance(status, str)
        or status not in ACK_STATUSES
    ):
        log("[Server] Ignoring malformed Unity ACK: commandId, sequence, and status are required.")
        return False

    acknowledged_scene_id = data.get("sceneId")
    if acknowledged_scene_id is not None:
        try:
            acknowledged_scene_id = validate_scene_id(acknowledged_scene_id)
        except ValueError:
            log("[Server] Ignoring Unity ACK with an unknown scene ID.")
            return False

    pending = pending_scene_commands.get(command_id)
    if pending is None:
        log("[Server] Ignoring late or unrelated Unity ACK.")
        return False
    if source is not None and pending.owner is not source:
        log("[Server] Ignoring Unity ACK from a connection that does not own the command.")
        return False
    if sequence != pending.sequence:
        log("[Server] Ignoring Unity ACK with an unexpected sequence.")
        return False

    error_code = data.get("errorCode")
    error_message = data.get("errorMessage")
    if (
        error_code is not None
        and (not isinstance(error_code, str) or len(error_code) > 128)
    ) or (
        error_message is not None
        and (not isinstance(error_message, str) or len(error_message) > 2000)
    ):
        log("[Server] Ignoring Unity ACK with oversized error details.")
        return False

    normalized_data = dict(data)
    if acknowledged_scene_id is not None:
        normalized_data["sceneId"] = acknowledged_scene_id
    if not pending.acknowledgement.done():
        pending.acknowledgement.set_result(normalized_data)
    return True


def fail_pending_scene_commands(message: str, *, owner: WebSocket | None = None) -> None:
    for pending in tuple(pending_scene_commands.values()):
        if owner is not None and pending.owner is not owner:
            continue
        if not pending.acknowledgement.done():
            pending.acknowledgement.set_exception(ConnectionError(message))


async def send_scene_command(
    socket: WebSocket,
    scene_id: str,
    command_type: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    request = build_scene_command_request(scene_id, command_type)
    command_id = request["commandId"]
    future = asyncio.get_running_loop().create_future()
    pending_scene_commands[command_id] = PendingSceneCommand(
        sequence=request["sequence"],
        scene_id=scene_id,
        acknowledgement=future,
        owner=socket,
    )

    try:
        await socket.send_json(request)
    except Exception as exception:
        pending_scene_commands.pop(command_id, None)
        log(f"[Server] Failed to send '{command_type}' to Unity: {exception}")
        return None

    try:
        acknowledgement = await asyncio.wait_for(future, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log(
            f"[Server] '{command_type}' for '{scene_id}' timed out after "
            f"{timeout_seconds:g} seconds."
        )
        return None
    except ConnectionError as exception:
        log(f"[Server] '{command_type}' for '{scene_id}' was interrupted: {exception}")
        return None
    finally:
        pending_scene_commands.pop(command_id, None)

    return acknowledgement


async def send_participant_assignment(
    socket: WebSocket, timeout_seconds: float = COMMAND_TIMEOUT_SECONDS
) -> bool:
    """Send the current identity after connect/reconnect and await its ACK."""
    global participant_acknowledged_socket
    request = build_participant_request()
    future = asyncio.get_running_loop().create_future()
    pending_scene_commands[request["commandId"]] = PendingSceneCommand(
        sequence=request["sequence"], scene_id="standby", acknowledgement=future, owner=socket
    )
    try:
        await socket.send_json(request)
        acknowledgement = await asyncio.wait_for(future, timeout=timeout_seconds)
    except (Exception, asyncio.TimeoutError) as exception:
        log(f"[Server] Participant assignment failed: {exception}")
        participant_acknowledged_socket = None
        return False
    finally:
        pending_scene_commands.pop(request["commandId"], None)
    if acknowledgement.get("status") != "applied":
        participant_acknowledged_socket = None
        return False
    expected = participant_manager.snapshot()
    actual = acknowledgement.get("participant")
    if actual is None and acknowledgement.get("participantName") is not None:
        acknowledged_name = acknowledgement.get("participantName") or ""
        acknowledged_session = acknowledgement.get("participantSessionId") or ""
        actual = None if not acknowledged_name and not acknowledged_session else {
            "participantName": acknowledged_name,
            "participantSessionId": acknowledged_session,
        }
    if actual != expected:
        log("[Server] Unity acknowledged a different participant assignment.")
        participant_acknowledged_socket = None
        return False
    participant_acknowledged_socket = socket
    return True


async def ensure_participant_assignment(socket: WebSocket) -> bool:
    if participant_acknowledged_socket is socket:
        return True
    return await send_participant_assignment(socket)


def scene_command_succeeded(
    acknowledgement: dict[str, Any],
    scene_id: str,
    command_type: str,
    expected_phase: str,
) -> bool:
    status = acknowledgement.get("status")
    acknowledged_scene_id = acknowledgement.get("sceneId")
    if isinstance(acknowledged_scene_id, str):
        acknowledged_scene_id = acknowledged_scene_id.lower()

    if status == "applied" and acknowledged_scene_id == scene_id:
        acknowledged_phase = acknowledgement.get("phase")
        if isinstance(acknowledged_phase, str):
            acknowledged_phase = acknowledged_phase.lower()
        if acknowledged_phase == expected_phase:
            return True
        log(
            f"[Server] '{command_type}' for '{scene_id}' failed: Unity acknowledged "
            f"phase '{acknowledged_phase}' instead of '{expected_phase}'."
        )
        return False

    if status == "applied":
        log(
            f"[Server] '{command_type}' for '{scene_id}' failed: Unity acknowledged "
            f"scene '{acknowledged_scene_id}'."
        )
        return False

    error_code = acknowledgement.get("errorCode") or status or "unknown_error"
    error_message = acknowledgement.get("errorMessage") or "Unity did not apply the command."
    log(f"[Server] '{command_type}' for '{scene_id}' failed ({error_code}): {error_message}")
    return False


async def set_game(
    scene_id: str,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> bool:
    try:
        scene_id = validate_scene_id(scene_id)
    except ValueError as exception:
        log(f"[Server] {exception}")
        return False

    socket = unity_ws
    if socket is None:
        log("[Server] Cannot change scene: Unity is not connected.")
        return False
    previous_activity = participant_manager.activity
    if scene_id != "standby" and participant_manager.active is not None:
        if active_run_id is not None and not run_result_store.is_synchronized(active_run_id):
            log("[Server] Cannot start a game while a completed result awaits database synchronization.")
            return False
        if not await ensure_participant_assignment(socket):
            log("[Server] Cannot start a game: Unity has not acknowledged the participant.")
            return False

    acknowledgement = await send_scene_command(
        socket,
        scene_id,
        "load_scene",
        timeout_seconds,
    )
    if acknowledgement is None or not scene_command_succeeded(
        acknowledgement,
        scene_id,
        "load_scene",
        "standby" if scene_id == "standby" else "ready",
    ):
        participant_manager.activity = previous_activity
        return False

    if scene_id == "standby":
        participant_manager.activity = "idle"
        log("[Server] Scene changed to 'standby'.")
        return True

    if unity_ws is not socket:
        log(f"[Server] Cannot start '{scene_id}': Unity reconnected after loading the scene.")
        participant_manager.activity = previous_activity
        return False

    acknowledgement = await send_scene_command(
        socket,
        scene_id,
        "start_scene",
        timeout_seconds,
    )
    if acknowledgement is None or not scene_command_succeeded(
        acknowledgement,
        scene_id,
        "start_scene",
        "running",
    ):
        participant_manager.activity = previous_activity
        return False

    participant_manager.activity = "gameplay"
    log(f"[Server] Scene '{scene_id}' loaded and started.")
    return True


async def reset_game(
    scene_id: str,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> bool:
    try:
        scene_id = validate_reset_target(scene_id)
    except ValueError as exception:
        log(f"[Server] {exception}")
        return False

    socket = unity_ws
    if socket is None:
        log("[Server] Cannot reset scene: Unity is not connected.")
        return False

    request = build_reset_request(scene_id)
    command_id = request["commandId"]
    future = asyncio.get_running_loop().create_future()
    pending_scene_commands[command_id] = PendingSceneCommand(
        sequence=request["sequence"],
        scene_id=scene_id,
        acknowledgement=future,
        owner=socket,
    )

    try:
        await socket.send_json(request)
    except Exception as exception:
        pending_scene_commands.pop(command_id, None)
        log(f"[Server] Failed to send scene reset to Unity: {exception}")
        return False

    try:
        acknowledgement = await asyncio.wait_for(future, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log(f"[Server] reset '{scene_id}' timed out after {timeout_seconds:g} seconds.")
        return False
    except ConnectionError as exception:
        log(f"[Server] Scene reset '{scene_id}' was interrupted: {exception}")
        return False
    finally:
        pending_scene_commands.pop(command_id, None)

    status = acknowledgement.get("status")
    acknowledged_scene_id = acknowledgement.get("sceneId")
    if isinstance(acknowledged_scene_id, str):
        acknowledged_scene_id = acknowledged_scene_id.lower()

    if status == "applied" and acknowledged_scene_id == "standby":
        participant_manager.activity = "idle"
        log(f"[Server] Reset '{scene_id}' and returned Unity to standby.")
        return True

    if status == "applied":
        log(
            f"[Server] Scene reset '{scene_id}' failed: Unity acknowledged "
            f"scene '{acknowledged_scene_id}' instead of 'standby'."
        )
        return False

    error_code = acknowledgement.get("errorCode") or status or "unknown_error"
    error_message = acknowledgement.get("errorMessage") or "Unity did not apply the scene reset."
    log(f"[Server] Scene reset '{scene_id}' failed ({error_code}): {error_message}")
    return False


def _authorization_is_valid(authorization: str | None, token: str | None = None) -> bool:
    expected = get_settings().api_token
    if expected is None:
        return True
    if token == expected:
        return True
    if not isinstance(authorization, str):
        return False
    scheme, _, supplied = authorization.partition(" ")
    return scheme.lower() == "bearer" and supplied == expected


def _require_http_auth(authorization: str | None) -> None:
    if not _authorization_is_valid(authorization):
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _json_size(data: Any) -> int:
    try:
        return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


@app.get("/api/health")
async def health() -> dict[str, Any]:
    service = llm_service
    llm_configured = service is not None and service.configured
    stt_ready = sales_transcriber.ready
    return {
        "status": "ok",
        "ready": llm_configured and stt_ready,
        "unityConnected": unity_ws is not None,
        "llmConfigured": llm_configured,
        "llmLastError": service.last_error if service is not None else None,
        "sttReady": stt_ready,
        "sttLastError": sales_transcriber.last_error,
    }


@app.get("/api/health/ready")
async def readiness() -> dict[str, Any]:
    result = await health()
    if not result["ready"]:
        raise HTTPException(status_code=503, detail=result)
    return result


@app.post("/api/telemetry")
async def telemetry(
    data: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
    background_tasks: BackgroundTasks = None,
) -> dict[str, str]:
    _require_http_auth(authorization)
    settings = get_settings()
    if _json_size(data) > settings.max_telemetry_bytes:
        raise HTTPException(status_code=413, detail="Telemetry payload is too large.")

    event_type = data.get("eventType")
    if event_type is not None and (
        not isinstance(event_type, str) or len(event_type) > 128
    ):
        raise HTTPException(status_code=422, detail="eventType must be a short string.")

    if event_type == DEFENSE_RECORDING_EVENT_TYPE:
        try:
            payload = data.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            if data.get("runId") is not None and payload.get("runId") is None:
                payload = {**payload, "runId": data.get("runId")}
            submission = submission_from_lawyer_telemetry(payload)
            record, should_process = await lawyer_attempt_store.accept(submission)
        except LawyerAttemptConflictError as exception:
            raise HTTPException(status_code=409, detail=str(exception)) from exception
        except LawyerProcessingError as exception:
            raise HTTPException(status_code=500, detail=str(exception)) from exception
        except ValueError as exception:
            log(f"[Server] Rejected defense recording: {exception}")
            raise HTTPException(status_code=422, detail=str(exception)) from exception
        except OSError as exception:
            log(f"[Server] Failed to store defense recording: {exception}")
            raise HTTPException(
                status_code=500,
                detail="Unable to store defense recording.",
            ) from exception

        if should_process or (
            record.get("assessmentStatus") == "processing"
            and submission.round_id not in lawyer_processing_tasks
        ):
            _schedule_lawyer_processing(submission.round_id, background_tasks)
        try:
            participant = participant_manager.active
            if participant is not None:
                await run_result_store.accept_fragment({
                    "runId": record.get("runId") or data.get("runId", submission.round_id), "gameId": "lawyer",
                    "fragmentId": f"lawyer:{submission.round_id}", "data": project_lawyer_record(record)
                }, participant=participant)
        except (ValueError, RuntimeError):
            logger.warning("Unable to project Lawyer result into simulation aggregate")
        return {
            "status": "accepted",
            "roundId": submission.round_id,
            "assessmentStatus": str(record.get("assessmentStatus", "processing")),
        }

    if event_type == SALES_RECORDING_EVENT_TYPE:
        try:
            payload = data.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            if data.get("runId") is not None and payload.get("runId") is None:
                payload = {**payload, "runId": data.get("runId")}
            submission = submission_from_sales_telemetry(
                payload, session_id=data.get("sessionId")
            )
            record, should_process = await sales_attempt_store.accept(submission)
        except SalesAttemptConflictError as exception:
            raise HTTPException(status_code=409, detail=str(exception)) from exception
        except SalesProcessingError as exception:
            raise HTTPException(status_code=500, detail=str(exception)) from exception
        except ValueError as exception:
            log(f"[Server] Rejected sales persuasion recording: {exception}")
            raise HTTPException(status_code=422, detail=str(exception)) from exception
        except OSError as exception:
            raise HTTPException(
                status_code=500,
                detail="Unable to store sales recording.",
            ) from exception

        if should_process or (
            record.get("assessmentStatus") == "processing"
            and submission.attempt_id not in sales_processing_tasks
        ):
            _schedule_sales_processing(submission.attempt_id, background_tasks)
        try:
            participant = participant_manager.active
            if participant is not None:
                await run_result_store.accept_fragment({
                    "runId": record.get("runId") or data.get("runId", submission.attempt_id), "gameId": "sale",
                    "fragmentId": f"sale.part1:{submission.attempt_id}", "data": {"part1": project_sales_part1(record)}
                }, participant=participant)
        except (ValueError, RuntimeError):
            logger.warning("Unable to project Sales Part 1 into simulation aggregate")
        return {
            "status": "accepted",
            "attemptId": submission.attempt_id,
            "assessmentStatus": record.get("assessmentStatus", "processing"),
        }

    log(f"[Server] Received telemetry event '{event_type or 'unknown'}'.")
    return {"status": "ok"}


@app.post("/api/results/fragments")
async def submit_result_fragment(
    data: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Accept one idempotent Clinic/Doctor/Lawyer/Sales result fragment."""
    _require_http_auth(authorization)
    global active_run_id
    if data.get("kind") in FRAGMENT_TYPES or data.get("fragmentType"):
        try:
            data = translate_unity_result_fragment(data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    elif "kind" in data and data.get("kind") not in {"run.started", "run.aborted"}:
        raise HTTPException(status_code=422, detail="unknown result fragment kind")
    if isinstance(data.get("runId"), str):
        active_run_id = data["runId"]
    if data.get("kind") == "run.started":
        participant = participant_manager.active
        if participant is None:
            raise HTTPException(status_code=409, detail="No active participant.")
        try:
            result = await run_result_store.begin(
                str(data.get("runId")), str(data.get("gameId")), participant,
                data.get("startedAtUtc"),
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        active_run_id = result["runId"]
        participant_manager.activity = "gameplay"
        return {"status": "accepted", "runId": result["runId"], "participantSessionId": result["participantSessionId"]}
    if data.get("kind") == "run.aborted":
        try:
            await run_result_store.mark_aborted(str(data.get("runId")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        active_run_id = None
        participant_manager.activity = "idle"
        return {"status": "aborted", "runId": data.get("runId")}
    try:
        participant = participant_manager.active
        result = await run_result_store.accept_fragment(data, participant=participant)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result.get("status") == "completed":
        participant_manager.activity = "idle"
        if run_result_store.is_synchronized(result["runId"]):
            old = participant_manager.clear(force=True)
            if old:
                log(f"[Server] Cleared participant {old.name} ({old.session_id}).")
            active_run_id = None
    return {"status": result.get("status", "accepted"), "runId": result["runId"],
            "participantName": result["participantName"],
            "participantSessionId": result["participantSessionId"]}


def _public_sales_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"requestHash", "audioFile"}
    }


async def _queue_lawyer_processing(round_id: str) -> None:
    current = lawyer_processing_tasks.get(round_id)
    if current is not None and not current.done():
        return

    async def process_and_project() -> object:
        record = await process_lawyer_attempt(
            round_id,
            store=lawyer_attempt_store,
            transcriber=sales_transcriber,
            assessor=LLMLawyerAssessor(llm_service),
        )
        participant = participant_manager.active
        if participant is not None and isinstance(record, dict) and record.get("assessmentStatus") == "completed":
            try:
                await run_result_store.accept_fragment({"runId": record.get("runId") or round_id, "gameId": "lawyer",
                    "fragmentId": f"lawyer:{round_id}:completed", "data": project_lawyer_record(record)}, participant=participant)
            except (ValueError, RuntimeError):
                logger.warning("Unable to project completed Lawyer result")
        return record

    task = asyncio.create_task(process_and_project())
    lawyer_processing_tasks[round_id] = task

    def finished(completed: asyncio.Task[object]) -> None:
        if lawyer_processing_tasks.get(round_id) is completed:
            lawyer_processing_tasks.pop(round_id, None)
        try:
            completed.exception()
        except asyncio.CancelledError:
            return
        except Exception as exception:
            logger.error("Lawyer processing task failed for %s: %s", round_id, exception)

    task.add_done_callback(finished)


def _schedule_lawyer_processing(
    round_id: str, background_tasks: BackgroundTasks | None
) -> None:
    if background_tasks is None:
        asyncio.create_task(_queue_lawyer_processing(round_id))
    else:
        background_tasks.add_task(_queue_lawyer_processing, round_id)


async def _resume_lawyer_processing() -> None:
    try:
        round_ids = await lawyer_attempt_store.processing_round_ids()
    except LawyerProcessingError as exception:
        logger.error("Unable to scan Lawyer attempts for recovery: %s", exception)
        return
    for round_id in round_ids:
        await _queue_lawyer_processing(round_id)


@app.get("/api/lawyer/defense-recordings/{round_id}")
async def get_lawyer_defense_status(
    round_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_http_auth(authorization)
    try:
        record = await lawyer_attempt_store.get(round_id)
    except (ValueError, LawyerProcessingError) as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    if record is None:
        raise HTTPException(status_code=404, detail="Lawyer defense not found.")
    return public_lawyer_status(record)


@app.get("/api/lawyer/defense-recordings/{round_id}/diagnostic")
async def get_lawyer_defense_diagnostic(
    round_id: str,
    x_diagnostic_token: str | None = Header(default=None, alias="X-Diagnostic-Token"),
) -> dict[str, Any]:
    configured_token = get_settings().lawyer_diagnostic_token
    if not configured_token or x_diagnostic_token != configured_token:
        raise HTTPException(status_code=403, detail="Lawyer diagnostics are unavailable.")
    try:
        record = await lawyer_attempt_store.get(round_id)
    except (ValueError, LawyerProcessingError) as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    if record is None:
        raise HTTPException(status_code=404, detail="Lawyer defense not found.")
    return {
        key: value
        for key, value in record.items()
        if key not in {"requestHash", "audioFile"}
    }


async def _queue_sales_processing(attempt_id: str) -> None:
    current = sales_processing_tasks.get(attempt_id)
    if current is not None and not current.done():
        return

    async def process_and_project() -> object:
        record = await process_sales_attempt(
            attempt_id,
            store=sales_attempt_store,
            transcriber=sales_transcriber,
            assessor=LLMSalesAssessor(llm_service),
        )
        participant = participant_manager.active
        if participant is not None and isinstance(record, dict) and record.get("assessmentStatus") == "completed":
            try:
                await run_result_store.accept_fragment({"runId": record.get("runId") or attempt_id, "gameId": "sale",
                    "fragmentId": f"sale.part1:{attempt_id}:completed", "data": {"part1": project_sales_part1(record)}}, participant=participant)
            except (ValueError, RuntimeError):
                logger.warning("Unable to project completed Sales Part 1 result")
        return record

    task = asyncio.create_task(process_and_project())
    sales_processing_tasks[attempt_id] = task

    def finished(completed: asyncio.Task[object]) -> None:
        if sales_processing_tasks.get(attempt_id) is completed:
            sales_processing_tasks.pop(attempt_id, None)
        try:
            completed.exception()
        except asyncio.CancelledError:
            return
        except Exception as exception:
            logger.error(
                "Sales processing task failed for %s: %s",
                attempt_id,
                exception,
            )

    task.add_done_callback(finished)


def _schedule_sales_processing(
    attempt_id: str,
    background_tasks: BackgroundTasks | None,
) -> None:
    if background_tasks is None:
        asyncio.create_task(_queue_sales_processing(attempt_id))
    else:
        background_tasks.add_task(_queue_sales_processing, attempt_id)


async def _resume_sales_processing() -> None:
    try:
        attempt_ids = await sales_attempt_store.processing_attempt_ids()
    except SalesProcessingError as exception:
        logger.error("Unable to scan sales attempts for recovery: %s", exception)
        return
    for attempt_id in attempt_ids:
        await _queue_sales_processing(attempt_id)


@app.post(
    "/api/sales/persuasion-recordings",
    response_model=SalesSubmissionResponse,
    response_model_by_alias=True,
)
@app.post(
    "/api/sales/persuasion-recording",
    response_model=SalesSubmissionResponse,
    response_model_by_alias=True,
    include_in_schema=False,
)
async def submit_sales_persuasion_recording(
    request: SalesPersuasionSubmission,
    authorization: str | None = Header(default=None),
    background_tasks: BackgroundTasks = None,
) -> SalesSubmissionResponse:
    _require_http_auth(authorization)
    try:
        record, should_process = await sales_attempt_store.accept(request)
    except SalesAttemptConflictError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception
    except SalesProcessingError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    except OSError as exception:
        log(f"[Server] Failed to persist sales attempt '{request.attempt_id}': {exception}")
        raise HTTPException(
            status_code=500,
            detail="Unable to store sales recording.",
        ) from exception

    if should_process or (
        record.get("assessmentStatus") == "processing"
        and request.attempt_id not in sales_processing_tasks
    ):
        _schedule_sales_processing(request.attempt_id, background_tasks)
    return submission_response(record)


@app.get("/api/sales/persuasion-recordings/{attempt_id}")
async def get_sales_persuasion_recording(
    attempt_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_http_auth(authorization)
    try:
        record = await sales_attempt_store.get(attempt_id)
    except SalesProcessingError as exception:
        raise HTTPException(status_code=500, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    if record is None:
        raise HTTPException(status_code=404, detail="Sales attempt not found.")
    return _public_sales_record(record)


@app.post("/api/sales/sessions")
@app.post("/api/sales/session", include_in_schema=False)
async def create_sales_session(
    request: ReturningSessionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    run_id = request.run_id or active_run_id
    _require_http_auth(authorization)
    try:
        session = await sales_returning_store.create_or_resume(request.session_id, request.part1_attempt_id, run_id)
    except ValueError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    except OSError as exception:
        raise HTTPException(status_code=500, detail="Unable to store sales session.") from exception
    return public_session(session)


@app.get("/api/sales/sessions/{session_id}")
@app.get("/api/sales/session/{session_id}", include_in_schema=False)
async def get_sales_session(
    session_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_http_auth(authorization)
    try:
        session = await sales_returning_store.get(session_id)
    except ValueError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    if session is None:
        raise HTTPException(status_code=404, detail="Sales session not found.")
    return public_session(session)


@app.post("/api/sales/sessions/{session_id}/turns")
@app.post("/api/sales/session/{session_id}/turns", include_in_schema=False)
async def submit_sales_returning_turn(
    session_id: str,
    request: ReturningTurnRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_http_auth(authorization)
    try:
        return await submit_turn(
            session_id,
            request,
            store=sales_returning_store,
            transcriber=sales_returning_transcriber,
            responder=LLMSalesResponder(llm_service),
        )
    except KeyError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=409, detail="Sales operation cannot proceed.") from exception
    except OSError as exception:
        raise HTTPException(status_code=500, detail="Unable to store sales turn.") from exception


@app.post("/api/sales/sessions/{session_id}/part1")
@app.post("/api/sales/session/{session_id}/part1", include_in_schema=False)
async def associate_sales_part1(
    session_id: str,
    request: ReturningSessionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    run_id = request.run_id or active_run_id
    _require_http_auth(authorization)
    if request.part1_attempt_id is None:
        raise HTTPException(status_code=422, detail="part1AttemptId is required.")
    try:
        session = await sales_returning_store.create_or_resume(session_id, request.part1_attempt_id, run_id)
    except ValueError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    except OSError as exception:
        raise HTTPException(status_code=500, detail="Unable to associate Part 1 attempt.") from exception
    return public_session(session)


@app.post("/api/sales/sessions/{session_id}/complete")
@app.post("/api/sales/session/{session_id}/complete", include_in_schema=False)
async def complete_sales_session(
    session_id: str,
    request: CompletionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_http_auth(authorization)
    try:
        result = await complete_session(
            session_id,
            request,
            store=sales_returning_store,
            analyzer=LLMSalesAnalyzer(llm_service),
        )
        participant = participant_manager.active
        if participant is not None and result.get("completionStatus") == "completed":
            try:
                await run_result_store.accept_fragment({
                    "runId": result.get("runId") or session_id, "gameId": "sale", "fragmentId": f"sale.part2:{request.completion_id}",
                    "data": {"part2": project_sales_part2(result)},
                }, participant=participant)
            except (ValueError, RuntimeError):
                logger.warning("Unable to project completed Sales Part 2 result")
        return public_session(result)
    except KeyError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception
    except RuntimeError as exception:
        raise HTTPException(status_code=503, detail=str(exception)) from exception


@app.delete("/api/sales/sessions/{session_id}/diagnostics")
@app.delete("/api/sales/session/{session_id}/diagnostics", include_in_schema=False)
async def delete_sales_diagnostics(
    session_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    if settings.sales_diagnostic_token is None or authorization != "Bearer " + settings.sales_diagnostic_token:
        raise HTTPException(status_code=401, detail="Diagnostic authorization required.")
    try:
        count = await sales_returning_store.delete_diagnostics(session_id)
        count += await sales_attempt_store.delete_session_diagnostics(session_id)
    except ValueError as exception:
        raise HTTPException(status_code=422, detail=str(exception)) from exception
    return {"status": "deleted", "sessionId": session_id, "deletedFiles": count}


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(
        min_length=1,
        max_length=REQUEST_SETTINGS.max_message_chars,
    )


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: str = Field(min_length=1, max_length=REQUEST_SETTINGS.max_transcript_chars)
    game_id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")
    conversation: list[ConversationMessage] = Field(
        default_factory=list,
        max_length=REQUEST_SETTINGS.max_conversation_messages,
    )


@app.post("/api/ai/respond")
async def respond(
    request: ChatRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    _require_http_auth(authorization)
    service = llm_service
    if service is None or not service.configured:
        raise HTTPException(status_code=503, detail="Language model is not configured.")

    system_prompt = get_game_prompt(request.game_id)
    messages = [
        {"role": "system", "content": system_prompt},
        *[message.model_dump() for message in request.conversation],
        {"role": "user", "content": f"{request.transcript}\n/no_think"},
    ]
    
    try:
        answer = await service.generate(messages)
    except LLMServiceError as exception:
        log(f"[Server] LLM request failed: {exception}")
        raise HTTPException(
            status_code=exception.status_code,
            detail=str(exception),
        ) from exception

    return {"text": answer, "game_id": request.game_id}


@app.post(
    "/api/ai/initial-career-assessment",
    response_model=CareerAssessmentResponse,
)
async def assess_careers(
    request: CareerAssessmentRequest,
    authorization: str | None = Header(default=None),
) -> CareerAssessmentResponse:
    _require_http_auth(authorization)
    service = llm_service
    if service is None or not service.configured:
        raise HTTPException(status_code=503, detail="Language model is not configured.")

    settings = get_settings()
    generation_options = {
        "temperature": 0.2,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "max_tokens": settings.llm_career_max_tokens,
        "response_format": CAREER_RESPONSE_FORMAT,
    }

    try:
        answer = await service.generate(
            build_assessment_messages(request),
            options=generation_options,
            max_message_chars=settings.max_career_prompt_chars,
        )
    except LLMServiceError as exception:
        log(f"[Server] Career assessment LLM request failed: {exception}")
        raise HTTPException(
            status_code=exception.status_code,
            detail=str(exception),
        ) from exception

    try:
        return parse_assessment_response(answer, request)
    except CareerAssessmentOutputError:
        log("[Server] Career assessment returned invalid JSON; attempting one repair.")

    try:
        repaired_answer = await service.generate(
            build_repair_messages(request, answer),
            options=generation_options,
            max_message_chars=settings.max_career_prompt_chars,
        )
        return parse_assessment_response(repaired_answer, request)
    except LLMServiceError as exception:
        log(f"[Server] Career assessment repair failed: {exception}")
        raise HTTPException(
            status_code=exception.status_code,
            detail=str(exception),
        ) from exception
    except CareerAssessmentOutputError as exception:
        log("[Server] Career assessment repair returned invalid JSON.")
        raise HTTPException(
            status_code=502,
            detail="The language model returned an invalid career assessment.",
        ) from exception


def get_game_prompt(game_id: str) -> str:
    prompts = {
        "lawyer": (
            "Bạn là nhân vật trong trò chơi mô phỏng nghề luật sư. "
            "Trả lời bằng tiếng Việt tự nhiên và ngắn gọn. "
            "Chỉ sử dụng thông tin có trong kịch bản và bằng chứng được cung cấp."
        ),
        "doctor": (
            "Bạn là bệnh nhân trong trò chơi mô phỏng nghề bác sĩ. "
            "Trả lời bằng tiếng Việt tự nhiên và ngắn gọn. "
            "Không tự tạo thêm triệu chứng ngoài kịch bản."
        ),
    }
    stt_prompt = (
        "This is Vietnamese STT output and may be incomplete, noisy, or incoherent.\n"
        "Determine whether the player's intent is clear enough to answer.\n"
        "Do not invent missing meaning. If unclear, return NEED_CLARIFICATION\n"
        "with one short question.\n"
    )
    return stt_prompt + prompts.get(
        game_id,
        "You are a character in a simulation game. Answer in Vietnamese naturally and concisely.",
    )


@app.websocket("/ws/ctrl")
async def commands(
    ws: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    global unity_ws, participant_acknowledged_socket, active_run_id

    authorization = ws.headers.get("authorization")
    if not _authorization_is_valid(authorization, token):
        await ws.close(code=1008, reason="Authentication required")
        return

    await ws.accept()
    previous_socket = unity_ws
    if previous_socket is not None and previous_socket is not ws:
        fail_pending_scene_commands("Unity connection replaced.", owner=previous_socket)
        try:
            await previous_socket.close(code=1012, reason="Replaced by a newer Unity connection")
        except Exception:
            pass
    unity_ws = ws
    log("[Server] Unity connected")
    assignment_task = asyncio.create_task(send_participant_assignment(ws))

    try:
        while True:
            message = await ws.receive_text()
            if len(message) > 256_000:
                log("[Server] Ignoring oversized Unity message.")
                continue
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                log("[Server] Ignoring malformed Unity ACK: invalid JSON.")
                continue

            handle_unity_acknowledgement(data, source=ws)
    except WebSocketDisconnect:
        log("[Server] Unity disconnected")
    finally:
        if not assignment_task.done():
            assignment_task.cancel()
            await asyncio.gather(assignment_task, return_exceptions=True)
        if unity_ws is ws:
            unity_ws = None
            if participant_acknowledged_socket is ws:
                participant_acknowledged_socket = None
            fail_pending_scene_commands("Unity disconnected.", owner=ws)
            if active_run_id is not None:
                await run_result_store.mark_aborted(active_run_id)
                active_run_id = None
                participant_manager.activity = "idle"

async def run_llm_smoke_test() -> bool:
    """Send a fixed prompt to the configured LLM and print its response."""

    service = llm_service
    if service is None or not service.configured:
        log("[LLM Test] Failed: language model is not configured.")
        return False

    try:
        answer = await service.generate(
            [{"role": "user", "content": LLM_SMOKE_TEST_PROMPT}]
        )
    except LLMServiceError as exception:
        log(f"[LLM Test] Failed: {exception}")
        return False

    log(f"[LLM Test] Result: {answer}")
    return True


def _server_names(names: tuple[str, ...]) -> str:
    return " and ".join(names)


async def run_ai_server_command(command: str) -> bool:
    """Run one model-server lifecycle command from the operator console."""

    try:
        parsed = parse_ai_command(command)
        if parsed.action == "setup":
            settings = get_settings()
            result = await asyncio.to_thread(
                install_model_bundle, resolve_model_dir(settings.sherpa_model_dir)
            )
            if not await sales_transcriber.initialize():
                raise AIServerError(sales_transcriber.last_error or "Speech recognition initialization failed.")
            await _resume_sales_processing()
            state = "Installed" if result.installed else "Already installed"
            log(f"[AI] {state} {result.model_dir}; speech recognition is ready.")
            return True
        if parsed.target is None:
            raise ValueError("AI server target is required.")
        if parsed.action == "status":
            statuses = await ai_server_manager.status(parsed.target)
            for status in statuses:
                if status.running:
                    log(
                        f"[AI] {status.name}: running (PID {status.pid}, "
                        f"http://{status.host}:{status.port})."
                    )
                elif status.return_code is not None:
                    log(
                        f"[AI] {status.name}: stopped "
                        f"(exit code {status.return_code})."
                    )
                else:
                    log(f"[AI] {status.name}: stopped.")
            return True

        if parsed.action == "start":
            started = await ai_server_manager.start(parsed.target)
            if started:
                log(f"[AI] Started {_server_names(started)}.")
            else:
                log(f"[AI] {parsed.target} is already running.")
            return True

        if parsed.action == "stop":
            stopped = await ai_server_manager.stop(parsed.target)
            if stopped:
                log(f"[AI] Stopped {_server_names(stopped)}.")
            else:
                log(f"[AI] {parsed.target} is not running.")
            return True

        restarted = await ai_server_manager.restart(parsed.target)
        log(f"[AI] Restarted {_server_names(restarted)}.")
        return True
    except (AIServerError, OSError, ValueError) as exception:
        log(f"[AI] {exception}")
        return False


async def run_user_command(command: str) -> bool:
    global participant_acknowledged_socket
    try:
        value = parse_user_command(command)
        if value is None:
            current = participant_manager.snapshot()
            log(f"[Server] Active participant: {current['participantName']} ({current['participantSessionId']})." if current else "[Server] No active participant.")
            return True
        if value == "clear":
            old = participant_manager.clear()
            participant_acknowledged_socket = None
            if old is None:
                log("[Server] No active participant to clear.")
                return True
            log(f"[Server] Cleared participant {old.name} ({old.session_id}).")
        else:
            participant, changed = participant_manager.assign(value)
            participant_acknowledged_socket = None
            log(f"[Server] Participant {participant.name} assigned ({participant.session_id})." if changed else "[Server] Participant assignment unchanged.")
        if unity_ws is not None:
            await ensure_participant_assignment(unity_ws)
        return True
    except (ValueError, RuntimeError) as exception:
        log(f"[Server] {exception}")
        return False


async def run_db_sync_command() -> bool:
    global active_run_id
    try:
        result = await run_result_store.sync_all()
        if active_run_id is not None and run_result_store.is_synchronized(active_run_id):
            old = participant_manager.clear(force=True)
            if old:
                log(f"[Server] Cleared participant {old.name} ({old.session_id}).")
            active_run_id = None
        log(f"[DB] synchronized={result['synchronized']} failed={result['failed']} remaining={result['remaining']}")
        return result["failed"] == 0
    except Exception as exception:
        log(f"[DB] Synchronization failed: {str(exception)[:500]}")
        return False


async def stop_ai_servers_for_shutdown() -> None:
    """Stop every managed AI server before the operator process exits."""

    statuses = await ai_server_manager.status("all")
    if not any(status.running for status in statuses):
        return
    log("[AI] Stopping managed AI servers before backend shutdown...")
    try:
        stopped = await ai_server_manager.stop("all")
    except (AIServerError, OSError) as exception:
        log(f"[AI] Shutdown failed: {exception}")
        return
    if stopped:
        log(f"[AI] Stopped {_server_names(stopped)}.")


async def handleCommands() -> None:
    global server

    command_session = get_prompt_session()
    while True:
        try:
            command: str = (await command_session.prompt_async("> ")).strip()
        except (EOFError, KeyboardInterrupt):
            log("[Server] Exiting...")
            return

        if not command:
            continue
        if command == "test_llm":
            await run_llm_smoke_test()
            continue
        if command == "status":
            status = "connected" if unity_ws is not None else "not connected"
            current = participant_manager.snapshot()
            log(f"[Server] Unity is {status}; participant={'set' if current else 'none' }.")
            continue
        if command.split(maxsplit=1)[0].lower() == "user":
            await run_user_command(command)
            continue
        if command.lower() == "db sync":
            await run_db_sync_command()
            continue
        if command.split(maxsplit=1)[0].lower() == "ai":
            await run_ai_server_command(command)
            continue
        if command == "exit":
            log("[Server] Exiting...")
            return

        try:
            command_name = command.split(maxsplit=1)[0].lower()
            if command_name == "reset":
                scene_id = parse_reset_command(command)
            else:
                scene_id = parse_set_game_command(command)
        except ValueError as exception:
            log(f"[Server] {exception}")
            continue

        if command_name == "reset":
            await reset_game(scene_id)
        else:
            if scene_id != "standby" and participant_manager.active is None:
                log("[Server] Cannot start a game: assign a participant with 'user <name>'.")
            else:
                await set_game(scene_id)


async def _run_backend() -> None:
    global server

    settings = get_settings()
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        access_log=False,
        use_colors=False,
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    commands_task: asyncio.Task[None] | None = None

    try:
        while not server.started:
            await asyncio.sleep(0.05)

        commands_task = asyncio.create_task(handleCommands())
        done, _ = await asyncio.wait(
            {server_task, commands_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if commands_task in done:
            await stop_ai_servers_for_shutdown()
            server.should_exit = True
        await server_task
    finally:
        if commands_task is not None and not commands_task.done():
            commands_task.cancel()
            try:
                await commands_task
            except asyncio.CancelledError:
                pass
        await stop_ai_servers_for_shutdown()
        if not server_task.done():
            server.should_exit = True
            try:
                await asyncio.shield(server_task)
            except asyncio.CancelledError:
                pass


async def main() -> None:
    with patch_stdout():
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        await _run_backend()



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
