import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
import uvicorn

server: uvicorn.Server | None = None
unity_ws: WebSocket | None = None

COMMAND_TIMEOUT_SECONDS = 5.0
VALID_SCENE_IDS = frozenset({"standby", "clinic", "doctor", "lawyer-office"})


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
