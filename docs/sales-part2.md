# Sales Part 2 backend

The `sale` scene links Part 1 and Lan's returning-customer conversation with a pseudonymous `sessionId`. The backend stores the authoritative conversation, phase, accepted-turn count, silence count, and final evaluation. Customer replies and final analysis use separate constrained LLM requests.

The final analysis scores two criteria from 0 to 50 each. `apologyAndPolicyRemedy` measures whether the player apologizes or acknowledges the disappointment and offers a complete remedy allowed by store policy. `adaptabilityAndDeescalation` measures relevant questioning, adaptation to the customer's answers, calm objection handling, and de-escalation. Their sum is stored as `rawScore`. Each recorded store-policy violation deducts 10 points, down to zero, and the final value is stored as `score`. The result also stores `policyViolationPenalty` and the violation list.

- More `good` turns than `bad` turns: `customerRating: good` and `trustState: restored`.
- More `bad` turns than `good` turns: `customerRating: bad` and `trustState: lost`.
- Equal counts: `customerRating: considering` and `trustState: partially_restored`.

The terminal customer line follows the same counts. A good majority uses one of the return-intent lines, a bad majority uses one of the rejection or manager-escalation lines, and a tie uses the consideration line. Two silences without a rated turn remain a lost-trust outcome.

For each turn, the model returns semantic evidence flags for the player's transcript. The backend derives the final `good` or `bad` rating from those flags. A polite response is good when it includes apology plus remedy, explanation plus corrective advice, or a reasonable policy-compliant return policy. Rude or condescending speech, profanity, apology without a solution, asking the customer to keep trying the product without an explanation, and repeating a customer question already asked in a recent player turn are bad. The model receives the four most recent player transcripts to compare questions, including paraphrases. The backend owns Lan's fixed replies, including the second missing-policy prompt, apology-only prompt, and abusive-employee warning. It also derives `activeObjective`, `objectiveCompleted`, `conversationComplete`, and `deterministicEnding` from the current session and fixed gameplay rules.

## Configuration

- `LLM_BASE_URL` and `LLM_MODEL`: existing local llama.cpp service and model alias. The model must support the supplied JSON response schema and Vietnamese dialogue. Model quality still needs a live evaluation.
- `SHERPA_MODEL_DIR`, `SHERPA_NUM_THREADS`, and `SHERPA_TIMEOUT_SECONDS`: local sherpa-onnx Zipformer model location and bounded CPU inference settings. The controlled research sessions accept Vietnamese speech only and do not run automatic language identification.
- `RECORDINGS_DIR`: existing diagnostic storage directory.
- `SALES_RETENTION_DAYS`: default 30, configurable from 1 to 3650. Cleanup runs at startup, every hour, and on session-state reads. It removes expired Part 2 audio and transcripts, and linked Part 1 diagnostics. Tombstones prevent retries from recreating deleted data.
- `BACKEND_API_TOKEN`: optional existing gameplay bearer token. When enabled, provide it to the Unity request service at runtime through `BearerToken`; do not serialize credentials into scenes.
- `SUPERTONIC_BASE_URL`: loopback URL for the separately managed Supertonic sidecar. It defaults to `http://127.0.0.1:7788`.
- `TTS_TIMEOUT_SECONDS`: end-to-end speech deadline, including the one-request queue. It defaults to 8 seconds.
- `DISABLE_AI_SALE_PT2`: set to `true` to omit speech metadata and disable TTS routes. Sales responses continue to include customer text.
- `SALES_DIAGNOSTIC_TOKEN`: separate bearer token required for diagnostic deletion. Deletion is disabled when this value is absent.

Run one backend worker for this file-backed store. Atomic file replacement protects completed turns across process restarts; per-session locks serialize concurrent requests within the worker. Multiple workers sharing the directory require a transactional store or cross-process locking.

## HTTP contract

| Method and route | Request / behavior |
| --- | --- |
| `POST /api/sales/sessions` | `{ "sessionId": "...", "part1AttemptId": "..." }`; omit the optional Part 1 ID until uploaded. Creates or resumes without resetting progress. |
| `GET /api/sales/sessions/{sessionId}` | Current phase, counts, last customer text, and completion status; excludes stored conversation and turn caches. |
| `GET /api/sales/sessions/{sessionId}/speech/{speechId}` | Resolves an opaque speech ID to authoritative Lan text, synthesizes a complete WAV in memory, and returns `audio/wav`. It never accepts caller text. |
| `POST /api/tts` | Internal bounded text-to-speech endpoint. Accepts `{ "text": "..." }` with at most 600 characters and returns a complete WAV. |
| `POST /api/sales/sessions/{sessionId}/turns` | `turnId`, `clientVersion`, `retryCount`, and `audio` with `mimeType`, `encoding`, `sampleRateHz`, `channels`, `dataBase64`. Audio is mono PCM16 WAV at 16 kHz. |
| `POST /api/sales/sessions/{sessionId}/complete` | `completionId` and `reason` (`natural`, `time_limit`, or `turn_limit`). Returns criterion scores, `rawScore`, the policy-violation list and penalty, `score`, good/bad counts, `customerRating`, `trustState`, and `finalCustomerText`. Final states are immutable; retrying returns the existing result. |
| `DELETE /api/sales/sessions/{sessionId}/diagnostics` | Requires the diagnostic bearer token. Deletes Part 2 and linked Part 1 audio/transcripts and retains a deletion audit. |

Retry a failed turn with the same `turnId` and identical audio. A repeated accepted turn returns its cached response without incrementing counters. Changed audio under an existing ID is rejected. Silence does not consume an accepted turn. Infrastructure failures return a sanitized failure category, never a failed-performance result. Four accepted turns request final analysis. Profanity and insults receive a bad rating, a warning reply, and a recorded policy violation, but the conversation continues. Two valid silences, manager escalation, or a maintained unauthorized promise end with lost trust.

Unless `DISABLE_AI_SALE_PT2=true`, each opening, turn, and terminal customer line includes `speech` metadata with an opaque `speechId`, a sales speech URL, WAV format, and `available` state. The backend holds no generated audio after sending its response. It permits one synthesis and one queued request. A third request receives `429` with `tts_busy`; unavailable sidecars use `tts_unavailable`, the shared deadline uses `tts_timeout`, and invalid sidecar WAV data uses `tts_invalid_audio`. These details do not enter sales results or diagnostics.

Diagnostic records include audio, transcript, phase, retry count, format, processing duration, and available model metadata. The LLM server version is marked unavailable because its API does not provide a verified version. Raw diagnostics stay in the backend store; they must not be copied into general telemetry or logs.

## Validation

Run `.venv\Scripts\python.exe -m unittest discover -s app/tests -q`.

Tests cover strict contracts, ordered phases, gated fact disclosure, the mandatory trust challenge, deterministic endings, final-state caching, concurrent deletion, token isolation, linked Part 1 retention, and the Unity-shaped HTTP payload. These use deterministic speech/model substitutes; they do not establish live transcription accuracy or LLM dialogue quality.
