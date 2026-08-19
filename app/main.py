import asyncio
from contextlib import asynccontextmanager
import threading
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout
import uvicorn

server: uvicorn.Server | None = None
unityws: WebSocket | None = None

app = FastAPI()
session = PromptSession()

def log(str):
    print_formatted_text(str)


async def handleCommands():
    while True:
        try: 
            command: str = (await session.prompt_async("> ")).strip()

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
                if unityws is not None:
                    log("[Server] Unity is connected.")
                else:
                    log("[Server] Unity is not connected.")
                continue
            
            case "exit":
                log("[Server] Exiting...")
                 
                if server is not None:
                    server.should_exit = True
            
            case _:
                log("[Server] Unknown command")
                continue
                
            
            
async def main():
    global server
    
    config = uvicorn.Config(
        "app.main:app", 
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
    global unityws
    
    await ws.accept()
    unityws = ws
    
    log("[Server] Unity connected")
    
    try:
        while True:
            data = await ws.receive_json()
            log(f"[Server] Received command: {data}")      
    except WebSocketDisconnect:
        log("[Server] Unity disconnected")
        unityws = None   

async def send_command_to_unity(command: dict[str, Any]):
    if unityws is None:
        log("[Server] No active Unity connection.")
        return
    
    await unityws.send_json(command)

            