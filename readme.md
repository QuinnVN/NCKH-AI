# Unity VR backend

This repository contains the Python backend for a Unity VR training project. It accepts telemetry and recorded voice clips from Unity, exposes an optional OpenAI-compatible LLM response endpoint, and maintains a control WebSocket that lets the backend request a scene change and wait for Unity to acknowledge it.

The sales endpoints perform speech-to-text with an embedded sherpa-onnx recognizer and the pinned Vietnamese Zipformer 30M INT8 model. The general `/api/ai/respond` route still accepts a client-provided transcript. The interactive operator console installs the speech model and supervises the local llama.cpp server.

## Architecture and data flow

```text
Unity VR client
  ├─ POST /api/telemetry ──► Lawyer WAV + immutable attempt ──► background STT/assessment
  ├─ POST /api/sales/persuasion-recordings ──► atomic WAV + attempt record ──► background STT/assessment
  ├─ POST /api/ai/respond ─► bounded request ─► local OpenAI-compatible LLM
  ├─ POST /api/ai/initial-career-assessment ─► categorized questionnaire ─► up to 5 career suggestions
  └─ WS /ws/ctrl ◄────────── load_scene, start_scene commands / ACKs

Operator console ─────────── set_game <scene_id> ─► WS /ws/ctrl ─► Unity
```

Participant-linked results are accepted at `POST /api/results/fragments`.
The operator assigns identity with `user <name>`, queries it with `user`, or
clears it with `user clear`. A `run.started` fragment creates a run snapshot;
subsequent fragments use stable `fragmentId` values and `data` fields, and a
fragment with `complete: true` finalizes one immutable aggregate. Use
`db sync` to retry unsynchronized aggregates. Local drafts, finalized JSON,
WAV/processing records, and synchronization sidecars are retained indefinitely.

`app/main.py` owns the FastAPI routes, the single active Unity control connection, command sequencing, and the small operator console. `app/ai_servers.py` owns llama.cpp preflight, startup, status, and shutdown. `app/sherpa_stt.py` owns the serialized in-process recognizer, while `app/sherpa_setup.py` verifies and installs its model bundle. `app/save_recording.py` validates the recording contract and writes files with a temporary file followed by an atomic replace. `app/llm_service.py` is a bounded, error-normalizing HTTP client. `app/config.py` centralizes environment-driven settings.

`app/sales_persuasion.py` owns the sales recording contract and attempt store. `app/lawyer_assessment.py` provides the equivalent durable workflow for the Lawyer closing defense, including bounded case context, four scoring criteria, and the interview-restart penalty. A successful submission means the WAV and JSON attempt record have been persisted; it does not wait for transcription or assessment. Reusing an attempt ID with the same payload is idempotent, while changed immutable data returns 409.

The liveness endpoint is `GET /api/health`. It always reports `status: "ok"` when the process is serving; `ready` and `llmConfigured` indicate whether an LLM service was initialized. `GET /api/health/ready` returns 503 until the LLM is configured. Unity connectivity is reported as `unityConnected`. TTS fields report whether Supertonic is configured, has recently produced a valid WAV, and requires the backend bearer token. TTS does not affect general readiness.

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
    "interviewRestartCount": 4,
    "assessmentContextJson": "{\"caseSummary\":\"...\",\"investigationObjective\":\"...\",\"defenseConclusion\":\"...\",\"orderedEvidence\":[...],\"reasoningCards\":[...],\"sampleAnswers\":[...]}",
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

The recording must be a non-empty PCM WAV with signed 16-bit samples, 16,000 Hz, mono audio, and at most 8 MiB by default. `roundId` must be exactly 32 hexadecimal characters. The assessment context contains only the configured case summary, objective, defense conclusion, ordered linked evidence, reasoning cards, and positive sample answers. Files are written atomically as `recordings/lawyer-defense-<lowercase-roundId>.wav` plus a JSON attempt record. An identical retry is idempotent; changing the audio, context, or restart count under the same round ID returns 409. Invalid recording events return 422; storage failures return 500.

The background worker detects silence, transcribes Vietnamese speech, and scores `evidence_use` out of 40, `logical_connections` out of 35, `conclusion_fidelity` out of 15, and `clarity_and_persuasiveness` out of 10. The raw total is preserved. Zero through three interview restarts have no penalty; four or more apply one 50% deduction to the raw total, retaining half points. `GET /api/lawyer/defense-recordings/<roundId>` exposes only gameplay-safe processing status. The diagnostic endpoint at the same path plus `/diagnostic` returns the detailed result only when `X-Diagnostic-Token` matches `LAWYER_DIAGNOSTIC_TOKEN`.

Unity sales uploads use the same telemetry route with `eventType: "sales.persuasion_recording"`. Its payload uses `roundId`, `questionId`, `caseId`, `cardId`, a JSON-string `resolution` containing `selectedShoe`, `bestFitShoe`, `customerNeeds`, `objection`, and `availableShoes`, plus the WAV `audio` object. The adapter validates this envelope and maps it to the sales attempt contract below. Unity audio metadata such as `fileName`, `durationSeconds`, and `endedEarly` is accepted but does not replace WAV validation.

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

### `POST /api/sales/persuasion-recordings`

This endpoint accepts one immutable sales attempt. The JSON body contains `attemptId`, `scenarioId`, `customerId`, `selectedShoeId`, `bestFitShoeId`, `customerNeeds`, `objection`, `availableShoes`, and a mono 16-bit PCM WAV in `audio.dataBase64`. Each shoe has `shoeId`, `name`, `price`, and optional `details`. The selected and best-fit IDs must reference the supplied shoe list. The recording must be 16,000 Hz and is subject to `MAX_RECORDING_BYTES`.

The endpoint returns `status: "accepted"`, the attempt ID, and `assessmentStatus: "processing"` after it persists the audio and attempt association. A background worker checks for silence, transcribes non-silent audio with Zipformer, and asks the configured LLM for a score from 0 to 100 plus brief Vietnamese feedback. Silence completes with score 0 and an explanation. STT or model errors complete with `assessmentStatus: "failed"` and an error code, never with a fabricated zero. `GET /api/sales/persuasion-recordings/<attemptId>` reads the stored status and result.

The service requeues attempts left in `processing` when it starts again. A shutdown cancels active workers without deleting their durable records, so the next start can resume them.

### `POST /api/ai/initial-career-assessment`

This endpoint accepts up to 28 categorized questionnaire dimensions, then uses Qwen3-4B thinking mode to generate 1–5 career suggestions. Interests are the main signal, while abilities, traits, and other dimensions provide supporting information. Each suggestion contains a Vietnamese career name and an independent estimated match percentage. The website does not send candidate careers, and suggestions are not limited to available VR simulations. Thinking content is not returned or logged. The breaking request and response contract, migration checklist, validation rules, and JSON Schemas are documented in [`docs/initial-career-assessment-api.md`](docs/initial-career-assessment-api.md).

## Unity control protocol

Unity opens one authenticated connection to `WS /ws/ctrl`. The token can be supplied as the `Authorization: Bearer <token>` header or the `token=<token>` query parameter. A newer connection replaces the older one, and pending commands belonging to the old connection fail rather than accepting an ACK from the wrong client.

The operator console accepts `set_game <scene_id>`, where the scene catalog is `standby`, `clinic`, `doctor`, `lawyer`, and `sale`. For a gameplay scene, the backend sends `load_scene`, waits for a `ready` acknowledgement, then sends `start_scene` and waits for a `running` acknowledgement. Standby requires only `load_scene`. The console also accepts `reset <scene_id|all>` for gameplay scenes. `reset standby` is rejected, and `all` is only valid for reset. Use `test_llm` to send a fixed smoke-test prompt to the configured language model and print either its response or a safe failure message. Use `ai setup`, `ai setup stt`, or `ai setup supertonic` to install speech dependencies. Use `ai start`, `ai status`, `ai stop`, or `ai restart` with `all`, `llama`, or `supertonic` to manage local model services. The remaining commands are `status` and `exit`. The first command is:

```json
{
  "commandId": "<uuid hex>",
  "sequence": 1,
  "type": "load_scene",
  "sceneId": "doctor",
  "issuedAtUtc": "2026-08-31T00:00:00Z"
}
```

After Unity acknowledges the gameplay scene with phase `ready`, the backend sends:

```json
{
  "commandId": "<new uuid hex>",
  "sequence": 2,
  "type": "start_scene",
  "sceneId": "doctor",
  "issuedAtUtc": "2026-08-31T00:00:01Z"
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

Unity should acknowledge the same `commandId` and `sequence` with `status: "applied"` and the applied `sceneId`, or with `status: "rejected"` or `status: "failed"` plus `errorCode` and `errorMessage`:

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
| `AI_THINKING_SALE_PT2` | `true` | Enables thinking for Sales Part 2 customer responses. Set to `false` to request no thinking. |
| `MAX_CAREER_PROMPT_CHARS` | `32000` | Maximum size of an individual career-assessment prompt message. |
| `MAX_SALES_PROMPT_CHARS` | `24000` | Maximum size of an individual sales-assessment prompt message. |
| `MAX_LAWYER_PROMPT_CHARS` | `32000` | Maximum size of the bounded Lawyer assessment prompt. |
| `MONGODB_URI` | unset | Optional MongoDB URI; unset keeps results local-only. |
| `MONGODB_DATABASE` | `desmap` | MongoDB database for completed aggregates. |
| `MONGODB_RESULTS_COLLECTION` | `game_results` | MongoDB collection for completed aggregates. |
| `LAWYER_DIAGNOSTIC_TOKEN` | unset | Token required by the detailed Lawyer diagnostic result endpoint. |
| `LLAMA_SERVER_BIN` | `%LOCALAPPDATA%\Microsoft\WindowsApps\llama.exe` | Unified llama.cpp executable used by the operator console. The manager invokes its `serve` subcommand. |
| `AI_SERVER_START_TIMEOUT_SECONDS` | `180` | Time allowed for each managed server to open its local port. |
| `AI_SERVER_SHUTDOWN_TIMEOUT_SECONDS` | `15` | Time allowed for a managed server to exit before it is force-closed. |
| `SHERPA_MODEL_DIR` | `models/sherpa-onnx-zipformer-vi-30M-int8-2026-02-09` | Directory containing the verified encoder, decoder, joiner, and token files. |
| `SHERPA_NUM_THREADS` | `1` | CPU threads used by the serialized recognizer, capped at 16. |
| `SHERPA_TIMEOUT_SECONDS` | `20` | Maximum time awaited for one transcription, capped at 600 seconds. |

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

On a fresh checkout, install the pinned speech model from the operator console:

```text
ai setup
```

For a non-interactive deployment, run `scripts/setup-zipformer.ps1` before starting Uvicorn. Both paths verify the release archive and every installed model file by SHA-256. Repeating setup reports that the model is already installed.

Run the focused tests with:

```powershell
py -m unittest discover -s app/tests -p "test_*.py"
```

From the interactive console, start llama.cpp with:

```text
ai start
```

This opens llama.cpp and, when selected, Supertonic in separate Windows console windows. `ai start` preflights each selected service and waits for its port. Supertonic listens on `127.0.0.1:7788`. Use `ai status`, `ai stop`, or `ai restart` to inspect or control either service. A failed Supertonic launch does not stop a llama.cpp process that already started.

The llama executable path comes from `LLAMA_SERVER_BIN`. If that variable is unset, the manager derives `%LOCALAPPDATA%\Microsoft\WindowsApps\llama.exe`. The manager uses llama.cpp's `-hf` option, so the first run may download `Qwen/Qwen3-4B-GGUF:Q4_K_M`. The backend sends `LLM_MODEL` (default `qwen3-4b`) as the OpenAI-compatible model field, so it must match the server alias.

`ai stop` sends a stop request only to the llama process started by the current operator console. It force-closes the process only if it remains alive after 15 seconds. Entering `exit`, pressing Ctrl+C, or ending the backend also runs the same shutdown before the operator process exits.

Verify llama.cpp independently with:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/models
```

`/api/health/ready` requires non-empty LLM configuration and a loaded Zipformer recognizer. It does not probe llama.cpp. A stopped or misconfigured LLM server is detected on the first response request. If the speech model is absent, the backend and operator console still start, readiness returns 503, and `ai setup` can install and initialize it.

## Security and operational notes

Keep the default loopback bind unless Unity runs on another machine. If remote access is required, set `BACKEND_API_TOKEN`, use a firewall or private network, and terminate TLS at a trusted reverse proxy. The optional token is a shared secret, not a replacement for transport security. Do not put tokens in command history or source control.

Recording payloads are base64-encoded JSON and therefore consume more memory than raw uploads. The decoded size and serialized telemetry size are bounded. Files contain potentially sensitive voice data; protect `RECORDINGS_DIR` and apply retention outside this service.

## Current limitations and model status

- Sales STT accepts Vietnamese speech only. It does not identify or reject other spoken languages because research sessions use selected Vietnamese-speaking candidates.
- The backend currently supports one active Unity control WebSocket. A new connection replaces the previous one.
- Recording persistence is local filesystem storage; there is no database, object storage, cleanup policy, or cross-process idempotency lock.
- LLM readiness means that configuration exists, not that the upstream model server has passed a live health check. The first response request verifies availability.
- The Zipformer bundle is not stored in Git. Run `ai setup` or `scripts/setup-zipformer.ps1` on each deployment.
