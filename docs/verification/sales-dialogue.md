# Sales dialogue live verification

Run on 2026-09-29 with the configured `Qwen3-4B-Hybrid` service at `127.0.0.1:13305`. The test called `LLMSalesResponder.respond` sequentially with fixed Vietnamese player transcripts and in-memory session states. It measured wall-clock time around each call, including JSON parsing and backend validation. It did not include STT, TTS, disk persistence, or the HTTP API.

Eleven hand-written scenarios checked apology with remedy and an open question, apology alone, abuse, a paraphrased repeated question, a new pain-onset question, use and fit investigation, cause explanation with advice, policy-compliant exchange, an unauthorized refund promise, explicit denial of a refund promise, and trust rebuilding. Each scenario asserted the relevant rating, flags, disclosed facts, or objective transition.

| Measure | Result |
| --- | ---: |
| Scenarios passing their assertions | 11/11 |
| LLM requests | 11, no retries |
| Median latency | 4.54 s |
| 90th-percentile latency, nearest rank | 6.17 s |
| Observed range | 3.43–7.21 s |
| Total sequential time | 53.74 s |

The first nine-scenario run exposed a missed paraphrased repeat and an unasked color fact. Backend grounding corrected those cases. Review of the following run found that a valid cause explanation and trust-rebuilding answer were still rated `bad`; the hybrid classifier now recognizes explanation with corrective advice. The final eleven-scenario run above passed all specified checks.

These are smoke-test results for one local model run, not an estimate of accuracy on real player speech. Live sessions may have different STT wording, model outputs, and hardware load.
