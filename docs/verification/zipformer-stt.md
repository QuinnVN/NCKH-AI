# Zipformer speech-recognition verification

The migration comparison ran on 14 September 2026 with `sherpa-onnx` 1.13.8 and `sherpa-onnx-zipformer-vi-30M-int8-2026-02-09`. It used two private, human-transcripted runtime recordings without copying them into the repository. Each Zipformer file ran ten times after model initialization.

| Recording | Duration | PhoWhisper CER | Zipformer CER | Zipformer warm p95 | RTF |
| --- | ---: | ---: | ---: | ---: | ---: |
| Lawyer defense | 30.040 s | 0.1627 | 0.1475 | 1.485 s | 0.0494 |
| Sales persuasion | 20.220 s | 0.1672 | 0.1185 | 0.915 s | 0.0453 |
| Mean | | 0.1650 | 0.1330 | | |

Zipformer passed the required maximum RTF of 0.25, produced no empty result or timeout, improved mean normalized character error rate, and retained the required word `camera`. The available recordings did not contain `tivi`, `size`, `cỡ`, or the product codes `1A`, `3B`, and `8A`, so this run makes no accuracy claim for those terms. The benchmark starts with greedy decoding; add hotword biasing only if a later transcripted recording demonstrates a critical term failure.

Run the comparison with `scripts/benchmark-stt.py`. It requires explicit paths to the retired PhoWhisper executable and model because those assets are no longer part of the application.
