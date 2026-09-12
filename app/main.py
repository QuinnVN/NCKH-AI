"""FastAPI application for Unity telemetry, control, and AI responses."""

"""FastAPI application for Unity telemetry, control, and AI responses."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Literal
import logging
import re
from typing import Any, Literal
from uuid import uuid4

from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
from pydantic import BaseModel, ConfigDict, Field
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
from app.save_recording import decode_defense_recording, write_defense_recording
from app.sales_persuasion import (
    LLMSalesAssessor,
    SalesAttemptConflictError,
    SalesAttemptStore,
    SalesPersuasionSubmission,
    SalesProcessingError,
    SalesSubmissionResponse,
    SALES_RECORDING_EVENT_TYPE,
    WhisperCppTranscriber,
    process_sales_attempt,
    submission_from_sales_telemetry,
    submission_response,
)


logger = logging.getLogger("vr_backend")
server: uvicorn.Server | None = None
unity_ws: WebSocket | None = None
llm_service: LLMService | None = None
ai_server_manager = AIServerManager()
sales_attempt_store = SalesAttemptStore()
sales_transcriber = WhisperCppTranscriber()
sales_processing_tasks: dict[str, asyncio.Task[object]] = {}

COMMAND_TIMEOUT_SECONDS = 5.0
VALID_SCENE_IDS = frozenset({"standby", "clinic", "doctor", "lawyer"})
DEFENSE_RECORDING_EVENT_TYPE = "lawyer.defense_recording"
ACK_STATUSES = frozenset({"applied", "rejected"})
LLM_SMOKE_TEST_PROMPT = (
    "Trả lời ngắn gọn bằng tiếng Việt để xác nhận mô hình ngôn ngữ đang hoạt động. "
    "/no_think"
)

ACK_STATUSES = frozenset({"applied", "rejected"})
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_service

    llm_service = LLMService()
    await _resume_sales_processing()
    yield
    tasks = tuple(sales_processing_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    sales_processing_tasks.clear()
    if llm_service is not None:
        await llm_service.close()
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
    """Emit one JSON log record while keeping the existing CLI-friendly API."""

    safe_message = _redact(str(message))
    logger.info(json.dumps({"message": safe_message}, ensure_ascii=False))
    if not logger.handlers and not logging.getLogger().handlers:
        print_formatted_text(safe_message)
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
    """Emit one JSON log record while keeping the existing CLI-friendly API."""

    safe_message = _redact(str(message))
    logger.info(json.dumps({"message": safe_message}, ensure_ascii=False))
    if not logger.handlers and not logging.getLogger().handlers:
        print_formatted_text(safe_message)


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


def build_set_game_request(scene_id: str) -> dict[str, Any]:
    global next_command_sequence

    scene_id = validate_scene_id(scene_id)
    request = {
        "commandId": uuid4().hex,
        "sequence": next_command_sequence,
        "type": "load_scene",
        "sceneId": scene_id,
        "issuedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    next_command_sequence += 1
    return request


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

    request = build_set_game_request(scene_id)
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
        log(f"[Server] Failed to send scene change to Unity: {exception}")
        return False

    try:
        acknowledgement = await asyncio.wait_for(future, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log(f"[Server] set_game '{scene_id}' timed out after {timeout_seconds:g} seconds.")
        return False
    except ConnectionError as exception:
        log(f"[Server] Scene change to '{scene_id}' was interrupted: {exception}")
        return False
    finally:
        pending_scene_commands.pop(command_id, None)

    status = acknowledgement.get("status")
    acknowledged_scene_id = acknowledgement.get("sceneId")
    if isinstance(acknowledged_scene_id, str):
        acknowledged_scene_id = acknowledged_scene_id.lower()

    if status == "applied" and acknowledged_scene_id == scene_id:
        log(f"[Server] Scene changed to '{scene_id}'.")
        return True

    if status == "applied":
        log(
            f"[Server] Scene change to '{scene_id}' failed: Unity acknowledged "
            f"scene '{acknowledged_scene_id}'."
        )
        return False

    error_code = acknowledgement.get("errorCode") or status or "unknown_error"
    error_message = acknowledgement.get("errorMessage") or "Unity did not apply the scene change."
    log(f"[Server] Scene change to '{scene_id}' failed ({error_code}): {error_message}")
    return False


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
    return {
        "status": "ok",
        "ready": llm_configured,
        "unityConnected": unity_ws is not None,
        "llmConfigured": llm_configured,
        "llmLastError": service.last_error if service is not None else None,
    }


@app.get("/api/health/ready")
async def readiness() -> dict[str, Any]:
    result = await health()
    if not result["ready"]:
        raise HTTPException(status_code=503, detail=result)
    return result


@app.post("/api/telemetry")
async def telemetry(data: dict[str, Any]):
    if data.get("eventType") == DEFENSE_RECORDING_EVENT_TYPE:
        try:
            round_id, wav_bytes = decode_defense_recording(data)
        except ValueError as exception:
            log(f"[Server] Rejected defense recording: {exception}")
            raise HTTPException(status_code=422, detail=str(exception)) from exception

        try:
            destination = await asyncio.to_thread(
                write_defense_recording, round_id, wav_bytes
            )
        except OSError as exception:
            log(f"[Server] Failed to save defense recording '{round_id}': {exception}")
            raise HTTPException(
                status_code=500,
                detail="Unable to save defense recording.",
            ) from exception

        log(
            f"[Server] Saved defense recording '{round_id}' "
            f"({len(wav_bytes)} bytes) to '{destination}'."
        )
        return {
            "status": "ok",
        }

    log(data)
    return {
        "status": "ok",
    }
    
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
    
    log(f"[Server] Sending request to LLM service: {messages}")

    answer = await llm_service.generate(messages)

    return {
        "text": answer,
        "game_id": request.game_id,
    }


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
    global unity_ws

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
        if unity_ws is ws:
            unity_ws = None
            fail_pending_scene_commands("Unity disconnected.")

async def handleCommands():
    global unity_ws

    command_session = get_prompt_session()
    while True:
        try:
            command: str = (await command_session.prompt_async("> ")).strip()
        except (EOFError, KeyboardInterrupt):
            log("[Server] Exiting...")
            
            if server is not None:
                server.should_exit = True
            return

        if not command:
            continue
            
        match command:
            case "test":
                log("Test command executed.")
                continue
            
            case "status":
                if unity_ws is not None:
                    log("[Server] Unity is connected.")
                else:
                    log("[Server] Unity is not connected.")
                continue
            
            case "exit":
                log("[Server] Exiting...")
                 
                if server is not None:
                    server.should_exit = True
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
            await set_game(scene_id)


async def main() -> None:
    global server

    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    
    while not server.started:
        await asyncio.sleep(0.05)
     
    with patch_stdout():
        commands_task = asyncio.create_task(handleCommands())
        
        #wait until either the server or the commands task is done
        done, pending = await asyncio.wait(
            {server_task, commands_task},
            return_when=asyncio.FIRST_COMPLETED
        )
        
        #if the commands task is done, we want to exit the server
        if commands_task in done:
            server.should_exit = True
        
        #wait for the server to shutdown gracefully
        await server_task
        
        #stop the commands task if it is still running
        if not commands_task.done():
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



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
