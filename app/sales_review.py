"""Bounded completion review using the immutable evidence ledger and the shared scorer."""
from __future__ import annotations

import asyncio
import copy
import itertools
import json
import time
from dataclasses import replace

from app.config import get_settings
from app.sales_arbitration import MAX_LABELS, LunaSalesArbitrator, validate_decisions
from app.sales_concerns import VIOLATIONS, score_ledger
from app.sales_openrouter import QUESTION_TEXT

REVIEW_VERSION = "sales-review-v1"
MAX_VARIANTS = 64


def _signature(scored: dict) -> str:
    return json.dumps({"outcome": scored["outcome"], "components": scored["rubricComponents"],
                       "violations": scored["policyViolations"], "promises": scored["unresolvedPromises"],
                       "resolved": scored["resolvedConcerns"], "ending": scored["endingReason"]}, sort_keys=True)


def _context_changed(ledger: list, scored: dict) -> bool:
    """A counterfactual that would have changed what Lan said cannot be scored."""
    turns = scored["turns"]
    for index, (entry, turn) in enumerate(zip(ledger, turns)):
        if "disclosedFactIds" in entry and set(turn["derivedDisclosedFactIds"]) != set(entry["disclosedFactIds"]):
            return True
        # Lan raised a concern at a hint level; a variant that changes either
        # would have produced a different reply and a different dialogue.
        if "statedConcern" in entry and turn["statedConcern"] != entry["statedConcern"]:
            return True
        if "hintLevel" in entry and turn["hintLevel"] != entry["hintLevel"]:
            return True
        if "unresolvedPromiseTypesBefore" in entry and set(entry["unresolvedPromiseTypesBefore"]) != {
                name for name, code in VIOLATIONS.items() if code in turn["promisesBefore"]}:
            return True
        if turn["endingReason"] and index < len(ledger) - 1:
            return True
        if index + 1 < len(ledger):
            resolved = {concern for previous in turns[:index + 1] for concern in previous["resolvedConcerns"]}
            shown = bool(entry.get("challengeShownBefore")) or {1, 2, 3} <= resolved
            if shown != bool(ledger[index + 1].get("challengeShownBefore")):
                return True
    return False


def review_impact(session: dict) -> dict:
    """Try both values for each unresolved label without rewriting stored labels."""
    items = session.get("assessmentReviewItems", [])
    ledger = list(session.get("evidenceLedger", []))
    by_id = {entry.get("turnId"): entry for entry in ledger}
    fixed = {turn_id: {name: decision["status"] for name, decision in labels.items()
                       if isinstance(decision, dict) and decision.get("status") in ("true", "false")}
             for turn_id, labels in session.get("assessmentReviewDecisions", {}).items()}
    slots = []
    for item in items:
        turn_id = item.get("turnId")
        entry = by_id.get(turn_id)
        for name in item.get("labels", []):
            if entry is None or name not in QUESTION_TEXT or name not in entry.get("labels", {}):
                return {"reason": "missing_evidence", "invariant": False}
            if name not in fixed.get(turn_id, {}) and (turn_id, name) not in slots:
                slots.append((turn_id, name))
    if not items or len(ledger) != session.get("dialogueTurnCount"):
        return {"reason": "missing_evidence", "invariant": False}
    if 2 ** len(slots) > MAX_VARIANTS:
        return {"reason": "replay_budget_exceeded", "invariant": False, "unresolvedLabels": len(slots)}
    signature = None
    for variant, values in enumerate(itertools.product(("false", "true"), repeat=len(slots)), 1):
        overrides = copy.deepcopy(fixed)
        for (turn_id, name), value in zip(slots, values):
            overrides.setdefault(turn_id, {})[name] = value
        scored = score_ledger(ledger, overrides, session.get("endingReason") or "stopped_early")
        if any(turn["assessmentUncertain"] for turn in scored["turns"]):
            return {"reason": "unresolved_evidence", "invariant": False, "variantsChecked": variant}
        if _context_changed(ledger, scored):
            return {"reason": "context_changed", "invariant": False, "variantsChecked": variant}
        current = _signature(scored)
        if signature is not None and current != signature:
            return {"reason": "outcome_sensitive", "invariant": False, "variantsChecked": variant}
        signature = current
    assumptions = {}
    for turn_id, name in slots:
        assumptions.setdefault(turn_id, {})[name] = "false"
    return {"reason": "outcome_invariant" if slots else "reclassified", "invariant": True,
            "variantsChecked": 2 ** len(slots), "unresolvedLabels": len(slots), "assumptions": assumptions}


def apply_resolution(session: dict, impact: dict) -> None:
    # Any assignment gives the same outcome; record the one complete() reads.
    assumptions = session.setdefault("assessmentReviewAssumptions", {})
    for turn_id, labels in impact.get("assumptions", {}).items():
        assumptions.setdefault(turn_id, {}).update(labels)
    session.setdefault("assessmentResolvedReviewItems", []).extend(copy.deepcopy(session["assessmentReviewItems"]))
    session.update(assessmentReviewItems=[], assessmentStatus="pending", clarificationCount=0,
                   clarificationIssue=None, clarificationIssues={})
    session["assessmentReviewResolution"] = {key: value for key, value in impact.items() if key != "assumptions"}
    session["assessmentReviewResolution"]["version"] = REVIEW_VERSION



async def resolve_review(session: dict, store, arbitrator=None) -> dict:
    """One completion pass, at most eight labels and one shared Luna deadline."""
    impact = review_impact(session)
    if impact["invariant"]:
        apply_resolution(session, impact)
        return session
    session["assessmentReviewResolution"] = {**impact, "version": REVIEW_VERSION}
    if not session.get("lunaArbitrationEnabled") or session.get("assessmentReviewAttempted"):
        return session
    session["assessmentReviewAttempted"] = True
    await store.save(session)
    deadline = session.get("lunaDeadlineSeconds", 6)
    started = time.monotonic()
    metadata, decisions = {}, copy.deepcopy(session.get("assessmentReviewDecisions", {}))
    by_id = {turn["turnId"]: (index, turn) for index, turn in enumerate(session["turns"])}
    ledger = {entry.get("turnId"): entry for entry in session.get("evidenceLedger", [])}
    budget = MAX_LABELS
    try:
        async with asyncio.timeout(deadline):
            for item in session.get("assessmentReviewItems", []):
                turn_id = item["turnId"]
                if turn_id not in by_id or budget <= 0:
                    continue
                index, turn = by_id[turn_id]
                requested = [name for name in item["labels"] if name in QUESTION_TEXT
                             and decisions.get(turn_id, {}).get(name, {}).get("status") not in ("true", "false")][:budget]
                if not requested:
                    continue
                budget -= len(requested)
                remaining = max(.001, deadline - (time.monotonic() - started))
                service = arbitrator or LunaSalesArbitrator(settings=replace(get_settings(), sales_luna_timeout_seconds=remaining))
                prior = ledger.get(turn_id, {})
                context = {"objective": turn.get("objectiveActiveDuringTurn", prior.get("objective")),
                           "knownFacts": prior.get("factsKnownBefore", []),
                           "unresolvedPromiseTypes": prior.get("unresolvedPromiseTypesBefore", []),
                           "history": session["turns"][max(0, index - 3):index]}
                adjudicated = await service.adjudicate(turn.get("transcript", ""), context, requested)
                valid, rejected = validate_decisions(turn.get("transcript", ""), requested, adjudicated.get("labels", {}))
                decisions.setdefault(turn_id, {}).update({name: value for name, value in valid.items() if value["status"] in ("true", "false")})
                metadata[turn_id] = {**adjudicated.get("metadata", {}), "completionRejectionReasons": rejected}
    except Exception as exc:
        metadata["errorCode"] = "arbitrator_timeout" if isinstance(exc, TimeoutError) else "arbitrator_failed"
    # A diagnostics deletion during a paid call must remain authoritative.
    latest = await store.get(session["sessionId"])
    if latest is None:
        raise KeyError("session_not_found")
    if latest.get("diagnosticsDeleted"):
        raise RuntimeError("diagnostics_deleted")
    session.update(assessmentReviewDecisions=decisions, assessmentReviewMetadata=metadata)
    impact = review_impact(session)
    if impact["invariant"]:
        apply_resolution(session, impact)
    else:
        session["assessmentReviewResolution"] = {**impact, "version": REVIEW_VERSION}
    return session
