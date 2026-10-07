---
status: accepted
---

# Score returning-customer evidence on the whole ledger, not per objective

Lan now reacts to four customer concerns that can be resolved in any order, so rubric components are no longer tied to the objective active when the player spoke. Each component is credited once when its context condition holds on the evidence ledger: an early apology or a policy-compliant exchange offer counts when it is said, while a cause statement, a lightweight recommendation and a trial still require that Lan has disclosed the relevant facts first. One pure function folds the ledger to produce every turn rating, the completion outcome, the completion review and offline rescoring, so these cannot disagree. This amends one sentence of ADR 0003 ("Each rubric component is earned once in its objective context"); its other decisions stand: the backend owns progression and scores, and Qwen's wording never creates a score or an ending.

## Consequences

Scores under sales-rubric-v3 are not directly comparable with v2.x, because early acts now earn credit. The offline rescoring command reports the difference on the same recordings without changing stored results. The objective shown in VR is derived from the lowest open concern and rises at most one step per turn, so it can lag the real state for a few turns.
