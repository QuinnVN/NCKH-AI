# Sale Part 1 progress

- Coordinator: active Sol role; no extra Sol spawned, no user-owned chats.
- Exact Opus CLI launched: codex exec --model anthropic/claude-opus-5-5 --cd D:\NCKH-AI --ephemeral --json --output-last-message .scratch\sale-part1-sol-opus-20261005\01-red-result.txt -
- Stage 1 scope: deterministic RED regression only, no production fix; isolated process_sales_attempt -> LLMSalesAssessor -> saved score seam, no network.
- Part1 actual path confirmed: sales.persuasion_recording / POST persuasion-recordings -> store.accept -> main._queue_sales_processing -> process_sales_attempt -> LLMSalesAssessor -> persisted score -> project_sales_part1 / GET result.
- ADR 0003/0004 and sales_rubric/pipeline/review concern Part2. Keep their current changes untouched.
- Extensive pre-existing changes snapshotted as SHA256 baseline, preserve all unrelated user work.
- Worker startup has nonfatal websocket HTTP fallback and unavailable Unity MCP warnings. Waiting for model execution/result, do not assume successful model connectivity until execution events.
- Ranked hypotheses will be recorded here and in commentary after deterministic red repro, before testing them. Stage1 worker instructed to STOP after red.
New evidence: 15 saved Part1 attempts, 14 completed. Completed scores only 75 or 85. Transcripts of length 2 and 3 characters both scored 85. No transcript/auth/env contents printed. Worker created focused regression and is running it twice plus old test baseline. Event path: 01-red-events.jsonl; result path: 01-red-result.txt. Native send_input/spawn tools not exposed in this coordinator tool inventory; use this file for parent relay.

## Red loop verified by coordinator before hypotheses
Command: .\.venv\Scripts\python.exe -m unittest app.tests.test_sales_part1_scoring.SalesPart1UnrelatedAnswerScoringTests.test_unrelated_speech_cannot_earn_high_part1_score -v
Twice FAIL: AssertionError: 92 not less than or equal to 20. Actual Part1 process -> persisted record -> project_sales_part1, no network. unittest times 0.049s and 0.057s. Existing app.tests.test_sales_persuasion: 16 tests OK, 0.501s.
Worker CLI cannot launch base Python (Access denied; exit101 is NOT red evidence). Coordinator execution tools ran tests without permissions changes. Opus wrote the test, Sol only inspected and executed.

## Ranked hypotheses BEFORE further probes
1. Backend trusts raw upstream score without player evidence. Prediction: changing only mock score from 85 to 10 changes saved/assessed score even for identical unrelated transcript.
2. Judge credits scenario/customer/product facts as player acts. Prediction: requiring transcript quotes per rubric criterion rejects unsupported credit; live model cause cannot be conclusively tested by mocks alone.
3. Prompt truncation drops player speech. Prediction: even short unrelated repro transcript is missing in outgoing bounded prompt. Inspect exact message payload without printing secrets.
4. Projection/UI introduces inflated default. Prediction: projected score differs from stored score. Compare values at real seam.
No native send_input tool is available in ALL_TOOLS; parent can poll this file and 01-red-events.jsonl / 01-red-result.txt. Proceed without waiting for relay feedback.
