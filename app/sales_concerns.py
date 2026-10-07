"""Pure customer state and evidence-ledger scoring for returning-customer Sales v3.

The module performs no network, file or clock access. advance() applies one
ledger entry to the customer state. score_ledger() folds it over a ledger, so
per-turn ratings, completion outcomes, review replays and offline rescoring all
use the same rules.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping, Sequence

from app.sales_openrouter import QUESTION_TEXT

VIOLATIONS = {"unauthorizedRefund": "unauthorized_refund", "unauthorizedDiscount": "unauthorized_discount",
              "unauthorizedCompensation": "unauthorized_compensation", "absoluteGuarantee": "absolute_guarantee",
              "managerEscalation": "unnecessary_manager_escalation"}
RETRACTIONS = {"retractsUnauthorizedRefund": "unauthorized_refund",
               "retractsUnauthorizedDiscount": "unauthorized_discount",
               "retractsUnauthorizedCompensation": "unauthorized_compensation",
               "retractsAbsoluteGuarantee": "absolute_guarantee"}
BAD_TURN_LABELS = ("abuse", "disrespect", "refusesRemedy", "beggingWithoutExplanation")
COMPONENTS = ("acknowledgment", "responsibility", "policy", "lightweight", "trial",
              "use_question", "fit_question", "cause", "explanation", "challenge_reply")
# Order decides which two facts Lan answers first when one utterance asks more.
FACT_QUESTIONS = (("painLocationQuestion", "pain_location"), ("painTimingQuestion", "late_discomfort"),
                  ("walkingQuestion", "walking_routine"), ("fitQuestion", "fit_condition"),
                  ("preferenceQuestion", "lighter_preference"), ("appearanceQuestion", "appearance"))
COMPLAINT_FACTS = frozenset({"pain_location", "late_discomfort", "walking_routine", "fit_condition", "lighter_preference"})
FORCED_ENDINGS = frozenset({"manager_escalation", "maintained_unauthorized_promise", "second_silence"})
CONCERNS = (1, 2, 3, 4)
MAX_HINT = 2
MAX_FACTS_PER_REPLY = 2
CHALLENGE_LABELS = ("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
# Acts that can only help the player. Used by the monotonicity property.
GOOD_LABELS = tuple(name for name in QUESTION_TEXT if name not in VIOLATIONS and name not in BAD_TURN_LABELS
                    and name not in ("maintainsUnauthorizedPromise", "repeatedQuestion")) + tuple(RETRACTIONS)
CONCERN_PARTS = {1: ("acknowledgment", "problem_question"), 2: ("walking", "fit", "cause"),
                 3: ("offer", "conditions", "lightweight", "trial"), 4: ("responsibility", "explanation", "trial")}


def investigated(facts: Iterable[str]) -> bool:
    """Concern 2 has enough facts when walking and fit or preference are known."""
    facts = set(facts)
    return "walking_routine" in facts and bool(facts & {"fit_condition", "lighter_preference"})


def lowest_open(resolved: Iterable[int]) -> int | None:
    resolved = set(resolved)
    return next((concern for concern in CONCERNS if concern not in resolved), None)


def initial_state() -> dict:
    return {"components": {}, "violations": [], "promises": [], "resolved": [], "ackSeen": False,
            "openQuestionSeen": False, "standingOffer": False, "postChallenge": [], "statedConcern": 1,
            "hintLevels": {str(concern): 0 for concern in CONCERNS}, "ending": None}


def _statuses(labels: Any, overrides: Mapping | None) -> dict[str, str]:
    labels = labels if isinstance(labels, Mapping) else {}
    result = {name: (value.get("status") if isinstance(value, Mapping) else None) or "uncertain"
              for name, value in labels.items()}
    for name, value in (overrides or {}).items():
        status = value.get("status") if isinstance(value, Mapping) else value
        if status in ("true", "false"):
            result[name] = status
    return result


def critical_labels(state: Mapping, status: Mapping[str, str], facts: Iterable[str], challenge: bool) -> list[str]:
    """Uncertain labels that could change the score, a concern or an ending now."""
    unsure = lambda name: status.get(name) == "uncertain"
    names = [name for name in (*VIOLATIONS, *BAD_TURN_LABELS) if unsure(name)]
    components, resolved = state["components"], set(state["resolved"])
    stated = state["statedConcern"]
    if stated == 1:
        if not state["openQuestionSeen"] and unsure("openQuestion"):
            names.append("openQuestion")
        if not state["ackSeen"] and unsure("acknowledgment"):
            names.append("acknowledgment")
    if 2 not in resolved and investigated(facts) and unsure("causeStatement"):
        names.append("causeStatement")
    if stated == 3:
        for name, component in (("exchangeOffer", "policy"), ("exchangeConditions", "policy"),
                                ("lightweightForWalking", "lightweight"), ("fitOrWalkTrial", "trial")):
            if component not in components and unsure(name):
                names.append(name)
    if stated == 4 and challenge:
        names += [name for name in CHALLENGE_LABELS if name not in state["postChallenge"] and unsure(name)]
    if state["promises"]:
        active = [name for name, code in RETRACTIONS.items() if code in state["promises"]]
        names += [name for name in ("maintainsUnauthorizedPromise", *active) if unsure(name)]
    return sorted(set(names))


def facts_to_answer(status: Mapping[str, str], known: Iterable[str], bad: bool) -> tuple[list[str], bool]:
    """Facts Lan answers now, and whether every fact asked about was already known."""
    known = set(known)
    asked = list(dict.fromkeys(fact for label, fact in FACT_QUESTIONS if status.get(label) == "true"))
    if bad:
        return [], False
    new = [fact for fact in asked if fact not in known][:MAX_FACTS_PER_REPLY]
    return new, bool(asked) and not new


def missing_part(state: Mapping, concern: int | None, facts: Iterable[str]) -> str | None:
    """The unfinished part of a concern that Lan raises next."""
    facts = set(facts)
    components = state["components"]
    if concern == 1:
        return "acknowledgment" if not state["ackSeen"] else "problem_question"
    if concern == 2:
        if "walking_routine" not in facts:
            return "walking"
        return "fit" if not facts & {"fit_condition", "lighter_preference"} else "cause"
    if concern == 3:
        if "policy" not in components:
            return "conditions" if state["standingOffer"] else "offer"
        return "lightweight" if "lightweight" not in components else "trial"
    if concern == 4:
        seen = set(state["postChallenge"])
        return ("responsibility" if "originalSaleResponsibility" not in seen else
                "explanation" if "routineMatchExplanation" not in seen else "trial")
    return None


def advance(state: Mapping, entry: Mapping, overrides: Mapping | None = None) -> tuple[dict, dict]:
    """Apply one ledger entry. Returns the new state and the turn assessment."""
    state = copy.deepcopy(dict(state))
    turn_id = entry.get("turnId")
    status = _statuses(entry.get("labels"), (overrides or {}).get(turn_id))
    yes = lambda name: status.get(name) == "true"
    facts = set(entry.get("factsKnownBefore") or [])
    challenge = bool(entry.get("challengeShownBefore"))
    resolved_before = set(state["resolved"])
    stated_before = state["statedConcern"]
    promises_before = list(state["promises"])
    critical = critical_labels(state, status, facts, challenge)
    events = [code for label, code in VIOLATIONS.items() if yes(label)]
    if yes("abuse"):
        events.append("abusive_language")
    for code in events:
        event = {"turnId": turn_id, "code": code}
        if event not in state["violations"]:
            state["violations"].append(event)
    new_promises = [code for code in events if code.startswith("unauthorized_") or code == "absolute_guarantee"]
    bad = bool(events) or any(yes(name) for name in BAD_TURN_LABELS)
    respectful = not any(yes(name) for name in BAD_TURN_LABELS)
    ending = "manager_escalation" if yes("managerEscalation") else None
    new_components: list[str] = []
    if critical:
        # Unknown acts never earn credit, but separately certain violations and
        # promises still count. Retractions wait for the turn to be resolved.
        state["promises"] = sorted(set(promises_before) | set(new_promises))
        if not ending and promises_before and yes("maintainsUnauthorizedPromise"):
            ending = "maintained_unauthorized_promise"
        quality = "bad" if bad else "uncertain"
    else:
        retracted = {code for name, code in RETRACTIONS.items() if yes(name)}
        outstanding = [code for code in promises_before if code not in retracted]
        safe = not bad and not outstanding
        state["promises"] = sorted(set(outstanding) | set(new_promises))
        if not ending and promises_before and yes("maintainsUnauthorizedPromise") and state["promises"]:
            ending = "maintained_unauthorized_promise"
        components = state["components"]

        def grant(key: str, condition: bool) -> None:
            if condition and key not in components:
                components[key] = {"turnId": turn_id}
                new_components.append(key)

        enough = investigated(facts)
        offer_before = state["standingOffer"]
        if yes("acknowledgment") and not yes("abuse"):
            state["ackSeen"] = True
        if yes("openQuestion"):
            state["openQuestionSeen"] = True
        grant("acknowledgment", yes("acknowledgment") and not yes("abuse"))
        grant("policy", safe and yes("exchangeConditions") and (yes("exchangeOffer") or offer_before))
        grant("lightweight", safe and enough and yes("lightweightForWalking"))
        grant("trial", safe and enough and yes("fitOrWalkTrial"))
        grant("explanation", safe and enough and yes("routineMatchExplanation"))
        grant("use_question", yes("walkingQuestion") and "walking_routine" not in facts)
        grant("fit_question", (yes("fitQuestion") and "fit_condition" not in facts)
              or (yes("preferenceQuestion") and "lighter_preference" not in facts))
        grant("cause", yes("causeStatement") and enough)
        if challenge and safe:
            for name in CHALLENGE_LABELS:
                if yes(name) and name not in state["postChallenge"]:
                    state["postChallenge"].append(name)
        grant("responsibility", challenge and safe and yes("originalSaleResponsibility"))
        grant("challenge_reply", challenge and respectful
              and {"fitOrWalkTrial", "routineMatchExplanation"} <= set(state["postChallenge"]))
        if yes("refusesRemedy"):
            state["standingOffer"] = False
        elif safe and yes("exchangeOffer"):
            state["standingOffer"] = True
        resolved = set(resolved_before)
        if respectful and not events and not state["promises"] and not ending:
            if state["ackSeen"] and state["openQuestionSeen"]:
                resolved.add(1)
            if "cause" in components:
                resolved.add(2)
            if {"policy", "lightweight", "trial"} <= set(components):
                resolved.add(3)
            if challenge and set(CHALLENGE_LABELS) <= set(state["postChallenge"]):
                resolved.add(4)
        state["resolved"] = sorted(resolved)
        if (not ending and 1 in resolved_before and facts & COMPLAINT_FACTS and 2 not in resolved
                and safe and yes("exchangeOffer") and yes("exchangeConditions")):
            # Lan can accept an operational remedy before the cause is proven.
            ending = "exchange_accepted"
        if not ending and 4 in resolved:
            ending = "objectives_completed"
        quality = "bad" if bad else "good" if new_components or resolved - resolved_before else "neutral"
    answered, repeated = facts_to_answer(status, facts, bad)
    disclosed = list(entry["disclosedFactIds"]) if isinstance(entry.get("disclosedFactIds"), list) else answered
    newly_resolved = sorted(set(state["resolved"]) - resolved_before)
    progress = bool(new_components or disclosed or newly_resolved)
    no_progress = (quality in ("good", "neutral") and not progress and not ending
                   and stated_before not in state["resolved"])
    hints = state["hintLevels"]
    if no_progress:
        if hints[str(stated_before)] >= MAX_HINT:
            ending = "stalemate"
        else:
            hints[str(stated_before)] += 1
    stated = lowest_open(state["resolved"]) or stated_before
    state["statedConcern"] = stated
    if ending and not state["ending"]:
        state["ending"] = ending
    known_after = facts | set(disclosed)
    return state, {"turnId": turn_id, "turnQuality": quality, "assessmentUncertain": bool(critical),
                   "criticalLabels": critical, "violations": events, "newComponents": new_components,
                   "resolvedConcerns": newly_resolved, "disclosedFactIds": disclosed,
                   "derivedDisclosedFactIds": answered, "repeatedQuestion": repeated and not disclosed,
                   "noProgress": no_progress, "statedConcernBefore": stated_before, "statedConcern": stated,
                   "hintLevel": hints[str(stated)], "missingPart": missing_part(state, stated, known_after),
                   "endingReason": ending, "promisesBefore": promises_before, "promisesAfter": list(state["promises"])}


def score_ledger(entries: Sequence[Mapping], overrides: Mapping | None = None, ending: str | None = None) -> dict:
    """Score a whole conversation. Every entry is applied; none is skipped.

    The ending argument supplies session-level endings the ledger cannot
    derive, such as second_silence, turn_limit or stopped_early. A derived
    ending wins.
    """
    state, turns = initial_state(), []
    for entry in entries:
        state, turn = advance(state, entry, overrides)
        turns.append(turn)
    final_ending = state["ending"] or ending
    components = state["components"]
    criteria = {"apologyAndPolicyRemedy": sum(10 for key in COMPONENTS[:5] if key in components),
                "adaptabilityAndDeescalation": sum(10 for key in COMPONENTS[5:] if key in components)}
    raw = sum(criteria.values())
    penalty = min(raw, 10 * len(state["violations"]))
    score = raw - penalty
    resolved = set(state["resolved"])
    forced = final_ending in FORCED_ENDINGS
    accepted = final_ending == "exchange_accepted" and "policy" in components
    restored = resolved == set(CONCERNS) and score >= 70 and not state["promises"] and not forced
    partial = "acknowledgment" in components and not forced and not state["promises"] and (
        (3 in resolved and score >= 40) or accepted)
    trust = "restored" if restored else "partially_restored" if partial else "lost"
    result = {"criterionScores": criteria, "rawScore": raw, "policyViolationPenalty": penalty, "score": score,
              "trustState": trust,
              "customerRating": {"restored": "good", "partially_restored": "considering", "lost": "bad"}[trust],
              "emotionalHandling": 1 in resolved, "causeIdentification": 2 in resolved,
              "solutionSuitability": 3 in resolved, "trustRebuilding": 4 in resolved}
    return {"state": state, "turns": turns, "outcome": result, "endingReason": final_ending,
            "resolutionAccepted": accepted, "rubricComponents": copy.deepcopy(components),
            "policyViolations": copy.deepcopy(state["violations"]), "unresolvedPromises": list(state["promises"]),
            "resolvedConcerns": sorted(resolved)}


def review_overrides(session: Mapping) -> dict:
    """Label statuses decided at completion review, keyed by turn ID.

    Luna decisions win. Labels from a review resolved as outcome-invariant are
    read as false; every assignment gives the same outcome by construction.
    """
    overrides: dict[str, dict[str, str]] = {}
    if (session.get("assessmentReviewResolution") or {}).get("invariant"):
        for item in session.get("assessmentResolvedReviewItems") or []:
            for name in item.get("labels", []):
                overrides.setdefault(item.get("turnId"), {})[name] = "false"
    for turn_id, labels in (session.get("assessmentReviewAssumptions") or {}).items():
        overrides.setdefault(turn_id, {}).update({name: value for name, value in labels.items() if value in ("true", "false")})
    for turn_id, labels in (session.get("assessmentReviewDecisions") or {}).items():
        for name, decision in labels.items():
            status = decision.get("status") if isinstance(decision, Mapping) else decision
            if status in ("true", "false"):
                overrides.setdefault(turn_id, {})[name] = status
    return overrides


def score_session(session: Mapping) -> dict:
    """The single completion outcome for a session's ledger."""
    return score_ledger(session.get("evidenceLedger") or [], review_overrides(session), session.get("endingReason"))
