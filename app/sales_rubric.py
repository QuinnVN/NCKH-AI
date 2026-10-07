"""Versioned session rules for returning-customer Sales.

Scoring and customer state live in the pure sales_concerns module. This module
freezes versions, normalizes Jev labels and turns one scored ledger entry into
the session fields the pipeline commits.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping
from app.sales_openrouter import (PENALTY_LABELS, QUESTION_TEXT, PIPELINE_VERSION, RUBRIC_VERSION,
    SCENARIO_VERSION, QUESTION_SET_VERSION, THRESHOLD_VERSION, PROMPT_VERSION, ARBITRATOR_VERSION)
from app.sales_concerns import (BAD_TURN_LABELS, COMPONENTS, RETRACTIONS, VIOLATIONS, lowest_open,
    score_ledger, score_session)

VERSIONS = dict(pipelineVersion=PIPELINE_VERSION, rubricVersion=RUBRIC_VERSION,
                scenarioVersion=SCENARIO_VERSION, questionSetVersion=QUESTION_SET_VERSION,
                thresholdVersion=THRESHOLD_VERSION, promptVersion=PROMPT_VERSION)
SAFETY = tuple(VIOLATIONS) + ("abuse", "maintainsUnauthorizedPromise") + tuple(RETRACTIONS)
ORDINARY = tuple(name for name in QUESTION_TEXT if name not in SAFETY)


def supports_versions(session: Mapping) -> bool:
    return all(session.get(name) == version for name, version in VERSIONS.items()) and (
        not session.get("lunaArbitrationEnabled") or session.get("arbitratorVersion") == ARBITRATOR_VERSION)


def normalized_labels(result: Any) -> dict[str, dict[str, Any]]:
    """Reject malformed values; never turn absent or unknown labels into false."""
    source = result.get("labels", {}) if isinstance(result, Mapping) else {}
    labels = {}
    for name in dict.fromkeys((*QUESTION_TEXT, *RETRACTIONS)):
        raw = source.get(name, {}) if isinstance(source, Mapping) else {}
        raw = raw if isinstance(raw, Mapping) else {}
        value = raw.get("noul")
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 and math.isfinite(value)
        high, low = (0.9, 0.1) if name in PENALTY_LABELS else (0.8, 0.2)
        status = "true" if valid and value >= high else "false" if valid and value <= low else "uncertain"
        # Thresholds are frozen by version; provider status cannot lower them.
        labels[name] = {"noul": value if valid else None, "status": status,
                        "questionId": raw.get("questionId", name),
                        "questionSetVersion": VERSIONS["questionSetVersion"],
                        "thresholdVersion": VERSIONS["thresholdVersion"]}
        for diagnostic in ("rawNoul", "responseIssue"):
            if diagnostic in raw:
                labels[name][diagnostic] = copy.deepcopy(raw[diagnostic])
        arbitration = raw.get("arbitration")
        if (raw.get("source") == "luna" and isinstance(arbitration, Mapping)
                and arbitration.get("version") == ARBITRATOR_VERSION
                and arbitration.get("status") in ("true", "false")
                and isinstance(arbitration.get("evidence"), str)):
            labels[name].update(status=arbitration["status"], source="luna",
                                jevStatus=status, arbitration=copy.deepcopy(dict(arbitration)))
        if not valid:
            labels[name].setdefault("responseIssue", "missing_answer" if not raw else "invalid_noul")
            if value is not None:
                # Preserve the provider value as diagnostic data, never evidence.
                labels[name].setdefault("rawNoul", str(value) if isinstance(value, float) and not math.isfinite(value) else value)
    return labels


def initialize(session: dict, settings: Any) -> None:
    mode = getattr(settings, "sales_pipeline_mode", "legacy")
    session.update(pipelineMode=mode, maxTurns=4 if mode != "openrouter" else getattr(settings, "sales_max_turns", 8),
                   assessmentStatus="pending", supplementalTurnGranted=False, evaluableTurnCount=0)
    if mode == "openrouter":
        session.update(VERSIONS)
        session.update(rubricComponents={}, objectiveEvidence={}, evidenceLedger=[], completedObjectives=[],
                       customerState={"resolvedConcerns": [], "statedConcern": 1,
                                      "hintLevels": {"1": 0, "2": 0, "3": 0, "4": 0}},
                       unresolvedPromises=[], clarificationCount=0, clarificationIssue=None,
                       dialogueTurnCount=0, assessmentReviewItems=[], resolutionAccepted=False,
                       jevDeadlineSeconds=getattr(settings, "sales_jev_timeout_seconds", 3),
                       writerDeadlineSeconds=getattr(settings, "sales_writer_timeout_seconds", 4),
                       lunaArbitrationEnabled=getattr(settings, "sales_luna_arbitration_enabled", True),
                       lunaDeadlineSeconds=getattr(settings, "sales_luna_timeout_seconds", 6),
                       arbitratorVersion=ARBITRATOR_VERSION)
    else:
        session.update(pipelineVersion="sales-legacy-v1", rubricVersion="sales-legacy-v1",
                       scenarioVersion="returning-shoes-v1", questionSetVersion="sales-local-v1",
                       thresholdVersion="sales-local-v1", promptVersion="sales-local-v1")
        if mode == "shadow":
            session["shadowVersions"] = dict(VERSIONS)


def remaining(session: Mapping) -> int:
    used = (session.get("dialogueTurnCount", session.get("evaluableTurnCount", 0)) if session.get("pipelineMode") == "openrouter"
            else session.get("acceptedTurnCount", 0))
    return max(0, int(session.get("maxTurns", 4)) - int(used))


def outcome(session: Mapping) -> dict:
    """Completion outcome computed from the evidence ledger alone."""
    return score_session(session)["outcome"]


def evaluate(session: Mapping, labels: Mapping, turn_id: str) -> tuple[dict, dict]:
    """Compute a candidate state. Only the turn commit makes it authoritative."""
    state = copy.deepcopy(dict(session))
    state["dialogueTurnCount"] = state.get("dialogueTurnCount", state.get("evaluableTurnCount", 0)) + 1
    previous = int(state.get("phase", 1))
    entry = {"turnId": turn_id, "transcriptRef": turn_id, "objective": previous,
             "labels": copy.deepcopy(dict(labels)), "contextVersion": SCENARIO_VERSION,
             "factsKnownBefore": sorted(state.get("investigationEvidence", [])),
             "unresolvedPromiseTypesBefore": [name for name, code in VIOLATIONS.items()
                                              if code in state.get("unresolvedPromises", [])],
             "challengeShownBefore": bool(state.get("challengeShown"))}
    ledger = list(state.get("evidenceLedger") or [])
    # Live turns use the stored labels only. Completion review decisions apply
    # at complete, so a later clear turn never reclassifies an earlier one.
    scored = score_ledger(ledger + [entry])
    turn, customer = scored["turns"][-1], scored["state"]
    resolved = set(scored["resolvedConcerns"])
    entry.update(disclosedFactIds=turn["disclosedFactIds"], statedConcern=turn["statedConcern"],
                 hintLevel=turn["hintLevel"], noProgress=turn["noProgress"], turnQuality=turn["turnQuality"])
    state["evidenceLedger"] = ledger + [entry]
    state.update(investigationEvidence=sorted(set(entry["factsKnownBefore"]) | set(turn["disclosedFactIds"])),
                 rubricComponents=scored["rubricComponents"], policyViolations=scored["policyViolations"],
                 unresolvedPromises=scored["unresolvedPromises"], completedObjectives=sorted(resolved),
                 challengeAnswered=4 in resolved)
    decision = {}
    if turn["assessmentUncertain"]:
        issue = ",".join(turn["criticalLabels"])
        previous_counts = state.get("clarificationIssues", {})
        issue_counts = {name: previous_counts.get(name, 0) + 1 for name in turn["criticalLabels"]}
        count = max(issue_counts.values())
        state.update(clarificationIssue=issue, clarificationIssues=issue_counts, clarificationCount=count,
                     assessmentStatus="needs-review" if count > 2 or session.get("assessmentStatus") == "needs-review" else "pending")
        state.setdefault("assessmentReviewItems", []).append(
            {"turnId": turn_id, "objective": previous, "labels": list(turn["criticalLabels"])})
        decision["clarificationReason"] = issue
    else:
        state.update(clarificationCount=0, clarificationIssue=None, clarificationIssues={},
                     assessmentStatus="needs-review" if session.get("assessmentStatus") == "needs-review" else "pending")
        state["evaluableTurnCount"] = state.get("evaluableTurnCount", 0) + 1
    ending = turn["endingReason"]
    challenge_now = False
    if not ending and remaining(state) == 0:
        ending = "turn_limit"
    if not ending and {1, 2, 3} <= resolved and not state.get("challengeShown"):
        state["challengeShown"] = challenge_now = True
    # Unity rejects an objective that drops, skips a step or rises without
    # objectiveCompleted. The displayed value catches up one step per turn.
    display = max(previous, min(lowest_open(resolved) or 4, previous + 1))
    state["phase"] = display
    if ending:
        state.update(endingReason=ending, status="awaitingCompletion")
    if ending == "exchange_accepted":
        state["resolutionAccepted"] = True
    state["customerState"] = {
        "resolvedConcerns": sorted(resolved), "statedConcern": turn["statedConcern"],
        "hintLevels": dict(customer["hintLevels"]), "standingOffer": customer["standingOffer"],
        "lastTurn": {"statedConcern": turn["statedConcern"], "statedConcernBefore": turn["statedConcernBefore"],
                     "hintLevel": turn["hintLevel"], "missingPart": turn["missingPart"],
                     "repeatedQuestion": turn["repeatedQuestion"], "noProgress": turn["noProgress"],
                     "resolvedConcerns": turn["resolvedConcerns"], "newComponents": turn["newComponents"],
                     "challengeShownNow": challenge_now}}
    decision.update(turnQuality=turn["turnQuality"], assessmentUncertain=turn["assessmentUncertain"],
                    objectiveCompleted=display > previous, conversationComplete=bool(ending),
                    endingReason=ending, disclosedFactIds=list(turn["disclosedFactIds"]))
    return state, decision

