# Unity VR backend

This repository contains the Python backend for a Unity VR training project. It accepts telemetry and recorded voice clips from Unity, exposes an optional OpenAI-compatible LLM response endpoint, and maintains a control WebSocket that lets the backend request a scene change and wait for Unity to acknowledge it.

The sales endpoints perform speech-to-text with an embedded sherpa-onnx recognizer and the pinned Vietnamese Zipformer 30M INT8 model. The general `/api/ai/respond` route still accepts a client-provided transcript. The interactive operator console installs the speech model and manages the selected local Qwen3-4B runtime.

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

`app/main.py` owns the FastAPI routes, the single active Unity control connection, command sequencing, and the small operator console. `app/ai_servers.py` manages llama.cpp or the Lemonade hybrid model, plus Supertonic. `app/sherpa_stt.py` owns the serialized in-process recognizer, while `app/sherpa_setup.py` verifies and installs its model bundle. `app/save_recording.py` validates the recording contract and writes files with a temporary file followed by an atomic replace. `app/llm_service.py` is a bounded, error-normalizing HTTP client. `app/config.py` centralizes environment-driven settings.

`app/sales_persuasion.py` owns the sales recording contract and attempt store. `app/lawyer_assessment.py` provides the equivalent durable workflow for the Lawyer closing defense, including bounded case context, four scoring criteria, and the interview-restart penalty. A successful submission means the WAV and JSON attempt record have been persisted; it does not wait for transcription or assessment. Reusing an attempt ID with the same payload is idempotent, while changed immutable data returns 409.

The liveness endpoint is `GET /api/health`. It always reports `status: "ok"` when the process is serving; `ready` and `llmConfigured` indicate whether an LLM service was initialized. `GET /api/health/ready` returns 503 until the LLM is configured. Unity connectivity is reported as `unityConnected`. TTS fields report whether Supertonic is configured, has recently produced a valid WAV, and requires the backend bearer token. TTS does not affect general readiness.

## HTTP API

Sales returning-customer sessions also support an opt-in OpenRouter pipeline.
Inject `OPENROUTER_API_KEY` into this backend and choose `SALES_PIPELINE_MODE`
as `legacy`, `shadow`, or `openrouter` for newly created sessions. The default
is `legacy`. Jev recognizes acts, backend rules score them, Qwen writes Lan's
planned response, and Supertonic remains local. Existing sessions preserve
their frozen versions. New sessions use v3, in which Lan reacts to customer concerns and one ledger scorer decides every rating and result ([verification](docs/verification/sales-customer-state-v3-2026-10-05.md)). See [Sales v2 verification and rollout](docs/verification/sales-openrouter-v2.md)
for retry behavior, rubric, independent review tooling and remaining live checks.

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

Unity sales uploads use the same telemetry route with `eventType: "sales.persuasion_recording"`. Its payload uses `roundId`, `questionId`, `caseId`, `cardId`, a JSON-string `resolution` containing `selectedShoe`, `bestFitShoe`, `customerNeeds`, `objection`, and `availableShoes`, plus the WAV `audio` object. The adapter validates this envelope and maps it to the sales attempt contract below. Unity audio metadata such as `fileName`, `durationSeconds`, and `endedEarly` is accepted but does not replace WAV validation. Sales Part 2 completion scores apology plus policy-compliant remedy out of 50 and adaptability plus de-escalation out of 50. Each store-policy violation deducts 10 points. In the legacy pipeline the final customer rating and `trustState` come from whether good turns outnumber bad turns, bad turns outnumber good turns, or the counts are tied. OpenRouter sessions compute them from the evidence ledger instead; see [Sales Part 2](docs/sales-part2.md).

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

The operator console accepts `set_game <scene_id>`, where the scene catalog is `standby`, `clinic`, `doctor`, `lawyer`, `sale`, and `tutorial`. For a gameplay scene, the backend sends `load_scene`, waits for a `ready` acknowledgement, then sends `start_scene` and waits for a `running` acknowledgement. Clinic and Doctor load acknowledgements can also report `running` when the scene has already started automatically; the follow-up start command remains idempotent. Standby requires only `load_scene`. The console also accepts `reset <scene_id|all>` for gameplay scenes. `reset standby` is rejected, and `all` is only valid for reset. Use `test_llm` to send a fixed smoke-test prompt to the configured language model and print either its response or a safe failure message. Use `ai setup`, `ai setup stt`, or `ai setup supertonic` to install speech dependencies. Use `ai start`, `ai status`, `ai stop`, or `ai restart` with `all`, `llama`, `hybrid`, or `supertonic` to manage local model services. With `SALES_PIPELINE_MODE=openrouter`, `all` selects only Supertonic. In `legacy` or `shadow` mode, `all` selects the LLM runtime chosen by `LLM_USE_AMD_HYBRID`, together with Supertonic. Explicit targets still select the named service in every mode. The remaining commands are `status` and `exit`. The first command is:

```json
{
  "commandId": "<uuid hex>",
  "sequence": 1,
  "type": "load_scene",
  "sceneId": "doctor",
  "issuedAtUtc": "2026-08-31T00:00:00Z"
}
```

After Unity acknowledges an allowed gameplay load phase, the backend sends:

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
| `LLM_USE_AMD_HYBRID` | `false` | Set to `true` in `.env` to run backend Qwen3-4B through Lemonade's NPU + iGPU hybrid model. Restart the backend after changing it. |
| `LLM_BASE_URL` | `http://127.0.0.1:8080/v1` | llama.cpp base URL when hybrid mode is off; also used by the final-evaluation CLI even when the backend hybrid flag is on. |
| `LLM_MODEL` | `qwen3-4b` | llama.cpp model alias when hybrid mode is off. Hybrid mode uses `Qwen3-4B-Hybrid` at `http://127.0.0.1:13305/v1` automatically. |
| `LLM_READ_TIMEOUT_SECONDS` | `120` | LLM response timeout, capped at 600 seconds. |
| `LLM_MAX_TOKENS` | `100` | Maximum completion tokens, capped at 2,048. |
| `LLM_CAREER_MAX_TOKENS` | `4096` | Completion budget for thinking plus career JSON, capped at 8,192. |
| `MAX_CAREER_PROMPT_CHARS` | `32000` | Maximum size of an individual career-assessment prompt message. |
| `MAX_SALES_PROMPT_CHARS` | `24000` | Maximum size of an individual sales-assessment prompt message. |
| `MAX_LAWYER_PROMPT_CHARS` | `32000` | Maximum size of the bounded Lawyer assessment prompt. |
| `MONGODB_URI` | unset | Optional MongoDB URI; unset keeps results local-only. |
| `MONGODB_DATABASE` | `desmap` | MongoDB database for completed aggregates. |
| `MONGODB_RESULTS_COLLECTION` | `game_results` | MongoDB collection for completed aggregates. |
| `OPENROUTER_API_KEY` | unset | Private OpenRouter key used by the final-evaluation writer and Sales v2 adapters. |
| `FINAL_EVALUATION_MAX_PROMPT_CHARS` | `16000` | Maximum system prompt plus facts for one generated text field. |
| `FINAL_EVALUATION_MAX_TOKENS` | `1024` | Completion limit for one generated text field, bounded to 128–2,048. |
| `FINAL_EVALUATION_REQUEST_TIMEOUT_SECONDS` | `45` | Timeout for each OpenRouter request, bounded to 5–120 seconds. |
| `LAWYER_DIAGNOSTIC_TOKEN` | unset | Token required by the detailed Lawyer diagnostic result endpoint. |
| `LLAMA_SERVER_BIN` | `%LOCALAPPDATA%\Microsoft\WindowsApps\llama.exe` | Unified llama.cpp executable used by local backend tasks when hybrid mode is off. |
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

For the interactive console (including `set_game`, `reset`, `test_llm`, and the text-only Sales Part 2 test command), run:

```powershell
py -m app.main
```

`start_server.ps1` runs the same backend with the repository's virtual environment. When launched without an attached terminal, it serves the API without the operator console.

### Generate a final career evaluation

Run the interactive CLI:

```powershell
.\.venv\Scripts\python.exe .\scripts\generate_final_evaluation.py
```

The CLI sends final-assessment writing requests directly to OpenRouter with `deepseek/deepseek-v4.1-flash` and reasoning disabled. Configure `OPENROUTER_API_KEY` in the backend process or this repository's `.env`; the key is never sent to the browser. No local model or Python HTTP backend needs to be running for this CLI. `LLM_BASE_URL`, `LLM_MODEL`, `LLM_USE_AMD_HYBRID`, and the old `FINAL_EVALUATION_LLM_MODEL` / `FINAL_EVALUATION_LLM_START_TIMEOUT_SECONDS` settings do not control it. The old `--no-start-llm` option is accepted as a no-op for command compatibility.

The OpenRouter adapter retries a temporary rate limit or provider/network failure once, with at most a three-second delay. Authentication and credit failures return safe error messages. Invalid or truncated completions stop generation before any incomplete assessment is saved. Provider safety annotations are removed from participant text, and providers are restricted to routes that deny data collection. No other model is silently substituted. Existing validated text-field repair and career-evidence checks still apply.

The CLI reads participant names from `questionnaire_submissions`. Choose a number for one person or `all` for everyone, or pass `--participant "Nguyễn Văn An"` or `--all` on the command line. Both modes skip people already present in `final_evaluations` before calling DeepSeek. The CLI combines all completed `game_results` for each person into one evaluation; repeated runs of one game are averaged, and `doctor` and `clinic` are treated as two tasks in the Doctor experience. Use `--dry-run` to print new assessments without writing MongoDB.

Python calculates dimension levels, DESMAP scores and observed VR rubric scores. The final-career calculation uses `data/career-catalog.json`: 68 occupations whose profiles come from O*NET® 30.2 data (CC BY 4.0) through `data/onet-career-mapping.json`. The 22 non-Desire dimensions are matched by profile-shape correlation, and work values in D are compared with each occupation's O*NET work values rather than treated as ability. VR contributes at most 25%, scaled by the criteria actually observed. Only the selected interest groups are included, while `exploring` includes the full catalog. Careers are ranked at full precision before rounding. DeepSeek writes 3-4 short sentences for the first career and 2-3 for each alternative among the top seven careers; unsupported or negative descriptions are replaced with cited facts and a concrete exploratory activity. DeepSeek does not assign compatibility scores. Saved documents include `careerRankingVersion`, `generationProvider`, and `generationModel` so the web and operators can identify current prose and its source. See the web repository's `docs/career-ranking.md` for the formula and MongoDB audit. Refresh O*NET data with `python scripts/build_onet_extract.py --version 30.2`, export the catalog with `python scripts/build_career_catalog.py --web-root <NCKH-Web path>` (run from the repository root with `PYTHONPATH=.`), and increase the version when criteria change. Keep the O*NET attribution from `data/onet-extract.json` wherever the results are shown.


The read-only model benchmark uses the same final-evaluation generator and exports deidentified inputs, request costs, timing, raw prose and aggregate checks. Run `scripts/benchmark_final_evaluation.py prepare` and `run` with a new output directory under `recordings`; results do not change production model settings or MongoDB. The measured comparison of DeepSeek V4.1 Flash, Qwen3.8 Flash and MiMo V2.6 Flash is in [the final-evaluation benchmark report](docs/verification/final-eval-benchmark-2026-10-04/report.md).

Use `test <text>` to send one typed player response through the Sales Part 2
LLM responder. It bypasses speech recognition and does not create or alter a
gameplay session. For example:

```text
test Em xin lỗi chị, em sẽ kiểm tra độ vừa và mời chị thử đôi nhẹ hơn.
```

On a fresh checkout, install the pinned speech model from the operator console:

```text
ai setup
```

For a non-interactive deployment, run `scripts/setup-zipformer.ps1` before starting Uvicorn. Both paths verify the release archive and every installed model file by SHA-256. Repeating setup reports that the model is already installed.

Run the focused tests with:

```powershell
py scripts/run_tests.py
```

The test runner skips `.env`, uses a dummy OpenRouter key and temporary recordings,
disables MongoDB, and rejects live HTTP requests. Tests can still use `MockTransport` and
`ASGITransport`. To run a single module, use
`py scripts/run_tests.py app.tests.test_sales_pipeline_api -v`.

From the interactive console, start the local services selected by the pipeline mode with:

```text
ai start
```

With `SALES_PIPELINE_MODE=openrouter`, `ai start` starts only Supertonic, even when `LLM_USE_AMD_HYBRID=true`. Other simulations still use their configured local LLM; start it explicitly with `ai start hybrid` or `ai start llama` when needed, and use the same explicit target to inspect or stop it. In `legacy` or `shadow` mode, with hybrid mode off, `ai start` opens llama.cpp and Supertonic in separate Windows console windows. With hybrid mode on, it loads `Qwen3-4B-Hybrid` into the already installed Lemonade service with a 16K context; `ai stop` unloads that model but leaves Lemonade running. Supertonic listens on `127.0.0.1:7788`. Use `ai status`, `ai stop`, or `ai restart` to inspect or control the services selected by the pipeline mode. Restart the backend after changing the pipeline mode. A failed Supertonic launch does not stop an LLM that already started.

The llama executable path comes from `LLAMA_SERVER_BIN`. If that variable is unset, the manager derives `%LOCALAPPDATA%\Microsoft\WindowsApps\llama.exe`. The manager uses llama.cpp's `-hf` option, so the first run may download `Qwen/Qwen3-4B-GGUF:Q4_K_M`. The backend sends `LLM_MODEL` (default `qwen3-4b`) as the OpenAI-compatible model field in this mode.

For hybrid mode, install [Lemonade Server](https://lemonade-server.ai/docs/guide/install/) and its `ryzenai-llm:npu` backend on a compatible Ryzen AI Windows PC. Pull `Qwen3-4B-Hybrid` with `lemonade pull Qwen3-4B-Hybrid`, then set `LLM_USE_AMD_HYBRID=true` in `.env` and restart the backend. `ai start hybrid` loads the model; `ai status hybrid` checks that Lemonade reports the Ryzen AI hybrid checkpoint with a 16K context. The backend uses Lemonade on `127.0.0.1:13305` and reports an error if it is unavailable. No automatic llama.cpp fallback occurs while the flag is on.

In llama.cpp mode, `ai stop` sends a stop request only to the llama process started by the current operator console. It force-closes that process only if it remains alive after 15 seconds. Entering `exit`, pressing Ctrl+C, or ending the backend also stops the selected managed services before the operator process exits.

Verify the selected model endpoint independently with:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/v1/models
# Hybrid mode:
Invoke-RestMethod http://127.0.0.1:13305/v1/health
```

`/api/health/ready` requires non-empty LLM configuration and a loaded Zipformer recognizer. It does not probe the model server. A stopped or misconfigured LLM server is detected on the first response request. If the speech model is absent, the backend and operator console still start, readiness returns 503, and `ai setup` can install and initialize it.

## Security and operational notes

Keep the default loopback bind unless Unity runs on another machine. If remote access is required, set `BACKEND_API_TOKEN`, use a firewall or private network, and terminate TLS at a trusted reverse proxy. The optional token is a shared secret, not a replacement for transport security. Do not put tokens in command history or source control.

Recording payloads are base64-encoded JSON and therefore consume more memory than raw uploads. The decoded size and serialized telemetry size are bounded. Files contain potentially sensitive voice data; protect `RECORDINGS_DIR` and apply retention outside this service.

## Current limitations and model status

- Sales STT accepts Vietnamese speech only. It does not identify or reject other spoken languages because research sessions use selected Vietnamese-speaking candidates.
- The backend currently supports one active Unity control WebSocket. A new connection replaces the previous one.
- Recording persistence is local filesystem storage; there is no database, object storage, cleanup policy, or cross-process idempotency lock.
- LLM readiness means that configuration exists, not that the upstream model server has passed a live health check. The first response request verifies availability.
- The Zipformer bundle is not stored in Git. Run `ai setup` or `scripts/setup-zipformer.ps1` on each deployment.
