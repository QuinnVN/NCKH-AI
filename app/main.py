import asyncio
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4
import wave

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
import uvicorn

server: uvicorn.Server | None = None
unity_ws: WebSocket | None = None

COMMAND_TIMEOUT_SECONDS = 5.0
VALID_SCENE_IDS = frozenset({"standby", "clinic", "doctor", "lawyer"})
DEFENSE_RECORDING_EVENT_TYPE = "lawyer.defense_recording"
MAX_RECORDING_BYTES = 8 * 1024 * 1024
ROUND_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
BACKEND_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class PendingSceneCommand:
    sequence: int
    scene_id: str
    acknowledgement: asyncio.Future[dict[str, Any]]


pending_scene_commands: dict[str, PendingSceneCommand] = {}
next_command_sequence = 1

app = FastAPI()
session: PromptSession | None = None

def log(message: Any):
    print_formatted_text(message)


def get_recordings_directory() -> Path:
    configured = os.environ.get("RECORDINGS_DIR", "recordings").strip() or "recordings"
    directory = Path(configured).expanduser()
    if not directory.is_absolute():
        directory = BACKEND_ROOT / directory
    return directory.resolve()


def decode_defense_recording(data: dict[str, Any]) -> tuple[str, bytes]:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    round_id = payload.get("roundId")
    if not isinstance(round_id, str) or ROUND_ID_PATTERN.fullmatch(round_id) is None:
        raise ValueError("payload.roundId must be a 32-character hexadecimal ID")

    audio = payload.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("payload.audio must be an object")
    if audio.get("mimeType") != "audio/wav":
        raise ValueError("payload.audio.mimeType must be audio/wav")
    if audio.get("encoding") != "pcm_s16le":
        raise ValueError("payload.audio.encoding must be pcm_s16le")
    if type(audio.get("sampleRateHz")) is not int or audio["sampleRateHz"] != 16000:
        raise ValueError("payload.audio.sampleRateHz must be 16000")
    if type(audio.get("channels")) is not int or audio["channels"] != 1:
        raise ValueError("payload.audio.channels must be 1")

    encoded = audio.get("dataBase64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("payload.audio.dataBase64 must be a non-empty string")
    maximum_encoded_length = 4 * ((MAX_RECORDING_BYTES + 2) // 3)
    if len(encoded) > maximum_encoded_length:
        raise ValueError("recording exceeds the 8 MiB limit")

    try:
        wav_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exception:
        raise ValueError("payload.audio.dataBase64 is not valid Base64") from exception
    if len(wav_bytes) > MAX_RECORDING_BYTES:
        raise ValueError("recording exceeds the 8 MiB limit")

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as recording:
            channels = recording.getnchannels()
            sample_width = recording.getsampwidth()
            sample_rate = recording.getframerate()
            frame_count = recording.getnframes()
            compression = recording.getcomptype()
    except (EOFError, wave.Error) as exception:
        raise ValueError("recording is not a valid PCM WAV file") from exception

    if compression != "NONE" or sample_width != 2:
        raise ValueError("recording must use signed 16-bit PCM samples")
    if channels != audio["channels"]:
        raise ValueError("WAV channel count does not match payload.audio.channels")
    if sample_rate != audio["sampleRateHz"]:
        raise ValueError("WAV sample rate does not match payload.audio.sampleRateHz")
    if frame_count <= 0:
        raise ValueError("recording must contain at least one audio frame")

    return round_id.lower(), wav_bytes


def write_defense_recording(round_id: str, wav_bytes: bytes) -> Path:
    directory = get_recordings_directory()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"lawyer-defense-{round_id}.wav"
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.stem}-",
            suffix=".tmp",
            dir=directory,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(wav_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    return destination


def get_prompt_session() -> PromptSession:
    global session
    if session is None:
        session = PromptSession()
    return session


def parse_set_game_command(command: str) -> str:
    parts = command.strip().split()
    if len(parts) != 2 or parts[0] != "set_game":
        raise ValueError("Usage: set_game <scene_id>")

    scene_id = parts[1].lower()
    if scene_id not in VALID_SCENE_IDS:
        available = ", ".join(sorted(VALID_SCENE_IDS))
        raise ValueError(f"Unknown scene ID '{parts[1]}'. Available scenes: {available}")

    return scene_id


def build_set_game_request(scene_id: str) -> dict[str, Any]:
    global next_command_sequence

    request = {
        "commandId": uuid4().hex,
        "sequence": next_command_sequence,
        "type": "load_scene",
        "sceneId": scene_id,
        "issuedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    next_command_sequence += 1
    return request


def handle_unity_acknowledgement(data: Any) -> bool:
    if not isinstance(data, dict):
        log("[Server] Ignoring malformed Unity ACK: expected a JSON object.")
        return False

    command_id = data.get("commandId")
    sequence = data.get("sequence")
    if not isinstance(command_id, str) or not command_id or not isinstance(sequence, int):
        log("[Server] Ignoring malformed Unity ACK: commandId and sequence are required.")
        return False

    pending = pending_scene_commands.get(command_id)
    if pending is None:
        log(f"[Server] Ignoring late or unrelated Unity ACK '{command_id}'.")
        return False

    if sequence != pending.sequence:
        log(f"[Server] Ignoring Unity ACK '{command_id}' with an unexpected sequence.")
        return False

    if not pending.acknowledgement.done():
        pending.acknowledgement.set_result(data)
    return True


def fail_pending_scene_commands(message: str) -> None:
    for pending in tuple(pending_scene_commands.values()):
        if not pending.acknowledgement.done():
            pending.acknowledgement.set_exception(ConnectionError(message))


async def set_game(scene_id: str, timeout_seconds: float = COMMAND_TIMEOUT_SECONDS) -> bool:
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
            f"scene '{acknowledgement.get('sceneId')}'."
        )
        return False

    error_code = acknowledgement.get("errorCode") or status or "unknown_error"
    error_message = acknowledgement.get("errorMessage") or "Unity did not apply the scene change."
    log(f"[Server] Scene change to '{scene_id}' failed ({error_code}): {error_message}")
    return False
    
@app.get("/api/health")
async def health():
    return {
        "status": "ok"
    }

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

@app.websocket("/ws/ctrl")
async def commands(ws: WebSocket):
    global unity_ws
    
    await ws.accept()
    unity_ws = ws

    log("[Server] Unity connected")

    try:
        while True:
            message = await ws.receive_text()
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                log("[Server] Ignoring malformed Unity ACK: invalid JSON.")
                continue

            handle_unity_acknowledgement(data)
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

            case _:
                try:
                    scene_id = parse_set_game_command(command)
                except ValueError as exception:
                    log(f"[Server] {exception}")
                    continue

                await set_game(scene_id)
                continue
            
async def main():
    global server
    
    config = uvicorn.Config(
        app,
        host="0.0.0.0", 
        port=8000, 
        reload=False, 
        access_log=False, 
        use_colors=False
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
