# Sales customer memory verification, 2026-10-08

The latest recorded run linked a Part 1 purchase of light Adudu shoes for morning running and occasional walking to a separate Part 2 profile. Part 2 kept the attempt ID but did not load its needs. Recall did not count as investigation evidence, so the planner continued asking for walking information that the player had already heard.

## Completed changes

- Validate explicit customer, session and run continuity before inheriting Part 1 needs, budget and product.
- Store authored facts with source, certainty and first disclosure turn; preserve opening and volunteered disclosures.
- Answer receipt, purchase, condition, socks, previous-shoe, product, budget, packaging, medical-history and duration questions.
- Accept grounded recall and factual confirmation; keep unknown facts unknown.
- Remember inspection/trial requests and spoken consent separately from completed actions; remember exchange offers and withdrawal.
- Update Jev, Luna, writer, fallback and privacy/deletion handling. Linked purchases no longer require a heavy-shoe diagnosis.
- Add pipeline, HTTP and Unity regressions; freeze the new version tuple without rewriting stored runs.

## Checks

All backend tests use the credential-isolated runner, which blocks live HTTP. Model adapters use `httpx.MockTransport`; dialogue tests inject STT/classifier/writer fakes.

```powershell
# D:\NCKH\NCKH-AI
.venv/Scripts/python.exe -X utf8 scripts/run_tests.py -v

# D:\NCKH
NCKH-AI/.venv/Scripts/python.exe -X utf8 output/replay-sales-opening-context.py --linked-part1

# D:\NCKH\NCKH-VR
powershell -ExecutionPolicy Bypass -File ./Build.ps1 -Target PlayModeTests -TestFilter 'NCKH.VR.Sales.Tests.SalesPart2FlowTests'
powershell -ExecutionPolicy Bypass -File ./Build.ps1 -Target EditModeTests -TestFilter 'NCKH.VR.Sales.Tests'
```

The final backend suite passed all 443 tests. Unity passed 48 PlayMode and 15 EditMode tests. The real recorded opening/recall trace, replayed with validated Part 1 context, no longer requests walking information again. The recordings remain unchanged.

The checks cover deterministic state, replay, retries, payloads and guarded speech. No live Jev/Luna/Qwen conversation, APK build or physical-headset run was performed. Restart the backend and use a Unity build containing the continuity fields to test a new run; an existing APK does not send those fields. Older active version tuples remain unsupported instead of silently changing assessment rules.

## Follow-up: run 65d40d66 (session 3de47b1e)

- The session stored `continuesPart1Customer: false` although APK alpha_13 set the flag. `SalesPart2RequestService` copied the start request into a wire object without `customerId` and `continuesPart1Customer`, so Part 1 facts were never linked and Lan kept saying the walking need had not been asked. The wire now carries both fields; an EditMode test checks the serialized JSON.
- Turn 3 asked Lan to hand over her shoes. No label covered a hand-over, so Lan ignored it and repeated her concern. `physicalActionRequest` (question set `sales-jev-v5`, writer `lan-writer-v5`) and `physical_deferral` now make Lan say she did not bring the shoes and will bring them back, then continue.
- `output/replay-sales-physical-deferral.py` replays the stored labels with Part 1 linked (`physicalActionRequest` simulated on turns 2-3): turns 2-3 defer the action and no turn claims the walking need is missing.
- Backend: after the `pipeline_mode` rename was finished, the full suite passed all 449 tests (`output/sales-physical-suite.log`), and the replay above gives the same result on the live tree.
- Unity: not run; the editor had the project open, so batch mode refused to start.
- The opening no longer says "đôi giày này", so it does not contradict Lan saying she did not bring the shoes: "Em ơi, chị muốn đổi đôi giày chị mới mua ở đây ba ngày trước. Mang vào thì chị bị đau chân. Lần trước em tư vấn đôi đó phù hợp với chị nên chị khá thất vọng." It is changed in the backend constant (used for TTS), the Unity asset, the scenario default, the authoring command, the EditMode assertion and the specs.
