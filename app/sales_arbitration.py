"""Selective, evidence-grounded Luna arbitration; gameplay remains deterministic."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence

from app.sales_openrouter import (ARBITRATOR_VERSION, CHAT_ENDPOINT, LUNA_MODEL,
    QUESTION_TEXT, SCENARIO, SalesClassifierError, _OpenRouterAdapter, _SCOPE,
    _fact_ids, _history, _metadata, _text)
from app.sales_concerns import (BAD_TURN_LABELS, FACT_QUESTIONS, RETRACTIONS, VIOLATIONS, critical_labels,
    score_ledger)

MAX_LABELS = 8
QUESTION_FACTS = {label: fact for label, fact in FACT_QUESTIONS if fact != "appearance"}


def select_arbitration_labels(state: Mapping, labels: Mapping) -> list[str]:
    """Route unresolved consequential acts and conflicting or subtle judgments."""
    status = lambda key: labels.get(key, {}).get("status", "uncertain")
    selected = []
    def add(*names):
        for name in names:
            if name in QUESTION_TEXT and name not in selected:
                selected.append(name)
    if status("exchangeOffer") == status("refusesRemedy") == "true":
        add("refusesRemedy", "exchangeOffer")
    active = set(state.get("unresolvedPromises", []))
    for retraction, code in RETRACTIONS.items():
        promise = next(name for name, value in VIOLATIONS.items() if value == code)
        if status(retraction) == "true" and (status(promise) == "true" or status("maintainsUnauthorizedPromise") == "true"):
            add(retraction, promise, "maintainsUnauthorizedPromise")
    # Soft tone judgments caused false penalties in the replay corpus. Verify
    # these even when Jev is certain; explicit abuse has a separate definition.
    if status("disrespect") == "true":
        add("disrespect")
    for name in (*VIOLATIONS, *BAD_TURN_LABELS):
        if status(name) == "uncertain":
            add(name)
    if active:
        for name in ("maintainsUnauthorizedPromise", *(n for n, code in RETRACTIONS.items() if code in active)):
            if status(name) == "uncertain":
                add(name)
    # Labels that would hold the turn for clarification in the current customer
    # state, plus unanswered fact questions that decide what Lan may say.
    customer = score_ledger(state.get("evidenceLedger") or [])["state"]
    known = set(state.get("investigationEvidence", []))
    statuses = {name: value.get("status", "uncertain") for name, value in labels.items() if isinstance(value, Mapping)}
    add(*critical_labels(customer, statuses, known, bool(state.get("challengeShown"))))
    for name, fact in QUESTION_FACTS.items():
        if fact not in known and status(name) == "uncertain":
            add(name)
    return selected[:MAX_LABELS]


def merge_adjudication(classification: Mapping, adjudication: Mapping) -> dict:
    """Keep raw Jev probabilities; only validated, determinate Luna acts override."""
    result = copy.deepcopy(dict(classification))
    accepted = []
    decisions = adjudication.get("labels", {}) if isinstance(adjudication, Mapping) else {}
    for name, decision in decisions.items() if isinstance(decisions, Mapping) else ():
        if (name not in QUESTION_TEXT or not isinstance(decision, Mapping)
                or decision.get("status") not in ("true", "false")
                or not isinstance(decision.get("evidence"), str)):
            continue
        raw = result.setdefault("labels", {}).setdefault(name, {})
        raw.update(source="luna", arbitration={"version": ARBITRATOR_VERSION,
                   "status": decision["status"], "evidence": decision["evidence"], "model": LUNA_MODEL})
        accepted.append(name)
    metadata = copy.deepcopy(adjudication.get("metadata", {})) if isinstance(adjudication, Mapping) else {}
    metadata.update(version=ARBITRATOR_VERSION, acceptedLabels=accepted)
    result.setdefault("metadata", {})["arbitration"] = metadata
    return result


def validate_decisions(transcript: str, selected: Sequence[str], decisions: Mapping) -> tuple[dict, dict]:
    """Ground quotes in consecutive original words, allowing punctuation only."""
    original = unicodedata.normalize("NFC", _text(transcript))
    words = list(re.finditer(r"[^\W_]+", original, re.UNICODE))
    tokens = [word.group().casefold() for word in words]
    accepted, reasons = {}, {}
    for name in selected:
        decision = decisions.get(name)
        if not isinstance(decision, Mapping) or set(decision) != {"status", "evidence"}:
            reasons[name] = "invalid_decision_schema"
            continue
        if decision.get("status") not in ("true", "false", "uncertain"):
            reasons[name] = "invalid_status"
            continue
        if not isinstance(decision.get("evidence"), str):
            reasons[name] = "invalid_evidence_type"
            continue
        quote = unicodedata.normalize("NFC", decision["evidence"])
        quoted = re.findall(r"[^\W_]+", quote.casefold(), re.UNICODE)
        if not quoted:
            if decision["status"] == "true" or quote.strip():
                reasons[name] = "missing_evidence" if not quote.strip() else "evidence_not_in_transcript"
                continue
            accepted[name] = dict(decision)
            continue
        index = next((i for i in range(len(tokens) - len(quoted) + 1)
                      if tokens[i:i + len(quoted)] == quoted), None)
        if index is None:
            reasons[name] = "evidence_not_in_transcript"
            continue
        accepted[name] = {"status": decision["status"],
                          "evidence": original[words[index].start():words[index + len(quoted) - 1].end()]}
    return accepted, reasons


class LunaSalesArbitrator(_OpenRouterAdapter):
    async def adjudicate(self, transcript: str, context: Mapping, requested: Sequence[str]) -> dict:
        selected = list(dict.fromkeys(name for name in requested if name in QUESTION_TEXT))[:MAX_LABELS]
        if not selected:
            return {"labels": {}, "metadata": {"skipped": True, "selectedLabels": []}}
        started = time.monotonic()
        body, attempts, accepted, rejected_decisions, reasons = {}, [], {}, {}, {}
        def result(error=None):
            metadata = _metadata(body, LUNA_MODEL, started, self.settings.openrouter_api_key)
            metadata.update(selectedLabels=selected, rejectedLabels=list(reasons), version=ARBITRATOR_VERSION,
                            rejectionReasons=reasons, rejectedDecisions=rejected_decisions, attempts=attempts)
            costs = [attempt.get("costUsd") for attempt in attempts]
            metadata["costUsd"] = sum(costs) if costs and all(cost is not None for cost in costs) else None
            if error:
                metadata["errorCode"] = error
            return {"labels": accepted, "metadata": metadata}
        item = {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["true", "false", "uncertain"]},
            "evidence": {"type": "string"}}, "required": ["status", "evidence"], "additionalProperties": False}
        schema = {"type": "object", "properties": {name: item for name in selected},
                  "required": selected, "additionalProperties": False}
        objective = context.get("objective", context.get("phase"))
        unresolved = context.get("unresolvedPromiseTypes", [])
        unresolved = unresolved if isinstance(unresolved, (list, tuple)) else []
        data = {"scenario": SCENARIO, "current_player_utterance": _text(transcript),
            "objective": objective if type(objective) is int and 1 <= objective <= 4 else None,
            "known_fact_ids": _fact_ids(context.get("knownFacts", [])),
            "prior_dialogue": _history(context.get("history", [])),
            "unresolved_promise_types": [name for name in unresolved if name in VIOLATIONS],
            "questions": {name: QUESTION_TEXT[name] for name in selected}}
        instruction = (_SCOPE + "Classify each supplied independent act as true, false or uncertain. "
            "Output only the requested JSON object. For true, quote a short exact span from "
            "current_player_utterance as evidence. For false, use an empty evidence string if the act is absent, "
            "or an exact current-utterance quote showing negation or correction. Do not quote prior dialogue. "
            "For ambiguous ASR use uncertain, never invent words. Do not infer disrespect or refusal merely "
            "from stopping investigation or complaining about repeated NPC questions while keeping an exchange offer. "
            "A generic explicit correction of an earlier promise can identify its type ONLY when there is exactly "
            "one unresolved promise. Evaluate final position after same-utterance corrections.")
        provider = {**self._provider(), "only": ["OpenAI"], "allow_fallbacks": False, "require_parameters": True}
        payload = {"model": LUNA_MODEL, "messages": [{"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "sales_act_arbitration", "strict": True, "schema": schema}},
            "reasoning": {"effort": "low", "exclude": True}, "max_tokens": 1024, "provider": provider}
        try:
            async with asyncio.timeout(self.settings.sales_luna_timeout_seconds):
                requested_now = selected
                for attempt_number in range(2):
                    attempt_started = time.monotonic()
                    remaining = self.settings.sales_luna_timeout_seconds - (attempt_started - started)
                    body = await self._post(CHAT_ENDPOINT, payload, max(.001, remaining))
                    attempt = _metadata(body, LUNA_MODEL, attempt_started, self.settings.openrouter_api_key)
                    attempts.append(attempt)
                    if body.get("model") != LUNA_MODEL:
                        return result(error="arbitrator_model_mismatch")
                    choices = body.get("choices", [])
                    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
                    message = choice.get("message", {})
                    content = message.get("content") if isinstance(message, Mapping) else None
                    if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                        return result(error="arbitrator_incomplete_response")
                    decisions = json.loads(content)
                    if not isinstance(decisions, dict) or set(decisions) - set(requested_now):
                        return result(error="arbitrator_invalid_schema")
                    valid, reasons = validate_decisions(transcript, requested_now, decisions)
                    accepted.update(valid)
                    key = self.settings.openrouter_api_key
                    rejected_decisions = {name: json.dumps(decisions.get(name), ensure_ascii=False).replace(key, "[REDACTED]")[:1500]
                                          if key else json.dumps(decisions.get(name), ensure_ascii=False)[:1500] for name in reasons}
                    attempt.update(selectedLabels=list(requested_now), rejectionReasons=dict(reasons), rejectedDecisions=dict(rejected_decisions))
                    if not reasons or attempt_number or self.settings.sales_luna_timeout_seconds - (time.monotonic() - started) < .25:
                        break
                    requested_now = list(reasons)
                    retry_data = {**data, "questions": {name: QUESTION_TEXT[name] for name in requested_now}}
                    retry_schema = {"type": "object", "properties": {name: item for name in requested_now}, "required": requested_now, "additionalProperties": False}
                    payload = {**payload, "messages": [payload["messages"][0],
                        {"role": "user", "content": json.dumps(retry_data, ensure_ascii=False)},
                        {"role": "user", "content": "Repair only these validation failures: " + json.dumps(reasons) + ". Quote consecutive original words; do not paraphrase or add words. If absent use false with empty evidence; if ambiguous use uncertain."}],
                        "response_format": {"type": "json_schema", "json_schema": {"name": "sales_act_arbitration", "strict": True, "schema": retry_schema}}}
            return result()
        except (TimeoutError, SalesClassifierError) as exc:
            return result(error="arbitrator_timeout" if isinstance(exc, TimeoutError) else exc.code)
        except (ValueError, TypeError):
            return result(error="arbitrator_invalid_schema")
