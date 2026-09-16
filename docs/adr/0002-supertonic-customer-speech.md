# Use a local Supertonic sidecar for customer speech

## Status

Accepted

## Decision

The backend calls Supertonic 1.3.1 through a loopback HTTP sidecar. It resolves sales speech IDs to the customer text stored in the sales session, receives a complete WAV, validates it in memory, and immediately sends it to Unity. Generated audio and arbitrary general TTS text are never stored.

The backend allows one active synthesis and one queued request under one eight-second deadline. Sidecar failures use stable error codes so Unity can display the authoritative subtitle and wait for acknowledgement.

## Consequences

The operator installs the pinned package with `ai setup supertonic` and starts its visible console with `ai start supertonic`. `ai start all` manages llama.cpp and Supertonic together, but a failed sidecar does not stop llama.cpp. The headset must download the full WAV before it begins playback. This adds latency but avoids partial playback and makes fallback behavior deterministic.
