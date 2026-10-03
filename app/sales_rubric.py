"""Versioned, deterministic evidence rules for returning-customer Sales."""
from __future__ import annotations

import copy
import math
from typing import Any, Mapping
from app.sales_openrouter import (PENALTY_LABELS, QUESTION_TEXT, PIPELINE_VERSION, RUBRIC_VERSION,
    SCENARIO_VERSION, QUESTION_SET_VERSION, THRESHOLD_VERSION, PROMPT_VERSION)

VERSIONS = dict(pipelineVersion=PIPELINE_VERSION, rubricVersion=RUBRIC_VERSION,
                scenarioVersion=SCENARIO_VERSION, questionSetVersion=QUESTION_SET_VERSION,
                thresholdVersion=THRESHOLD_VERSION, promptVersion=PROMPT_VERSION)
VIOLATIONS = {"unauthorizedRefund": "unauthorized_refund", "unauthorizedDiscount": "unauthorized_discount",
              "unauthorizedCompensation": "unauthorized_compensation", "absoluteGuarantee": "absolute_guarantee",
              "managerEscalation": "unnecessary_manager_escalation"}
RETRACTIONS = {"retractsUnauthorizedRefund": "unauthorized_refund",
               "retractsUnauthorizedDiscount": "unauthorized_discount",
               "retractsUnauthorizedCompensation": "unauthorized_compensation",
               "retractsAbsoluteGuarantee": "absolute_guarantee"}
SAFETY = tuple(VIOLATIONS) + ("abuse", "profanityOrInsult", "maintainsUnauthorizedPromise", "retractsUnauthorizedPromise") + tuple(RETRACTIONS)
ORDINARY = ("apology", "emotionalAcknowledgment", "openQuestion", "useOrDurationQuestion",
            "fitConditionOrPreferenceQuestion", "causeStatement", "exchangeOffer", "exchangeConditions",
            "lightweightForWalking", "fitOrWalkTrial", "originalSaleResponsibility",
            "routineMatchExplanation", "verificationStep", "painLocationQuestion", "painTimingQuestion",
            "walkingQuestion", "fitQuestion", "preferenceQuestion", "appearanceQuestion", "confirmation",
            "repeatedQuestion", "disrespect", "condescending", "clarificationQuestion", "neutralAcknowledgment")
COMPONENTS = ("acknowledgment", "responsibility", "policy", "lightweight", "trial",
              "use_question", "fit_question", "cause", "explanation", "challenge_reply")


def supports_versions(session: Mapping) -> bool:
    return all(session.get(name) == version for name, version in VERSIONS.items())


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
                       unresolvedPromises=[], clarificationCount=0, clarificationIssue=None,
                       jevDeadlineSeconds=getattr(settings, "sales_jev_timeout_seconds", 3),
                       writerDeadlineSeconds=getattr(settings, "sales_writer_timeout_seconds", 4))
    else:
        session.update(pipelineVersion="sales-legacy-v1", rubricVersion="sales-legacy-v1",
                       scenarioVersion="returning-shoes-v1", questionSetVersion="sales-local-v1",
                       thresholdVersion="sales-local-v1", promptVersion="sales-local-v1")
        if mode == "shadow":
            session["shadowVersions"] = dict(VERSIONS)


def remaining(session: Mapping) -> int:
    used = (session.get("evaluableTurnCount", 0) if session.get("pipelineMode") == "openrouter"
            else session.get("acceptedTurnCount", 0))
    return max(0, int(session.get("maxTurns", 4)) + int(bool(session.get("supplementalTurnGranted")))
               - int(used))


def outcome(session: Mapping) -> dict:
    components = session.get("rubricComponents", {})
    criteria = {"apologyAndPolicyRemedy": sum(10 for key in COMPONENTS[:5] if key in components),
                "adaptabilityAndDeescalation": sum(10 for key in COMPONENTS[5:] if key in components)}
    raw = sum(criteria.values())
    penalty = min(raw, 10 * len(session.get("policyViolations", [])))
    score = raw - penalty
    done = set(session.get("completedObjectives", []))
    forced = session.get("endingReason") in {"manager_escalation", "maintained_unauthorized_promise", "second_silence"}
    restored = done == {1, 2, 3, 4} and session.get("challengeAnswered") and score >= 70 and not session.get("unresolvedPromises") and not forced
    partial = "acknowledgment" in components and 3 in done and score >= 40 and not forced
    trust = "restored" if restored else "partially_restored" if partial else "lost"
    return dict(criterionScores=criteria, rawScore=raw, policyViolationPenalty=penalty, score=score,
                trustState=trust, customerRating={"restored": "good", "partially_restored": "considering", "lost": "bad"}[trust],
                emotionalHandling=1 in done, causeIdentification=2 in done,
                solutionSuitability=3 in done, trustRebuilding=4 in done)


def evaluate(session: Mapping, labels: Mapping, turn_id: str) -> tuple[dict, dict]:
    """Compute a candidate state. Only the turn commit makes it authoritative."""
    state = copy.deepcopy(dict(session))
    phase = int(state["phase"])
    yes = lambda name: labels.get(name, {}).get("status") == "true"
    prior_evidence = state.get("objectiveEvidence", {}).get(str(phase), {})
    uncertain = [name for name in tuple(VIOLATIONS) + ("abuse", "profanityOrInsult")
                 if labels.get(name, {}).get("status") == "uncertain"]
    relevant = {1: ("openQuestion",), 2: (),
                3: ("exchangeOffer", "exchangeConditions", "lightweightForWalking", "fitOrWalkTrial"),
                4: ("originalSaleResponsibility", "routineMatchExplanation", "verificationStep")}[phase]
    uncertain += [name for name in relevant if name not in prior_evidence and labels.get(name, {}).get("status") == "uncertain"]
    acknowledgment = ("apology", "emotionalAcknowledgment")
    if phase == 1 and not any(name in prior_evidence or yes(name) for name in acknowledgment) and any(
            labels.get(name, {}).get("status") == "uncertain" for name in acknowledgment):
        uncertain.append("acknowledgment")
    if phase == 2 and "walking_routine" in state.get("investigationEvidence", []) and set(state.get("investigationEvidence", [])) & {"fit_condition", "lighter_preference"} and labels.get("causeStatement", {}).get("status") == "uncertain":
        uncertain.append("causeStatement")
    if state.get("unresolvedPromises"):
        uncertain += [name for name in ("maintainsUnauthorizedPromise", "retractsUnauthorizedPromise")
                       if labels.get(name, {}).get("status") == "uncertain"]
        if yes("retractsUnauthorizedPromise") and len(state["unresolvedPromises"]) > 1:
            # A generic correction cannot identify which of several promises ended.
            active_retractions = [name for name, code in RETRACTIONS.items() if code in state["unresolvedPromises"]]
            uncertain += [name for name in active_retractions if labels.get(name, {}).get("status") == "uncertain"]
            if not any(yes(name) for name in active_retractions) and not uncertain:
                uncertain.append("promise_retraction_type")
    if uncertain:
        issue = ",".join(sorted(uncertain))
        previous_counts = state.get("clarificationIssues", {})
        issue_counts = {name: previous_counts.get(name, 0) + 1 for name in set(uncertain)}
        count = max(issue_counts.values())
        state.update(clarificationIssue=issue, clarificationIssues=issue_counts, clarificationCount=count,
                     assessmentStatus="needs-review" if count > 2 or session.get("assessmentStatus") == "needs-review" else "pending")
        return state, dict(turnQuality="uncertain", objectiveCompleted=False, disclosedFactIds=[],
                           conversationComplete=False, clarificationReason=issue)
    state.update(clarificationCount=0, clarificationIssue=None, clarificationIssues={},
                 assessmentStatus="needs-review" if session.get("assessmentStatus") == "needs-review" else "pending")
    facts = set(state.get("investigationEvidence", []))
    investigated = "walking_routine" in facts and bool(facts & {"fit_condition", "lighter_preference"})
    evidence = state.setdefault("objectiveEvidence", {}).setdefault(str(phase), {})
    retracted = {code for name, code in RETRACTIONS.items() if yes(name)}
    if yes("retractsUnauthorizedPromise") and len(state.get("unresolvedPromises", [])) == 1 and not retracted:
        # The generic legacy question is unambiguous for a single active promise.
        retracted.update(state["unresolvedPromises"])
    outstanding = [code for code in state.get("unresolvedPromises", []) if code not in retracted]
    safe_solution = not any(yes(name) for name in VIOLATIONS) and not yes("abuse") and not yes("profanityOrInsult") and (
        not outstanding)
    remedy_labels = {"exchangeOffer", "exchangeConditions", "lightweightForWalking", "fitOrWalkTrial",
                     "originalSaleResponsibility", "routineMatchExplanation", "verificationStep"}
    for name in ORDINARY:
        if yes(name) and (name not in remedy_labels or safe_solution):
            evidence.setdefault(name, turn_id)
    has = lambda name: name in evidence
    components = state.setdefault("rubricComponents", {})
    def grant(key, condition):
        if condition:
            components.setdefault(key, {"turnId": turn_id, "objective": phase})
    grant("acknowledgment", (yes("apology") or yes("emotionalAcknowledgment")) and not yes("abuse") and not yes("profanityOrInsult"))
    grant("responsibility", phase == 4 and safe_solution and state.get("challengeShown") and yes("originalSaleResponsibility"))
    appropriate_question = not yes("repeatedQuestion") or yes("confirmation")
    grant("use_question", phase == 2 and appropriate_question and (yes("useOrDurationQuestion") or yes("walkingQuestion")))
    grant("fit_question", phase == 2 and appropriate_question and (yes("fitConditionOrPreferenceQuestion") or yes("fitQuestion") or yes("preferenceQuestion")))
    grant("cause", phase == 2 and investigated and yes("causeStatement"))
    # Premature claims cannot be carried forward into later objectives.
    if phase == 2 and not investigated:
        evidence.pop("causeStatement", None)
    grant("policy", phase == 3 and safe_solution and has("exchangeOffer") and has("exchangeConditions"))
    grant("lightweight", phase >= 3 and safe_solution and investigated and yes("lightweightForWalking"))
    grant("trial", phase >= 3 and safe_solution and investigated and yes("fitOrWalkTrial"))
    grant("explanation", phase >= 3 and safe_solution and investigated and yes("routineMatchExplanation"))
    respectful = not any(yes(name) for name in ("abuse", "profanityOrInsult", "disrespect", "condescending"))
    grant("challenge_reply", phase == 4 and state.get("challengeShown") and has("verificationStep") and has("routineMatchExplanation") and respectful)
    events = [code for label, code in VIOLATIONS.items() if yes(label)]
    if yes("abuse") or yes("profanityOrInsult"):
        events.append("abusive_language")
    for code in events:
        state.setdefault("policyViolations", []).append({"turnId": turn_id, "code": code})
    promises = [code for code in events if code.startswith("unauthorized_") or code == "absolute_guarantee"]
    state["unresolvedPromises"] = outstanding
    if promises:
        state["unresolvedPromises"] = sorted(set(state.get("unresolvedPromises", [])) | set(promises))
    ending = "manager_escalation" if yes("managerEscalation") else "maintained_unauthorized_promise" if session.get("unresolvedPromises") and yes("maintainsUnauthorizedPromise") and state.get("unresolvedPromises") else None
    complete = ((has("apology") or has("emotionalAcknowledgment")) and has("openQuestion") if phase == 1 else
                investigated and has("causeStatement") if phase == 2 else
                has("exchangeOffer") and has("exchangeConditions") and has("lightweightForWalking") and has("fitOrWalkTrial") if phase == 3 else
                state.get("challengeShown") and has("originalSaleResponsibility") and has("routineMatchExplanation") and has("verificationStep"))
    complete = bool(complete and respectful and not state.get("unresolvedPromises") and not events and not ending)
    if complete:
        state.setdefault("completedObjectives", []).append(phase)
        if phase < 4:
            state["phase"] += 1
        else:
            state["challengeAnswered"] = True
            ending = "objectives_completed"
    quality = "bad" if events or yes("disrespect") or yes("condescending") else "good" if complete or any(ref.get("turnId") == turn_id for ref in components.values()) else "neutral"
    state["evaluableTurnCount"] = state.get("evaluableTurnCount", 0) + 1
    if phase == 3 and complete:
        state["challengeShown"] = True
        if remaining(state) == 0:
            state["supplementalTurnGranted"] = True
    if not ending and remaining(state) == 0:
        ending = "turn_limit"
    if ending:
        state.update(endingReason=ending, status="awaitingCompletion")
    return state, dict(turnQuality=quality, objectiveCompleted=complete, conversationComplete=bool(ending),
                       endingReason=ending, disclosedFactIds=[])
