# Adaptive Lan dialogue (opt-in, `lan-adaptive-v1`)

Status: implemented behind a flag, default off. Legacy sessions and the legacy writer request (220 tokens, reasoning off, 4 s) are unchanged.

## What changes

In the legacy flow, `plan_reply` maps each classified utterance to a fixed intent (for example every `refusesRemedy` turn becomes `refusal_challenge`). Qwen then only paraphrases that intent. In an adaptive session, one `qwen/qwen3.7-flash` request per turn chooses **how Lan reacts**: the action, whether to continue or close, and the cited fact IDs. The same request **drafts her words**. Code still owns everything that affects facts, policy, limits and scoring.

| Owner | Responsibilities |
|---|---|
| Jev/Luna + `sales_rubric`/`sales_concerns` (unchanged) | labels, evidence ledger, disclosures, rubric credit, score, hard endings, turn cap |
| `app/sales_adaptive.py` (code) | eligible actions, which facts may/must be stated, whether an early close is permitted, guards, fallback, action memory |
| Qwen (model) | one action from `allowedActions`, `continue`/`close`, `customerText`, `factIds` |

The model never sees or writes scores, ratings, labels' probabilities or ledger fields. The action it chooses never touches the evidence ledger.

## Enabling

| Variable | Default | Meaning |
|---|---|---|
| `SALES_ADAPTIVE_DIALOGUE_ENABLED` | `false` | New `openrouter` sessions freeze `adaptiveDialogue` at creation |
| `SALES_ADAPTIVE_REASONING` | `off` | `off` → `reasoning.enabled=false`; `on` → `enabled=true` |
| `SALES_ADAPTIVE_MAX_TOKENS` | `4096` | shared completion cap (reasoning + visible output) |
| `SALES_ADAPTIVE_REASONING_MAX_TOKENS` | `1024` | `reasoning.max_tokens` when reasoning is on; `0` omits it |
| `SALES_ADAPTIVE_TIMEOUT_SECONDS` | `45` | deadline for all attempts of one turn |

The frozen session config (`version`, `model`, `reasoning`, `maxTokens`, `reasoningMaxTokens`, `timeoutSeconds`, `temperature=0.4`) is authoritative. Changing the environment later never converts an existing session in either direction. An unknown `adaptiveDialogue.version` fails the turn with `pipeline_version_unsupported`. Provider routing uses the existing `SALES_OPENROUTER_DATA_COLLECTION`/`SALES_OPENROUTER_ZDR` preferences. Requests use `response_format={"type":"json_object"}` because the endpoint advertises `response_format` but not strict structured outputs. The schema is validated locally.

## Turn flow

1. STT, Jev, Luna and `evaluate()` run exactly as before. They produce the candidate state, the decision and the ledger entry.
2. `plan_reply()` and `select_customer_line()` still run. Their guarded line is the **fallback**; the legacy intent is kept only as diagnostics (`adaptiveAction.legacyIntent`).
3. `turn_policy()` derives the code limits:
   - `allowedActions` from recognized acts:
     - `challenge_refusal` only after a refusal or withdrawn exchange;
     - `reject_promise` only with a promise or violation;
     - `request_respect` only for bad conduct;
     - `defer_physical` only for physical requests;
     - `voice_trust_doubt` only once the challenge exists;
     - `close_conversation` only when an early close is permitted.
   - A challenge-type action already used twice in a row is withdrawn. Lan must change approach or leave instead of looping.
   - Fact limits: `requiredFactIds` (what Jev says was asked) must be stated and cited. Cited IDs must be shared or released now.
4. `model_context()` sends Lan the following:
   - store policy, purchased shoe, shared facts and facts released this turn;
   - the IDs of facts Lan does not know;
   - every committed prior turn, the current utterance, recognized acts and the open concern/hint (advisory);
   - limits, allowed actions, recent action memory and recent Lan replies.

   Private facts that are not yet released are withheld.
5. `AdaptiveLanResponder.respond()` sends one request. On schema or guard rejection it makes at most one repair request, with codes only: the invalid output is never echoed back. A transport error (5xx/429/network) gets one same-payload retry instead. Everything shares one deadline.
6. Validation (`output_rejections`) combines the legacy text guard, re-aimed at the chosen action and decision, with adaptive checks:
   - action allowed; decision matches action; `must_close` / `close_not_allowed`;
   - `uncitable_fact` / `missing_cited_fact`;
   - `fabricated_store_action` (a store, manager or salesperson "already did/approved/will" do something);
   - for soft closes, `missing_closure` and `unsupported_satisfaction`.

   Existing guards still reject the following:
   - refunds, discounts, compensation and guarantees;
   - invented periods or extra exchange conditions;
   - unallowed or contradicted facts, and unsupported details;
   - completed inspections or trials, and physical actions;
   - repetition, wrong address, length, and unplanned goodbyes.
7. Invalid output, timeout or provider failure falls back to the deterministic line and the deterministic continue/close. It never retries the turn or creates a bad rating. `adaptiveAction.source` is `model` or `fallback`, with `fallbackReason`.
8. The adaptive record and reply are checkpointed together (`turnCheckpoints[turnId].adaptive/written`), so a resubmitted turn never repeats the paid request. The compact `adaptiveMemory` (last 8 `{turn, action, decision, factIds, source}`) is written **only** inside the turn commit. It is hidden from public session responses.

## Endings: what stays authoritative

| Ending | Owner | Adaptive behaviour |
|---|---|---|
| `turn_limit`, `manager_escalation`, `maintained_unauthorized_promise`, `exchange_accepted`, `objectives_completed`, `stalemate` (ledger-derived), review endings | code | Lan **must** close (`allowedActions=["close_conversation"]`). The model only words it within the code-chosen tone (`ending_*` guard). It cannot extend the dialogue. |
| `second_silence` | code | handled before any model call (fixed text) |
| trust challenge opening (`challengeShownNow`) | code | not an ending; Lan must voice it (`voice_trust_doubt`) because scoring now expects a response |
| `customer_closed` (new) | model, within code limit | Lan may leave early when `closeAllowed`: no deterministic ending, the turn is not uncertain, no trust challenge opens now, turns remain, and the trailing turns without progress (bad conduct or no progress) are ≥3, or ≥2 when the current turn is bad. Close reason is `unresolved_frustration` or `needs_time`. |

Scoring is unchanged. `customer_closed` is a session-level ending like `stopped_early`/`turn_limit`: not in `FORCED_ENDINGS`, so trust follows the ledger. A legacy and an adaptive session with the same labels get identical scores, components, violations and trust; only `endingReason` differs (see `test_early_close_changes_ending_but_not_deterministic_score`). The deterministic concern/hint ladder still advances in the ledger and is shown to the model as advice. It no longer selects Lan's reaction in adaptive sessions.

## Benchmark (generation-only replay)

`scripts/benchmark_sales_adaptive.py` replays the 10 discovery snapshots and times only Lan's response generation. **It is not an end-to-end gameplay measurement:** STT, Jev/Luna and TTS are excluded.

The arms are:

- `adaptive_off` and `adaptive_on`: identical prompt, schema, context, `temperature=0.4`, `max_tokens=4096`, 45 s deadline and provider pin (`order=[alibaba]`, `allow_fallbacks=false`, `require_parameters=true`, plus the configured data-collection/ZDR preferences). Only `reasoning` differs: `{"enabled": false}` versus `{"enabled": true, "max_tokens": 1024}`. Each sample records payload SHA-256 values excluding `reasoning` to prove identity.
- Optional `--include-legacy` adds `legacy_220_off`, the production plan+writer path. Its prompt, architecture and budget differ, so it is not part of the causal comparison.

Ordering is serial, case-interleaved and seeded. Each case alternates AB/BA across its 3 repeats, and the case order is reshuffled per repeat. That gives 60 adaptive samples, or 90 with the legacy arm. There are at most 2 HTTP attempts per sample.

Each sample records:

- wall-clock ready-to-use seconds, which include attempts, guards and fallback;
- first-attempt latency and the latency of every request;
- per-attempt HTTP status, served model, provider, request ID and finish reason;
- prompt, completion, reasoning, cached and total tokens;
- `reasoningChars` (a length only — reasoning text is never stored);
- provider `usage.cost` (`null` when unknown, never 0);
- rejection codes, outcome (`model`/`fallback`/`harness_error`), action, decision and Lan's text.

### Replay reconstruction (`scripts/sales_adaptive_replay.py`)

The discovery manifest's `sessionMemorySnapshot` is the **final** session state and is never used. Each case is rebuilt from its source file:

- **Session:** a fresh current-version session (current rubric, scenario and memory rules).
- **Memory:** the source's immutable facts, with `shared` initially reset unless `sharedTurnId` is opening or part1. Disclosures from prior turns are added at their original turn during replay, so earlier ledger entries cannot know later prefix facts. Sessions saved without memory (SA-02, SA-06, SA-07, SA-10) are initialized by the current rules and Part 1-linked from their attempt file, with provenance recorded. SA-05 keeps its authentic unlinked memory.
- **Prior turns:** each one goes through `evaluate()` with its ledger labels (post-arbitration) or, failing that, its turn labels. Labels missing from an older question set are set to `false` and listed.
  - Pre-v2 boolean labels (SA-06, SA-10) are mapped by `LEGACY_BOOLEAN_MAP`.
  - If a prior turn would end the dialogue under current rules even though the saved conversation continued (SA-10 turn 1: generous legacy keyword labels), its credit-only labels are dropped. Conduct and safety labels are kept, and both are recorded in provenance.
- **Disclosures:** reconciled to what Lan actually said (`disclosedFactIds`/`volunteeredFactIds`).
- **Action memory:** the prefix's action memory is derived from the current planner's intents.
- **Selected turn:** evaluated with its own labels, since classification precedes Lan's reply. The historical reply and every later turn are excluded.
- **Checks:** each run verifies source SHA-256 before and after, 10 distinct sessions, and a leakage check covering the selected reply, new future text, private IDs and scoring fields.

**SA-01 amendment.** Saved turn 8 is the forced `turn_limit` turn and contains a spoken complaint about the app. The effective manifest uses turn 6 (`8fa9657a2b17451b9fbe26a2b7cdcff0`) of the same session instead: the fourth consecutive no-receipt refusal, with 2 turns remaining. The discovery files are not modified. `effective_manifest.json` records the amendment.

### Commands (PowerShell, from `D:\NCKH\NCKH-AI`)

```powershell
.\.venv\Scripts\python.exe -m pytest -q app/tests/test_sales_adaptive.py app/tests/test_sales_adaptive_benchmark.py
# No network/key/cost: rebuild cases, verify hashes/identity, simulated transport end to end.
.\.venv\Scripts\python.exe scripts\benchmark_sales_adaptive.py dry-run --include-legacy
# Paid: 60 adaptive + 30 legacy samples (at most 180 requests). Reads OPENROUTER_API_KEY from env/.env.
.\.venv\Scripts\python.exe scripts\benchmark_sales_adaptive.py live --include-legacy --confirm-paid
# Continue an interrupted live run (same flags/seed): finished samples are skipped.
.\.venv\Scripts\python.exe scripts\benchmark_sales_adaptive.py live --include-legacy --confirm-paid --resume D:\NCKH\output\sales-adaptive-benchmark\runs\live-<stamp>
```

Outputs go to `D:\NCKH\output\sales-adaptive-benchmark\`:

- `effective_manifest.json`;
- `runs/<mode>-<UTC stamp>/` containing:
  - `config.json`: arm configs, checks and schedule;
  - `cases/SA-xx.json`: provenance, policy, legacy plan and exact model context;
  - `samples.jsonl`;
  - `summary.json` and `summary.md`;
  - `grader_reference.json`: historical replies for blinded review only.

## Limitations

- Fixed-prefix, one-turn replay. It does not show how a player would answer the new reply. A free-running multi-turn check is still needed.
- Guards are conservative keyword/regex checks. False rejections fall back safely but reduce adaptivity; true semantic violations may slip past them.
- Lan cannot volunteer a private fact until a recognized question releases it. This keeps the ledger and memory consistent.
- Older sessions are replayed under current rules with mapped or absent labels. Their states are approximations, documented per case.
- A 45 s deadline is far longer than the legacy 4 s writer. Real-time suitability depends on the measured latency.
- Unity receives the new `endingReason` value `customer_closed` with `conversationComplete=true`. The flow matches other endings, but no headset or Unity test has been run.
