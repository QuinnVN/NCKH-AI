---
status: accepted
---

# Use Vietnamese Zipformer for local speech recognition

This non-commercial research system uses the offline `sherpa-onnx-zipformer-vi-30M-int8-2026-02-09` model on CPU instead of PhoWhisper. The selected-candidate environment guarantees Vietnamese input, so the backend deliberately reports `vi` and does not perform automatic language rejection. A transcripted local comparison found lower mean character error rate and lower latency than the previous recognizer. Model files are installed separately with pinned SHA-256 verification because they are runtime assets and use the CC BY-NC-ND 4.0 licence.
