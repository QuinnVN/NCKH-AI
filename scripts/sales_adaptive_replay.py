"""Rebuild the dialogue state BEFORE a saved salesperson turn, for adaptive replay.

The discovery manifest's ``sessionMemorySnapshot`` is the FINAL session state.
It is never used as prefix state. Instead each case is rebuilt from its source
session file:

1. a fresh current-version session (current rubric, scenario and memory rules);
2. customer memory from the source's immutable facts with shared flags reset to
   opening/part1; prefix disclosures arrive during replay, or for sessions
   saved without memory, the Part 1 attempt linked as the live backend would;
3. every PRIOR turn replayed through ``evaluate()`` with its stored labels
   (ledger labels where available), then disclosures reconciled to what Lan
   actually said in the saved transcript;
4. the selected turn evaluated with its own labels (the classifier result for
   the current utterance precedes Lan's reply, so it is not future knowledge).

The historical Lan reply to the selected turn and every later turn are never
read into the replay state; they stay in ``historicalReference`` for graders.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from app.sales_adaptive import LEGACY_INTENT_ACTIONS, ADAPTIVE_VERSION
from app.sales_concerns import GOOD_LABELS, RETRACTIONS
from app.sales_customer_lines import select_customer_line
from app.sales_memory import MEMORY_VERSION, initialize_memory, link_prior_sale
from app.sales_openrouter import FACTS, QUESTION_TEXT
from app.sales_pipeline import plan_reply
from app.sales_rubric import evaluate, initialize

LABEL_NAMES = tuple(dict.fromkeys((*QUESTION_TEXT, *RETRACTIONS)))
# Legacy (pre-v2) boolean question names -> current labels. Several legacy
# names may set one current label (logical OR). Unlisted legacy names carry no
# current meaning and are reported as unmapped.
LEGACY_BOOLEAN_MAP = {
    "emotionalAcknowledgment": ("acknowledgment",), "apology": ("acknowledgment",),
    "openQuestion": ("openQuestion",), "useOrDurationQuestion": ("walkingQuestion",),
    "fitConditionOrPreferenceQuestion": ("fitQuestion",), "causeStatement": ("causeStatement",),
    "policyExchange": ("exchangeOffer", "exchangeConditions"), "reasonableReturnPolicy": ("exchangeConditions",),
    "lightweightForWalking": ("lightweightForWalking",), "fitOrWalkTrial": ("fitOrWalkTrial",),
    "originalSaleResponsibility": ("originalSaleResponsibility",),
    "routineMatchExplanation": ("routineMatchExplanation",),
    "maintainsUnauthorizedPromise": ("maintainsUnauthorizedPromise",), "managerEscalation": ("managerEscalation",),
    "abuse": ("abuse",), "profanityOrInsult": ("abuse",), "condescending": ("disrespect",),
    "beggingWithoutExplanation": ("beggingWithoutExplanation",), "repeatedQuestion": ("repeatedQuestion",),
}
VIOLATION_LABELS = {"unauthorized_refund": "unauthorizedRefund", "unauthorized_discount": "unauthorizedDiscount",
                    "unauthorized_compensation": "unauthorizedCompensation",
                    "absolute_guarantee": "absoluteGuarantee"}
COMMIT_KEYS = ("phase", "assessmentStatus", "clarificationCount", "clarificationIssue", "clarificationIssues",
               "rubricComponents", "objectiveEvidence", "completedObjectives", "unresolvedPromises",
               "policyViolations", "evaluableTurnCount", "dialogueTurnCount", "assessmentReviewItems",
               "supplementalTurnGranted", "endingReason", "status", "challengeShown", "challengeAnswered",
               "investigationEvidence", "resolutionAccepted", "customerState", "customerMemory")
SA01_AMENDMENT = {
    "caseId": "SA-01", "fromTurnIndex": 8, "fromTurnId": "63b3fcb38b544ebba195da666bb2a2f2",
    "toTurnIndex": 6, "toTurnId": "8fa9657a2b17451b9fbe26a2b7cdcff0",
    "reason": ("Saved turn 8 is the forced turn_limit turn and its utterance includes a spoken complaint about the "
               "app, so no adaptive continue/close choice exists there. Turn 6 of the same session is a repeated "
               "no-receipt refusal with two dialogue turns still remaining, which tests leaving a refusal loop "
               "before the cap."),
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def labels_for(turn: Mapping, ledger_entry: Mapping | None) -> tuple[dict, dict]:
    """Current-name label statuses for one saved turn, with provenance.

    Ledger labels (what live scoring used, after arbitration) win over the turn
    record. Labels absent from the source question set are set to "false" and
    listed; legacy booleans are mapped by LEGACY_BOOLEAN_MAP.
    """
    source = ledger_entry.get("labels") if isinstance(ledger_entry, Mapping) and isinstance(ledger_entry.get("labels"), Mapping) else None
    origin = "ledger" if source is not None else "turnAssessment"
    source = source if source is not None else (turn.get("turnAssessment") if isinstance(turn.get("turnAssessment"), Mapping) else {})
    labels, absent, unmapped = {}, [], []
    legacy = bool(source) and all(isinstance(value, bool) or key == "policyViolations" for key, value in source.items())
    if legacy:
        origin += ":legacy-boolean"
        status = {name: False for name in LABEL_NAMES}
        mapped = set()
        for key, value in source.items():
            if key == "policyViolations":
                continue
            targets = LEGACY_BOOLEAN_MAP.get(key)
            if targets is None:
                if key != "unauthorizedPromise":
                    unmapped.append(key)
                continue
            for target in targets:
                status[target] = status[target] or bool(value)
                mapped.add(target)
        codes = list(turn.get("policyViolations") or source.get("policyViolations") or [])
        for code in codes:
            if code in VIOLATION_LABELS:
                status[VIOLATION_LABELS[code]] = True
                mapped.add(VIOLATION_LABELS[code])
        promise_uncertain = bool(source.get("unauthorizedPromise")) and not any(code in VIOLATION_LABELS for code in codes)
        mapped |= set(VIOLATION_LABELS.values()) if source.get("unauthorizedPromise") is not None else set()
        for name in LABEL_NAMES:
            value = "true" if status[name] else "false"
            if promise_uncertain and name in VIOLATION_LABELS.values():
                value = "uncertain"
            labels[name] = {"status": value, "noul": None, "source": "replay-legacy"}
            if name not in mapped:
                absent.append(name)
    else:
        for name in LABEL_NAMES:
            raw = source.get(name)
            if isinstance(raw, Mapping) and raw.get("status") in ("true", "false", "uncertain"):
                labels[name] = {"status": raw["status"], "noul": raw.get("noul"), "source": "replay"}
            else:
                labels[name] = {"status": "false", "noul": None, "source": "replay-absent"}
                absent.append(name)
    return labels, {"origin": origin, "absentLabelsSetFalse": absent, "unmappedLegacyKeys": unmapped}


def _find_part1(root: Path, attempt_id: str | None) -> Path | None:
    if not attempt_id:
        return None
    for folder in ("recordings", "backup"):
        for name in (f"sales-persuasion-{attempt_id}.json", f"{attempt_id}.json"):
            path = root / folder / name
            if path.exists():
                return path
    return None


def base_session(source: Mapping, root: Path, case_id: str, prior_ids: list[str]) -> tuple[dict, dict]:
    """Fresh current-version session with prefix-only customer memory."""
    from app.sales_returning_customer import OPENING_COMPLAINT
    session = {"sessionId": "replay-" + case_id, "phase": 1, "acceptedTurnCount": 0, "goodResponseCount": 0,
               "badResponseCount": 0, "missingReturnPolicyCount": 0, "policyViolations": [], "silenceCount": 0,
               "turnIds": [], "turns": [], "openingComplaint": source.get("openingComplaint") or OPENING_COMPLAINT,
               "status": "active"}
    max_turns = source.get("maxTurns") if source.get("pipelineMode") == "openrouter" and source.get("maxTurns") else 8
    initialize(session, SimpleNamespace(pipeline_mode="openrouter", sales_max_turns=max_turns,
                                        sales_luna_arbitration_enabled=False))
    provenance = {"maxTurns": max_turns, "maxTurnsSource": "source" if max_turns == source.get("maxTurns") else
                  "current-default (source used the legacy turn budget)"}
    memory = source.get("customerMemory")
    if isinstance(memory, Mapping) and isinstance(memory.get("facts"), Mapping):
        # Replay starts before turn one. Even a disclosure in the selected
        # prefix must arrive through apply_prior_turn(), not seed earlier
        # ledger entries with knowledge from a later prefix turn.
        allowed = {"opening", "part1"}
        facts = {}
        reset = []
        for fact, record in memory["facts"].items():
            if fact not in FACTS or not isinstance(record, Mapping):
                continue
            record = copy.deepcopy(dict(record))
            if record.get("shared") and record.get("sharedTurnId") not in allowed:
                record.update(shared=False, sharedTurnId=None)
                reset.append(fact)
            facts[fact] = record
        fresh = session["customerMemory"]
        fresh["facts"].update(facts)
        for key in ("priorSaleLinked", "purchasedShoe", "originalBudget"):
            if key in memory:
                fresh[key] = copy.deepcopy(memory[key])
        # actions/requests are rebuilt by replaying prior turns, never copied.
        provenance["memory"] = {"origin": "source customerMemory (immutable facts), shared flags reset to prefix",
                                "sharedResetFacts": sorted(reset), "priorSaleLinked": bool(fresh.get("priorSaleLinked"))}
    else:
        path = _find_part1(root, source.get("part1AttemptId"))
        linked = False
        detail = "no Part 1 attempt file"
        if path is not None:
            attempt = json.loads(path.read_text(encoding="utf-8"))
            session.update(sessionId=source.get("sessionId"), runId=source.get("runId"),
                           customerIdentity={"customerId": attempt.get("customerId"), "continuesPart1Customer": True})
            try:
                link_prior_sale(session, attempt)
                linked = bool(session["customerMemory"].get("priorSaleLinked"))
                detail = "linked from " + path.parent.name + "/" + path.name
            except ValueError as exc:
                detail = "Part 1 attempt rejected: " + str(exc)
            session["sessionId"] = "replay-" + case_id
            session.pop("runId", None)
            session.pop("customerIdentity", None)
        provenance["memory"] = {"origin": "source saved without customerMemory; initialized by current rules and "
                                          "Part 1 linked as the live backend would", "part1": detail,
                                "priorSaleLinked": linked}
    session["investigationEvidence"] = sorted(fact for fact, record in session["customerMemory"]["facts"].items()
                                              if record.get("shared"))
    return session, provenance


def _set_shared(session: dict, fact: str, turn_id: str | None) -> None:
    record = session["customerMemory"]["facts"].get(fact)
    if record is None:
        return
    record.update(shared=turn_id is not None, sharedTurnId=turn_id)


def apply_prior_turn(session: dict, turn: Mapping, ledger_entry: Mapping | None) -> dict:
    """Commit one saved turn as the pipeline would, keeping Lan's saved words."""
    turn_id = turn["turnId"]
    labels, label_provenance = labels_for(turn, ledger_entry)
    candidate, decision = evaluate(session, labels, turn_id)
    if decision.get("endingReason"):
        # The saved conversation continued after this turn, so a current-rule
        # ending here comes from incompatible (usually legacy keyword) credit
        # labels. Keep conduct/safety evidence; drop credit-only labels.
        dropped = sorted(name for name in GOOD_LABELS if labels.get(name, {}).get("status") == "true")
        labels = {name: (dict(value, status="false", source="replay-credit-dropped") if name in dropped else value)
                  for name, value in labels.items()}
        label_provenance["terminalConflictCreditLabelsDropped"] = dropped
        label_provenance["terminalConflictEnding"] = decision["endingReason"]
        candidate, decision = evaluate(session, labels, turn_id)
    plan = plan_reply(session, candidate, decision, labels)
    for key in COMMIT_KEYS:
        if key in candidate:
            session[key] = copy.deepcopy(candidate[key])
    if not decision.get("assessmentUncertain", decision["turnQuality"] == "uncertain"):
        session["acceptedTurnCount"] += 1
    if decision["turnQuality"] in ("good", "bad"):
        session["goodResponseCount" if decision["turnQuality"] == "good" else "badResponseCount"] += 1
    entry = copy.deepcopy(candidate["evidenceLedger"][-1])
    session["evidenceLedger"] = list(session.get("evidenceLedger") or []) + [entry]
    session["turnIds"].append(turn_id)
    session["turns"].append({"turnId": turn_id, "transcript": turn.get("transcript") or "",
                             "customerText": turn.get("customerText") or ""})
    # The saved reply is authoritative for what Lan disclosed.
    historical = set(turn.get("disclosedFactIds") or []) | set(turn.get("volunteeredFactIds") or [])
    historical &= set(FACTS)
    derived = set(decision.get("disclosedFactIds") or [])
    for fact in historical:
        if not session["customerMemory"]["facts"].get(fact, {}).get("shared"):
            _set_shared(session, fact, turn_id)
    for fact in derived - historical:
        if session["customerMemory"]["facts"].get(fact, {}).get("sharedTurnId") == turn_id:
            _set_shared(session, fact, None)
    session["investigationEvidence"] = sorted(fact for fact, record in session["customerMemory"]["facts"].items()
                                              if record.get("shared"))
    extra = sorted(historical - derived)
    if extra:
        entry["volunteeredFactIds"] = extra
    history = list((session.get("adaptiveMemory") or {}).get("history") or [])
    history.append({"turn": session.get("dialogueTurnCount"), "action": LEGACY_INTENT_ACTIONS.get(plan["intent"], "raise_concern"),
                    "decision": "continue", "factIds": [], "source": "legacy_prefix"})
    session["adaptiveMemory"] = {"version": ADAPTIVE_VERSION, "history": history[-8:]}
    return {"turnId": turn_id, "labels": label_provenance, "turnQuality": decision["turnQuality"],
            "endingReason": decision.get("endingReason"), "legacyIntent": plan["intent"],
            "derivedOnlyDisclosures": sorted(derived - historical), "historicalOnlyDisclosures": extra}


def reconstruct_case(case: Mapping, root: Path) -> dict:
    """Prefix state, selected-turn evaluation and legacy plan for one case."""
    path = root / case["sourceRelativePath"]
    source = json.loads(path.read_text(encoding="utf-8"))
    turns = source.get("turns") or []
    index = int(case["savedTurnIndex1Based"])
    selected = turns[index - 1]
    if selected.get("turnId") != case["turnId"]:
        raise ValueError(f"{case['caseId']}: turn {index} is not {case['turnId']}")
    prior = turns[:index - 1]
    ledger = {entry.get("turnId"): entry for entry in source.get("evidenceLedger") or [] if isinstance(entry, Mapping)}
    session, provenance = base_session(source, root, case["caseId"], [turn["turnId"] for turn in prior])
    provenance["priorTurns"] = []
    for turn in prior:
        item = apply_prior_turn(session, turn, ledger.get(turn["turnId"]))
        provenance["priorTurns"].append(item)
        if item["endingReason"]:
            raise ValueError(f"{case['caseId']}: prefix ends at turn {turn['turnId']} ({item['endingReason']})")
    before = session
    labels, label_provenance = labels_for(selected, ledger.get(selected["turnId"]))
    transcript = selected.get("transcript") or ""
    candidate, decision = evaluate(before, labels, selected["turnId"])
    plan = plan_reply(before, candidate, decision, labels)
    selection = select_customer_line(plan, transcript)
    plan["fallbackText"] = selection["text"]
    provenance.update(selectedLabels=label_provenance, memoryVersion=MEMORY_VERSION,
                      adaptiveMemory="derived from current planner intents for replayed prefix turns")
    return {"before": before, "candidate": candidate, "decision": decision, "plan": plan, "labels": labels,
            "transcript": transcript, "provenance": provenance}


def effective_manifest(manifest: Mapping, root: Path) -> dict:
    """Discovery manifest with the SA-01 amendment; replay inputs are rebuilt, not copied."""
    result = {key: copy.deepcopy(value) for key, value in manifest.items() if key != "cases"}
    result["schemaVersion"] = "sales-adaptive-benchmark-effective-v1"
    result["derivedFrom"] = "discovery/selection_manifest.json (unchanged)"
    result["amendments"] = [SA01_AMENDMENT]
    result["replayInputPolicy"] = ("Prefix state is rebuilt from each source file by scripts/sales_adaptive_replay.py. "
                                   "The discovery sessionMemorySnapshot is final session state and is not used.")
    cases = []
    for case in manifest["cases"]:
        case = copy.deepcopy(dict(case))
        case.pop("replayInput", None)
        if case["caseId"] == SA01_AMENDMENT["caseId"] and case["turnId"] == SA01_AMENDMENT["fromTurnId"]:
            source = json.loads((root / case["sourceRelativePath"]).read_text(encoding="utf-8"))
            turn = source["turns"][SA01_AMENDMENT["toTurnIndex"] - 1]
            if turn["turnId"] != SA01_AMENDMENT["toTurnId"]:
                raise ValueError("SA-01 amendment turn mismatch")
            case.update(savedTurnIndex1Based=SA01_AMENDMENT["toTurnIndex"], turnId=turn["turnId"],
                        caseRationale=("Repeated no-receipt refusal (fourth consecutive refusal) with two dialogue "
                                       "turns remaining. Tests whether Lan changes approach or leaves before the cap "
                                       "while keeping policy limits separate from the pain complaint."),
                        historicalReference={"customerText": turn.get("customerText"),
                                             "turnAssessment": turn.get("turnAssessment"),
                                             "disclosedFactIds": turn.get("disclosedFactIds") or [],
                                             "conversationComplete": bool(turn.get("conversationComplete"))},
                        amendment=SA01_AMENDMENT["reason"])
        cases.append(case)
    result["cases"] = cases
    return result
