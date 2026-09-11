# Unity VR backend

This repository contains the Python backend for a Unity VR training project. It accepts telemetry and recorded voice clips from Unity, exposes an optional OpenAI-compatible LLM response endpoint, and maintains a control WebSocket that lets the backend request a scene change and wait for Unity to acknowledge it.

The backend does not currently perform speech-to-text. Unity (or another client) must send the transcript to `/api/ai/respond`; the Whisper, `whisper.cpp`, and PhoWhisper assets in this repository are not wired into the running FastAPI application. LLM inference is provided by a separately hosted local `llama-server` process.

## Architecture and data flow

```text
Unity VR client
  ├─ POST /api/telemetry ──► validation ──► atomic WAV file in recordings/
  ├─ POST /api/ai/respond ─► bounded request ─► local OpenAI-compatible LLM
  ├─ POST /api/ai/career-assessment ─► questionnaire ─► Qwen3-4B JSON result
  └─ WS /ws/ctrl ◄────────── load_scene command / ACK

Operator console ─────────── set_game <scene_id> ─► WS /ws/ctrl ─► Unity
```

`app/main.py` owns the FastAPI routes, the single active Unity control connection, command sequencing, and the small operator console. `app/save_recording.py` validates the recording contract and writes files with a temporary file followed by an atomic replace. `app/llm_service.py` is a bounded, error-normalizing HTTP client. `app/config.py` centralizes environment-driven settings.

The liveness endpoint is `GET /api/health`. It always reports `status: "ok"` when the process is serving; `ready` and `llmConfigured` indicate whether an LLM service was initialized. `GET /api/health/ready` returns 503 until the LLM is configured. Unity connectivity is reported as `unityConnected`.

## HTTP API

When `BACKEND_API_TOKEN` is set, telemetry and AI clients must send `Authorization: Bearer <token>`. Health endpoints remain unauthenticated for probes. Authentication is disabled when the variable is unset, which preserves the documented local-development behavior.

### `POST /api/telemetry`

Telemetry is a JSON object. Unknown event types are accepted as best effort for forward compatibility. A defense recording event has this shape (metadata fields may be extended by Unity):

```json
{
  "schemaVersion": 1,
  "eventId": "<event id>",
  "sessionId": "<session id>",
  "occurredAtUtc": "2026-08-31T00:00:00Z",
  "sceneId": "lawyer-office",
  "phase": "running",
  "eventType": "lawyer.defense_recording",
  "payload": {
    "roundId": "0123456789abcdef0123456789abcdef",
    "caseId": "case-id",
    "audio": {
      "fileName": "client-name.wav",
      "mimeType": "audio/wav",
      "encoding": "pcm_s16le",
      "sampleRateHz": 16000,
      "channels": 1,
      "durationSeconds": 1.25,
      "endedEarly": false,
      "dataBase64": "<base64 WAV bytes>"
    }
  }
}
```

The recording must be a non-empty PCM WAV with signed 16-bit samples, 16,000 Hz, mono audio, and at most 8 MiB by default. `roundId` must be exactly 32 hexadecimal characters. Files are written as `recordings/lawyer-defense-<lowercase-roundId>.wav` (or `RECORDINGS_DIR`) and the same round ID deterministically replaces the previous file. This makes retries idempotent and leaves no partial `.tmp` file after a successful write. Invalid recording events return 422; storage failures return 500.

### `POST /api/ai/respond`

The request is bounded and rejects unknown fields:

```json
{
  "transcript": "Tôi muốn hỏi về bằng chứng này.",
  "game_id": "lawyer",
  "conversation": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

`transcript` and each message are limited to 4,000 characters by default, and `conversation` is limited to 20 messages. The backend adds a game prompt, then sends a non-streaming request to `${LLM_BASE_URL}/chat/completions` using `LLM_MODEL`. Upstream timeouts/unavailability return 503; malformed or rejected upstream responses return 502. Upstream error details and full prompts are not returned to clients or logged.

### `POST /api/ai/career-assessment`

This endpoint accepts normalized questionnaire dimension scores and career criteria, then uses Qwen3-4B thinking mode to return an independent match percentage and Vietnamese evaluation for every requested career. Thinking content is not returned or logged. The request, response, system prompt, validation rules, and JSON Schemas are documented in [`docs/career-assessment-api.md`](docs/career-assessment-api.md).

## Unity control protocol

Unity opens one authenticated connection to `WS /ws/ctrl`. The token can be supplied as the `Authorization: Bearer <token>` header or the `token=<token>` query parameter. A newer connection replaces the older one, and pending commands belonging to the old connection fail rather than accepting an ACK from the wrong client.

The operator console accepts `set_game <scene_id>`, where the scene catalog is `standby`, `clinic`, `doctor`, and `lawyer`. It also accepts `reset <scene_id|all>` for gameplay scenes. `reset standby` is rejected, and `all` is only valid for reset. Use `test_llm` to send a fixed smoke-test prompt to the configured language model and print either its response or a safe failure message. The remaining commands are `status` and `exit`. The backend sends:

```json
{
  "commandId": "<uuid hex>",
  "sequence": 1,
  "type": "load_scene",
  "sceneId": "doctor",
  "issuedAtUtc": "2026-08-31T00:00:00Z"
}
```

A reset uses the same envelope with `type: "reset_scene"`. Unity accepts a specific reset only when that logical game is active; `all` is accepted from any scene. It loads Standby, clears the requested persistent gameplay state, and acknowledges only after Standby is active:

```json
{
  "commandId": "<uuid hex>",
  "sequence": 2,
  "type": "reset_scene",
  "sceneId": "all",
  "issuedAtUtc": "2026-09-10T00:00:00Z"
}
```

Successful reset acknowledgements report `sceneId: "standby"` and `phase: "standby"`. A specific inactive target is rejected with `scene_mismatch`. Standby itself is never reset, and reset does not restart the target game.

Unity should acknowledge the same `commandId` and `sequence` with `status: "applied"` and the applied `sceneId`, or `status: "rejected"` plus `errorCode` and `errorMessage`:

```json
{
  "commandId": "<uuid hex>",
  "sequence": 1,
  "status": "applied",
  "sceneId": "doctor",
  "phase": "ready"
}
```

ACKs with an unknown command, wrong sequence, invalid status, invalid scene ID, or another connection owner are ignored. Commands time out after five seconds by default.

## Configuration

All settings are optional. Defaults are local-only and safe for a developer workstation.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKEND_HOST` | `127.0.0.1` | Bind address; use `0.0.0.0` only when remote Unity access is intentional. |
| `BACKEND_PORT` | `8000` | FastAPI/uvicorn port. |
| `BACKEND_API_TOKEN` | unset | Optional shared token for HTTP and control WebSocket authentication. |
| `RECORDINGS_DIR` | `recordings` | Recording directory; relative paths resolve from the repository root. |
| `MAX_RECORDING_BYTES` | `8388608` | Maximum decoded WAV size, bounded to 1–64 MiB. |
| `MAX_TELEMETRY_BYTES` | `12582912` | Maximum serialized telemetry object size. |
| `MAX_TRANSCRIPT_CHARS` | `4000` | Maximum AI transcript length. |
| `MAX_MESSAGE_CHARS` | `4000` | Maximum conversation message length. |
| `MAX_CONVERSATION_MESSAGES` | `20` | Maximum conversation history items. |
| `COMMAND_TIMEOUT_SECONDS` | `5` | Unity command ACK timeout, capped at 60 seconds. |
| `LLM_BASE_URL` | `http://127.0.0.1:8080/v1` | Separately hosted llama.cpp `llama-server` OpenAI-compatible base URL. |
| `LLM_MODEL` | `qwen3-4b` | Model alias sent upstream; must match the server's `--alias` value. |
| `LLM_READ_TIMEOUT_SECONDS` | `120` | LLM response timeout, capped at 600 seconds. |
| `LLM_MAX_TOKENS` | `100` | Maximum completion tokens, capped at 2,048. |
| `LLM_CAREER_MAX_TOKENS` | `4096` | Completion budget for thinking plus career JSON, capped at 8,192. |
| `MAX_CAREER_PROMPT_CHARS` | `32000` | Maximum size of an individual career-assessment prompt message. |

Invalid numeric environment values fall back to their defaults; bounded values are clamped to safe ranges.

## Install, run, and test

Use Python 3.11+ and install the pinned runtime dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

Start the API and operator console from the repository root:

```powershell
py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

For the interactive console (including `set_game`, `reset`, and `test_llm`), run:

```powershell
py -m app.main
```

Run the focused tests with:

```powershell
py -m unittest discover -s app/tests -p "test_*.py"
```

The LLM endpoints require a separately running llama.cpp `llama-server`; the backend does not start or stop the model server. Start the configured Qwen3-4B Q4_K_M model with:

```powershell
.\scripts\run-qwen3-4b.ps1
```

The script calls `llama-server` from `PATH` and uses llama.cpp's `-hf` option, so its first run downloads `Qwen/Qwen3-4B-GGUF:Q4_K_M`. The backend sends `LLM_MODEL` (default `qwen3-4b`) as the OpenAI-compatible `model` field, so it must match the server alias. Verify the server independently before starting the backend:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/models
```

`/api/health/ready` only confirms that the backend has non-empty LLM configuration; it does not probe llama-server. A stopped or misconfigured llama-server is detected on the first `/api/ai/respond` request and reported as an upstream failure.

## Security and operational notes

Keep the default loopback bind unless Unity runs on another machine. If remote access is required, set `BACKEND_API_TOKEN`, use a firewall or private network, and terminate TLS at a trusted reverse proxy. The optional token is a shared secret, not a replacement for transport security. Do not put tokens in command history or source control.

Recording payloads are base64-encoded JSON and therefore consume more memory than raw uploads. The decoded size and serialized telemetry size are bounded. Files contain potentially sensitive voice data; protect `RECORDINGS_DIR` and apply retention outside this service.

## Current limitations and model status

- There is no active STT route or transcription worker. `/api/ai/respond` accepts a client-provided transcript only.
- The backend currently supports one active Unity control WebSocket. A new connection replaces the previous one.
- Recording persistence is local filesystem storage; there is no database, object storage, cleanup policy, or cross-process idempotency lock.
- LLM readiness means that configuration exists, not that the upstream model server has passed a live health check. The first response request verifies availability.
- `openai-whisper/` is a vendored source tree, `whisper.cpp` is a separate submodule/worktree, and `PhoWhisper-small/` plus `ggml-phowhisper-small.bin` are local model assets. They remain separate from the FastAPI runtime and may be large or untracked; do not assume they are deployable STT components until an explicit integration is added.
