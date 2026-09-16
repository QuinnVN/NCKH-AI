# Supertonic 3 customer speech

## Problem Statement

The returning-customer conversation in the sales simulation presents Lan's Vietnamese replies only as text. The participant cannot hear the customer, and the interaction moves toward microphone recording without waiting for customer speech. The project needs complete spoken replies on the Android VR headset while preserving the authoritative text, the turn-based conversation model, and subtitle-only operation when speech synthesis is unavailable.

## Solution

Add customer speech backed by a separately managed local Supertonic 3 server. The backend will expose an internal text-to-speech API and a sales-specific speech route that resolves authoritative Lan dialogue by speech ID. It will obtain a complete WAV from Supertonic, validate and buffer the result in memory, and send it to Unity without retaining generated audio. Unity will download the complete clip, reveal the subtitle when playback begins, play the clip from Lan's position, and prevent player recording until playback ends. Failures will preserve the text and require the participant to press A before continuing.

## User Stories

1. As a participant, I want to hear Lan's opening complaint, so that the returning-customer conversation begins as spoken dialogue.
2. As a participant, I want every scripted Lan line to be spoken, so that customer speech does not disappear on deterministic branches.
3. As a participant, I want every generated Lan reply to be spoken, so that the dynamic conversation remains audible.
4. As a participant, I want warnings and final lines to be spoken, so that important state changes are not delivered only as text.
5. As a participant, I want Lan's voice to originate from her position, so that the conversation fits the VR scene.
6. As a participant, I want the subtitle to appear when speech begins, so that I can read while listening.
7. As a participant, I want the subtitle to remain visible while I respond, so that I can refer back to Lan's statement.
8. As a participant, I want a visible waiting state while speech is prepared, so that the application does not appear frozen.
9. As a participant, I want the microphone disabled while Lan speaks, so that her generated voice is not captured as my response.
10. As a participant, I want recording to begin only after Lan finishes, so that conversation turns do not overlap.
11. As a participant, I want a text fallback when speech synthesis fails, so that I can continue the simulation.
12. As a participant, I want to acknowledge fallback text with A, so that I control when recording begins after reading.
13. As a participant, I want a failed terminal line to finish after I acknowledge its text, so that the simulation does not start an extra recording.
14. As a participant, I want a completed terminal clip to finish before the conversation closes, so that Lan is not cut off.
15. As a participant, I want a transient speech failure retried once, so that a brief network problem does not immediately remove speech.
16. As a participant, I want a retried turn to play speech if I never heard it, so that a lost response does not make Lan silent.
17. As a participant, I do not want completed speech replayed after a retry or session restore, so that Lan does not unexpectedly repeat herself.
18. As a participant, I want speech work cancelled when I leave or restart the scene, so that dialogue from an old session cannot play in a new one.
19. As an operator, I want one command to set up speech recognition and synthesis, so that a fresh research computer is reproducible.
20. As an operator, I want setup targets for speech recognition and Supertonic, so that I can repair one installation without reinstalling everything.
21. As an operator, I want the existing AI lifecycle commands to manage llama.cpp and Supertonic together or separately, so that local services share one workflow.
22. As an operator, I want Supertonic to open in a visible console, so that model-loading and startup faults can be inspected.
23. As an operator, I want llama.cpp and the backend to remain usable when Supertonic fails, so that TTS does not block the research session.
24. As an operator, I want explicit TTS health fields, so that degraded speech can be diagnosed without changing overall backend readiness.
25. As an operator, I want TTS errors in the backend console without dialogue text, so that failures are traceable without duplicating participant content in logs.
26. As an operator, I want stable error categories, so that headset behavior does not depend on exception messages.
27. As a developer, I want a sales-specific speech route, so that Unity cannot replace authoritative Lan dialogue with caller-provided text.
28. As a developer, I want a bounded internal TTS route, so that other project features can request the same fixed voice without depending on sales sessions.
29. As a developer, I want complete WAV responses rather than Base64, so that the client avoids JSON expansion and extra decoding.
30. As a developer, I want generated audio discarded after delivery, so that TTS does not create a second retained audio dataset.
31. As a developer, I want bounded input, output, queueing, and latency, so that a faulty caller or sidecar cannot consume unbounded resources.
32. As a researcher, I want TTS diagnostics excluded from participant result aggregates, so that delivery infrastructure does not alter assessment data.
33. As a researcher, I want the same turn-based behavior in the Android headset and Windows Unity Editor, so that development behavior matches the research client.

## Implementation Decisions

- Customer speech covers every Lan line in the returning-customer conversation: the opening complaint, scripted warnings and challenges, generated replies, silence branches, and terminal lines. Part 1 customer dialogue is unchanged.
- The authoritative customer text remains the domain result. Customer speech is a derived delivery form and never replaces or mutates that text.
- Supertonic 3 runs as a separate HTTP sidecar through the final `supertonic[serve]` 1.3.1 package. The archived and unsupported status of the package and model is accepted for this research prototype.
- The package is installed in the backend's existing Python virtual environment. Setup pins version 1.3.1 and verifies the downloaded package and model assets. Runtime startup does not download missing assets.
- `ai setup` installs both speech-recognition and speech-synthesis assets. `ai setup stt` and `ai setup supertonic` provide targeted setup.
- `ai start`, `ai status`, `ai stop`, and `ai restart` support `all`, `llama`, and `supertonic`. Omitting the target or selecting `all` operates on both managed servers.
- A partial `ai start` failure keeps successfully started services running and reports the failed service. Shutdown allows the sidecar a short graceful period before terminating it.
- Supertonic opens in a separate visible Windows console and binds to `127.0.0.1`. The backend is the only intended sidecar client.
- The backend uses F4, Vietnamese language mode, four inference steps, normal speed, WAV output, and no expression tags. Callers cannot override these settings.
- Four inference steps are fixed. Technical acceptance requires a structurally valid WAV; subjective Vietnamese pronunciation quality does not block release.
- The internal general route is `POST /api/tts`. It accepts JSON containing text of at most 600 characters and returns raw `audio/wav` bytes.
- The general response includes headers for audio duration, sample rate, speech ID, and model version when available.
- The general route is an internal project API with no compatibility promise for third-party clients.
- The general route follows the backend's existing optional bearer-token policy. If no backend token is configured, it permits unauthenticated requests and starts without an additional warning.
- A sales turn or session response includes a `speech` object containing a stable speech ID, a backend URL, WAV format, and availability state.
- The sales route is `GET /api/sales/sessions/{sessionId}/speech/{speechId}`. It resolves text from the authoritative sales session and never accepts replacement text from Unity.
- Lan's opening complaint uses the same speech metadata and delivery path as later replies.
- A sales speech URL remains valid until its session expires. Reusing it may synthesize fresh audio because generated audio is not cached.
- The backend sends synthesis requests to the native Supertonic endpoint, obtains the complete WAV, validates it, and buffers it in memory before responding.
- Generated WAV data is not written to diagnostic storage, participant results, or logs. General TTS input text is not persisted or printed.
- The backend rejects a sidecar response larger than 10 MB and maps it to a stable invalid-audio failure.
- The synthesis coordinator permits one active request and one queued request. Further requests receive HTTP 429.
- The eight-second deadline begins when the backend receives the request and includes queue time, sidecar synthesis, validation, and response preparation.
- Invalid requests return 422, excess concurrency returns 429, unavailable TTS returns 503, and a deadline expiry returns 504. Unity receives stable categories such as `tts_unavailable`, `tts_timeout`, and `tts_invalid_audio`; exception details and local paths remain in backend logs.
- General backend readiness does not depend on TTS. Health responses add explicit configured, ready, and authentication-required TTS fields.
- TTS timing and errors remain operational diagnostics and do not enter participant result aggregates.
- Unity shows a neutral "Lan is responding..." state and withholds `customerText` while it downloads the complete WAV.
- When the WAV is ready, Unity reveals the subtitle and starts playback at the same time. The subtitle remains visible through playback and the participant's following recording.
- Unity plays speech through an AudioSource attached to Lan. It uses full spatial blending, logarithmic rolloff, no Doppler effect, and Inspector-configurable hearing distances.
- Unity does not enable or start microphone recording until playback completes. Customer speech cannot overlap player recording.
- The first release has no skip control, replay control, player interruption, lip synchronization, or expression animation.
- Unity treats WAV duration plus two seconds as the playback-completion guard. A stuck player stops, logs a failure, and enters subtitle fallback.
- Unity retries one failed speech request once. If the retry fails, it reveals the authoritative subtitle and prompts the participant to press A.
- Pressing A after fallback starts recording for a normal turn. For a terminal line, pressing A acknowledges the line and completes the conversation.
- Successful terminal speech completes the conversation only after playback ends.
- Unity tracks completed speech IDs in memory for the active sales session. It marks an ID completed only after playback finishes, does not replay completed IDs on turn retries, and never auto-plays speech while restoring session state.
- Leaving, restarting, pausing, or unloading the scene cancels downloads and playback. Callbacks associated with an obsolete session or controller state are ignored.
- Stopping the Supertonic service is the operator's feature-off control. No separate Unity feature toggle is added.
- Supported clients are the Android VR headset and the Windows Unity Editor.
- Documentation will update the sales specification, backend operations, verification checklist, glossary, and an accepted architecture decision record describing the sidecar, complete-file playback, and text fallback tradeoffs.

## Testing Decisions

- Tests assert externally visible behavior at the highest practical seam. They do not assert private helper calls, internal field layout, or a specific implementation of HTTP buffering.
- Backend API tests use a controlled fake Supertonic service and exercise the complete general TTS and sales speech contracts through HTTP.
- Sales API tests verify that speech metadata appears for the opening line and every accepted customer-text branch, including scripted warnings, generated replies, silence handling, and deterministic endings.
- Sales tests verify that speech IDs remain stable across idempotent turn retries and that session-state reads do not imply automatic playback.
- API tests verify the 600-character input bound, raw WAV response, metadata headers, 10 MB output bound, optional authentication behavior, and sanitized errors.
- Coordinator tests verify one active and one queued request, rejection of a third request, and an eight-second deadline that includes queue time.
- Failure tests cover invalid input, unavailable sidecar, sidecar error, malformed or oversized WAV, timeout, client cancellation, and shutdown during synthesis.
- Operator-command tests extend the existing managed-server test pattern to cover setup and lifecycle targets, partial startup, status reporting, visible-console launch, and graceful shutdown.
- Health tests verify that TTS fields reflect configured, ready, and authentication states without changing general readiness.
- Privacy tests verify that generated audio and arbitrary TTS text do not enter diagnostic storage, participant aggregates, or application logs.
- Unity Edit Mode tests use controlled request and audio-playback substitutes at the sales controller and request-service boundary.
- Unity tests cover the waiting display, hidden subtitle, reveal-at-playback behavior, microphone gating, one retry, A-button fallback, terminal-line completion, playback timeout, speech-ID deduplication, and cancellation of stale callbacks.
- Existing sales HTTP contract, idempotency, privacy, controller-state, and managed-server tests are the prior art. New coverage should extend those seams instead of creating parallel low-level test suites.
- Manual headset verification confirms complete WAV playback, spatial origin at Lan, useful hearing distance, subtitle timing, absence of microphone overlap, retry and fallback behavior, terminal-line completion, pause and scene-exit cancellation, and operation after `ai stop supertonic`.
- Manual verification records synthesis and download latency against the two-second target and eight-second deadline. Subjective pronunciation quality is observed but is not a release gate.
- Validation runs on the Android VR target and in the Windows Unity Editor. A Windows standalone build is not required by this specification.

## Out of Scope

- Customer speech for Sales Part 1.
- On-headset Supertonic inference or offline headset operation without the backend computer.
- Streaming or partial audio playback.
- Player interruption, barge-in, echo cancellation, or simultaneous recording and playback.
- Lip synchronization, mouth animation, gestures, or expression tags.
- Custom voices, voice cloning, per-session voice selection, or caller-controlled language, speed, steps, and format.
- Automatic adjustment above four inference steps when pronunciation is poor.
- Persistent generated-audio storage, audio replay controls, or playback history across application restarts.
- A stable third-party TTS integration contract.
- TTS-derived participant scoring, telemetry, or research-result fields.
- Supporting more than one active synthesis and one queued synthesis per backend process.

## Further Notes

- Supertonic's source and Python SDK are archived and receive no fixes, security patches, or official support. The implementation must keep sidecar details behind a replaceable backend client boundary.
- The model uses the OpenRAIL-M license, while the package code uses the MIT license. Setup and distribution documentation must preserve the applicable notices.
- Allowing unauthenticated access when no backend token is configured is a deliberate decision. Deployments that bind the backend beyond a trusted research network should configure the bearer token and transport protection.
- The current sales documentation explicitly excludes customer speech synthesis. It must be revised when this feature is implemented.
