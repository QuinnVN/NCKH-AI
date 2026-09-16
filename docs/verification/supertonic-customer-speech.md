# Supertonic customer-speech verification

Use this checklist after installing the pinned Supertonic sidecar with `ai setup supertonic`.

## Automated checks

- Run `py -m unittest app.tests.test_ai_servers app.tests.test_sales_returning_api app.tests.test_tts_api`.
- Confirm `/api/health` reports the TTS configuration fields without changing the normal readiness result.
- With the optional backend token enabled, confirm both `/api/tts` and the sales speech URL require it.
- Verify `/api/tts` returns raw `audio/wav`, rejects text above 600 characters, and reports stable unavailable, timeout, busy, and invalid-audio categories.
- Verify a sales speech URL accepts no caller-supplied dialogue and resolves only the opening or authoritative session turn text.

## Manual Unity checks

- In the Windows Editor and Android headset, begin the returning-customer conversation and confirm that Lan's subtitle is hidden while the complete WAV downloads, then appears when playback starts.
- Confirm the audio source is on Lan, is spatialized, and remains audible at the configured hearing range.
- Confirm the microphone starts only after normal customer speech ends; verify the same behavior for scripted, generated, silent, and terminal replies.
- Stop Supertonic with `ai stop supertonic`; confirm the subtitle remains visible and A acknowledges fallback before recording or terminal completion.
- Simulate a brief request failure and confirm one retry occurs. Verify scene exit, reset, and headset pause cancel a pending download or playback, and restoring a session does not replay a completed line.
- Record synthesis plus download latency and ensure the end-to-end timeout remains within eight seconds. Do not retain generated audio or spoken text in diagnostics while doing so.
