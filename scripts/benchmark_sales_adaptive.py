"""Generation-only replay benchmark: adaptive Lan with Qwen thinking off vs on.

Each case is a fixed saved prefix plus the recorded salesperson utterance
(scripts/sales_adaptive_replay.py). Only Lan's response generation is timed:
no STT, Jev/Luna classification or TTS runs here, so this is NOT an
end-to-end gameplay latency measurement.

Arms (identical prompt, schema, context, temperature, max_tokens, timeout and
provider; only ``reasoning`` differs):
  adaptive_off  reasoning {"enabled": false}
  adaptive_on   reasoning {"enabled": true, "max_tokens": <budget>}
Optional --include-legacy adds legacy_220_off: the production plan+writer path
(220 tokens, reasoning off, 4 s). Its prompt, architecture and budget differ, so
it is excluded from the causal thinking comparison.

  dry-run  rebuilds every case, verifies source hashes and payload identity,
           and exercises the whole sample/summary path with a SIMULATED local
           transport. No network, no key, no cost.
  live     paid OpenRouter calls, serial, seeded interleaved AB/BA order.
           Requires --confirm-paid. --resume <run dir> skips finished samples.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.sales_adaptive import (PROMPT_SHA256, SCHEMA_SHA256, AdaptiveLanResponder, AdaptiveRequestConfig,  # noqa: E402
                                attempt_record, build_messages, build_payload, model_context, run_adaptive_turn,
                                sha256_text, turn_policy)
from app.sales_openrouter import QWEN_MODEL, QwenSalesWriter, customer_text_rejections  # noqa: E402
from scripts.sales_adaptive_replay import effective_manifest, reconstruct_case, sha256_file  # noqa: E402

OUTPUT_ROOT = Path(r"D:\NCKH\output\sales-adaptive-benchmark")
DEFAULT_MANIFEST = OUTPUT_ROOT / "discovery" / "selection_manifest.json"
ADAPTIVE_ARMS = ("adaptive_off", "adaptive_on")
LEGACY_ARM = "legacy_220_off"
SAMPLE_SCHEMA = "sales-adaptive-benchmark-sample-v1"
# Public OpenRouter endpoint metadata observed 2026-10-28 for qwen/qwen3.7-flash.
# Reference only: reported costs always come from usage.cost.
RATE_CARD_REFERENCE = {
    "observedAt": "2026-10-28", "model": QWEN_MODEL,
    "alibaba": {"promptUsdPerToken": 3e-8, "completionUsdPerToken": 1.3e-7, "cacheReadUsdPerToken": 6e-9,
                "tierOverrides": [{"minPromptTokens": 32000, "prompt": 1e-7, "completion": 4e-7},
                                  {"minPromptTokens": 256000, "prompt": 2e-7, "completion": 8e-7}]},
    "alibaba/us-east-1": {"promptUsdPerToken": 3.45e-8, "completionUsdPerToken": 1.495e-7},
    "reasoning": {"mandatory": False, "default_enabled": True, "supports_max_tokens": True},
}


# ---------------------------------------------------------------- configuration

def arm_configs(args) -> dict[str, AdaptiveRequestConfig]:
    provider = None if args.no_provider_pin else {"order": [args.provider], "allow_fallbacks": False,
                                                   "require_parameters": True}
    base = AdaptiveRequestConfig(max_tokens=args.max_tokens, reasoning_max_tokens=args.reasoning_budget or None,
                                 timeout_seconds=args.timeout, temperature=args.temperature, provider=provider)
    return {"adaptive_off": replace(base, reasoning="off"), "adaptive_on": replace(base, reasoning="on")}


def payload_sha_without_reasoning(payload: Mapping) -> str:
    stripped = {key: value for key, value in payload.items() if key != "reasoning"}
    return sha256_text(json.dumps(stripped, ensure_ascii=False, sort_keys=True))


def provider_preferences(settings, config: AdaptiveRequestConfig) -> dict:
    return AdaptiveLanResponder(settings, config=config).provider_preferences()


# ---------------------------------------------------------------- cases

def build_cases(manifest_path: Path, project_root: Path, case_filter: set[str] | None = None) -> tuple[dict, list[dict], dict]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    checks = {"sourceHashBefore": {}, "sourceHashAfter": {}, "errors": []}
    for case in manifest["cases"]:
        actual = sha256_file(project_root / case["sourceRelativePath"])
        checks["sourceHashBefore"][case["caseId"]] = actual == case["sourceSha256"]
        if actual != case["sourceSha256"]:
            checks["errors"].append(f"{case['caseId']}: source hash differs before replay")
    effective = effective_manifest(manifest, project_root)
    cases = []
    for case in effective["cases"]:
        if case_filter and case["caseId"] not in case_filter:
            continue
        rebuilt = reconstruct_case(case, project_root)
        policy = turn_policy(rebuilt["before"], rebuilt["candidate"], rebuilt["decision"], rebuilt["plan"], rebuilt["labels"])
        context = model_context(rebuilt["before"], rebuilt["candidate"], rebuilt["decision"], rebuilt["plan"],
                                rebuilt["labels"], rebuilt["transcript"], policy)
        messages = build_messages(context)
        cases.append({"case": case, "rebuilt": rebuilt, "policy": policy, "context": context, "messages": messages,
                      "contextSha256": sha256_text(messages[1]["content"])})
    for case in manifest["cases"]:
        actual = sha256_file(project_root / case["sourceRelativePath"])
        checks["sourceHashAfter"][case["caseId"]] = actual == case["sourceSha256"]
        if actual != case["sourceSha256"]:
            checks["errors"].append(f"{case['caseId']}: source hash changed during replay")
    sessions = {item["case"]["sessionId"] for item in cases}
    checks["distinctSessions"] = len(sessions)
    checks["caseCount"] = len(cases)
    if not case_filter and (len(sessions) != len(cases) or len(cases) != len(manifest["cases"])):
        checks["errors"].append("cases do not cover distinct sessions one-to-one")
    checks["leakage"] = {item["case"]["caseId"]: leakage_findings(item) for item in cases}
    for case_id, findings in checks["leakage"].items():
        if findings:
            checks["errors"].append(f"{case_id}: model input leaks {findings}")
    return effective, cases, checks


def leakage_findings(item: Mapping) -> list[str]:
    """The model input must not contain the selected reply, later turns, scores or private IDs."""
    text = "\n".join(message["content"] for message in item["messages"])
    case = item["case"]
    findings = []
    reference = (case.get("historicalReference") or {}).get("customerText")
    if reference and reference in text:
        findings.append("historical_selected_reply")
    source = json.loads((ROOT / case["sourceRelativePath"]).read_text(encoding="utf-8")) if (ROOT / case["sourceRelativePath"]).exists() else {}
    index = int(case["savedTurnIndex1Based"])
    turns = source.get("turns") or []
    # Lan sometimes repeated an earlier line verbatim; text already in the
    # authentic prefix is not future knowledge.
    prefix = "\n".join([source.get("openingComplaint") or ""] + [str(turn.get(key) or "") for turn in turns[:index - 1]
                                                                 for key in ("transcript", "customerText")]
                       + [str(turns[index - 1].get("transcript") or "")] if turns else [])
    # Authored fact texts Lan may legitimately know this turn are not leakage either.
    facts = (item.get("context") or {}).get("facts") or {}
    prefix += "\n" + "\n".join([*(facts.get("sharedFacts") or {}).values(), *(facts.get("mayDiscloseNow") or {}).values()])
    for turn in turns[index:]:
        for key in ("transcript", "customerText"):
            value = turn.get(key)
            if isinstance(value, str) and len(value) > 20 and value in text and value not in prefix:
                findings.append("future_turn_" + key)
    for key in ("sessionId", "runId", "part1AttemptId", "participantName"):
        value = source.get(key) or case.get(key)
        if isinstance(value, str) and value and value in text:
            findings.append("private_id_" + key)
    for marker in ('"score"', '"customerRating"', '"trustState"', '"rubricComponents"', '"turnQuality"', '"noul"'):
        if marker in text:
            findings.append("scoring_field_" + marker.strip('"'))
    return sorted(set(findings))


def schedule(case_ids: list[str], repeats: int, seed: int, include_legacy: bool) -> list[dict]:
    """Serial, interleaved order. Each case alternates AB/BA across repeats
    (balanced, seeded); case order is reshuffled per repeat."""
    rng = random.Random(seed)
    orders = {}
    for case_id in case_ids:
        values = (["AB", "BA"] * math.ceil(repeats / 2))[:repeats]
        if repeats % 2:
            values[-1] = rng.choice(["AB", "BA"])
        rng.shuffle(values)
        orders[case_id] = values
    plan, seq = [], 0
    for repeat in range(1, repeats + 1):
        case_order = list(case_ids)
        rng.shuffle(case_order)
        for case_id in case_order:
            order = orders[case_id][repeat - 1]
            arms = list(ADAPTIVE_ARMS) if order == "AB" else list(reversed(ADAPTIVE_ARMS))
            if include_legacy:
                arms.insert(rng.randrange(len(arms) + 1), LEGACY_ARM)
            for position, arm in enumerate(arms):
                seq += 1
                plan.append({"seq": seq, "repeat": repeat, "caseId": case_id, "arm": arm,
                             "blockOrder": order, "positionInBlock": position})
    return plan


# ---------------------------------------------------------------- transport capture

class RecordingClient:
    """Wraps an httpx client and keeps safe per-request diagnostics only."""

    def __init__(self, client):
        self.client = client
        self.records: list[dict] = []

    async def post(self, url, *, headers=None, json=None, timeout=None, follow_redirects=False):
        started = time.monotonic()
        record = {"maxTokens": (json or {}).get("max_tokens"), "reasoning": copy.deepcopy((json or {}).get("reasoning")),
                  "payloadSha256ExcludingReasoning": payload_sha_without_reasoning(json or {})}
        try:
            response = await self.client.post(url, headers=headers, json=json, timeout=timeout,
                                               follow_redirects=follow_redirects)
        except BaseException as exc:  # includes cancellation by the deadline
            record.update(errorCode=type(exc).__name__, httpStatus=None, latencySeconds=round(time.monotonic() - started, 4),
                          usage=attempt_record(None, index=0, kind="", started=0, finished=0)["usage"])
            self.records.append(record)
            raise
        record["latencySeconds"] = round(time.monotonic() - started, 4)
        record["httpStatus"] = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        safe = attempt_record(body if isinstance(body, dict) else None, index=0, kind="", started=0, finished=0)
        record.update({key: safe[key] for key in ("servedModel", "provider", "requestId", "finishReason",
                                                  "contentChars", "reasoningChars", "usage")})
        record["errorCode"] = None if response.status_code == 200 else f"http_{response.status_code}"
        self.records.append(record)
        return response


def simulated_transport() -> httpx.MockTransport:
    """Local stand-in for dry-run: plausible JSON, synthetic usage. Never model output."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        reasoning = (payload.get("reasoning") or {}).get("enabled")
        user = next(message["content"] for message in payload["messages"] if message["role"] == "user")
        try:
            context = json.loads(user)
        except ValueError:
            context = None
        if isinstance(context, dict) and "allowedActions" in context:
            facts = context["facts"]
            must = facts["mustAnswerFactIds"]
            texts = {**facts["sharedFacts"], **facts["mayDiscloseNow"]}
            prefix = " ".join(texts[fact] for fact in must if fact in texts)
            ending = context["limits"]["deterministicEnding"]
            allowed = context["allowedActions"]
            if ending:
                content = {"action": "close_conversation", "decision": "close", "closeReason": "deterministic_ending",
                           "customerText": (prefix + " Chị dừng ở đây, chuyện này vẫn chưa giải quyết được.").strip(),
                           "factIds": must}
            else:
                action = "ask_clarification" if "ask_clarification" in allowed else allowed[0]
                content = {"action": action, "decision": "continue", "closeReason": None, "factIds": must,
                           "customerText": (prefix + " Ý em cụ thể là sao, em nói rõ cho chị được không?").strip()}
            text = json.dumps(content, ensure_ascii=False)
        else:
            text = "Ý em cụ thể là sao, em nói rõ cho chị được không?"
        completion = 60 + (120 if reasoning else 0)
        body = {"id": "sim-" + hashlib.sha256(request.content).hexdigest()[:12], "model": payload["model"],
                "provider": "Simulated", "choices": [{"finish_reason": "stop", "message": {"content": text}}],
                "usage": {"prompt_tokens": len(request.content) // 4, "completion_tokens": completion,
                          "total_tokens": len(request.content) // 4 + completion,
                          "completion_tokens_details": {"reasoning_tokens": 120 if reasoning else 0},
                          "prompt_tokens_details": {"cached_tokens": 0}, "cost": None}}
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- samples

def _token_totals(records: list[dict]) -> dict:
    fields = ("promptTokens", "completionTokens", "reasoningTokens", "cachedTokens", "totalTokens")
    totals = {}
    for name in fields:
        values = [record["usage"].get(name) for record in records]
        known = [value for value in values if value is not None]
        totals[name] = sum(known) if known else None
        totals[name + "Missing"] = len(values) - len(known)
    return totals


def _cost(records: list[dict]) -> tuple[float | None, int]:
    costs = [record["usage"].get("costUsd") for record in records]
    missing = sum(1 for cost in costs if cost is None)
    return (sum(costs) if costs and not missing else None), missing


def _base_sample(item: Mapping, slot: Mapping, arm: str, simulated: bool) -> dict:
    return {"schemaVersion": SAMPLE_SCHEMA, "simulated": simulated, **{key: slot[key] for key in
            ("seq", "repeat", "caseId", "arm", "blockOrder", "positionInBlock")},
            "startedAtUtc": datetime.now(timezone.utc).isoformat(),
            "causalComparison": arm in ADAPTIVE_ARMS,
            "architecture": "adaptive-single-call" if arm in ADAPTIVE_ARMS else "legacy-plan-writer",
            "deterministic": {"legacyIntent": item["rebuilt"]["plan"]["intent"],
                              "deterministicEnding": item["policy"]["deterministicEnding"],
                              "closeAllowed": item["policy"]["closeAllowed"],
                              "allowedActions": item["policy"]["allowedActions"],
                              "requiredFactIds": item["policy"]["requiredFactIds"]}}


def _finish(sample: dict, records: list[dict], wall: float) -> dict:
    cost, missing = _cost(records)
    sample.update(httpAttempts=records, attemptCount=len(records), retryCount=max(0, len(records) - 1),
                  failedAttempts=sum(1 for record in records if record.get("errorCode")),
                  firstAttemptLatencySeconds=records[0]["latencySeconds"] if records else None,
                  requestLatencySeconds=[record["latencySeconds"] for record in records],
                  wallClockReadySeconds=round(wall, 4), tokens=_token_totals(records), costUsd=cost,
                  costMissingAttempts=missing,
                  servedProviders=sorted({record.get("provider") for record in records if record.get("provider")}),
                  servedModels=sorted({record.get("servedModel") for record in records if record.get("servedModel")}),
                  finishReasons=[record.get("finishReason") for record in records])
    return sample


async def run_adaptive_sample(item: Mapping, slot: Mapping, config: AdaptiveRequestConfig, settings, http_client,
                              simulated: bool) -> dict:
    sample = _base_sample(item, slot, slot["arm"], simulated)
    recorder = RecordingClient(http_client)
    responder = AdaptiveLanResponder(settings, client=recorder, config=config)
    payload = build_payload(item["messages"], config, responder.provider_preferences())
    sample.update(requestConfig=config.redacted(), promptSha256=PROMPT_SHA256, schemaSha256=SCHEMA_SHA256,
                  contextSha256=item["contextSha256"], payloadSha256ExcludingReasoning=payload_sha_without_reasoning(payload))
    rebuilt = item["rebuilt"]
    started = time.monotonic()
    try:
        result = await run_adaptive_turn(rebuilt["before"], rebuilt["candidate"], rebuilt["decision"], rebuilt["plan"],
                                         rebuilt["labels"], rebuilt["transcript"], responder=responder, config=config)
    except Exception as exc:  # harness failure, not a gameplay fallback
        sample.update(outcome="harness_error", harnessError=type(exc).__name__)
        return _finish(sample, recorder.records, time.monotonic() - started)
    wall = time.monotonic() - started
    record = result["record"]
    sample.update(contextSha256Matches=record["contextSha256"] == item["contextSha256"])
    sample.update(outcome=record["source"], fallbackReason=record["fallbackReason"],
                  rejectionCodes=record["metadata"].get("rejectionCodes") or [],
                  responderAttempts=[{key: attempt.get(key) for key in ("index", "kind", "errorCode", "finishReason",
                                                                        "rejectionCodes", "latencySeconds")}
                                     for attempt in record["metadata"].get("attempts") or []],
                  output=dict(record["output"], customerText=result["written"]["customerText"]),
                  softClose=record["softClose"])
    return _finish(sample, recorder.records, wall)


class PinnedQwenWriter(QwenSalesWriter):
    def __init__(self, *args, provider_extra=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider_extra = provider_extra or {}

    def _provider(self) -> dict:
        preferences = super()._provider()
        preferences.update(self.provider_extra)
        return preferences


async def run_legacy_sample(item: Mapping, slot: Mapping, settings, http_client, provider_extra, timeout: float,
                            simulated: bool) -> dict:
    sample = _base_sample(item, slot, LEGACY_ARM, simulated)
    recorder = RecordingClient(http_client)
    writer = PinnedQwenWriter(replace(settings, sales_writer_timeout_seconds=timeout), client=recorder,
                              provider_extra=provider_extra)
    rebuilt = item["rebuilt"]
    plan = rebuilt["plan"]
    sample.update(requestConfig={"model": QWEN_MODEL, "max_tokens": 220, "reasoning": {"enabled": False},
                                 "temperature": 0.4, "timeout_seconds": timeout, "provider": provider_extra or None,
                                 "note": "production legacy writer request; prompt/architecture/budget differ"})
    started = time.monotonic()
    try:
        written = await asyncio.wait_for(writer.write(plan, rebuilt["transcript"], rebuilt["before"].get("turns", [])[-3:]),
                                         timeout + .25)
        rejected = customer_text_rejections(written.get("customerText", ""), plan, rebuilt["transcript"])
        if rejected:
            written = {"customerText": plan["fallbackText"], "fallbackUsed": True,
                       "metadata": dict(written.get("metadata") or {}, errorCode="writer_guard_rejected")}
    except Exception as exc:
        written = {"customerText": plan["fallbackText"], "fallbackUsed": True,
                   "metadata": {"errorCode": "writer_timeout" if isinstance(exc, TimeoutError) else "writer_failed"}}
    wall = time.monotonic() - started
    metadata = written.get("metadata") or {}
    sample.update(outcome="fallback" if written.get("fallbackUsed") else "model",
                  fallbackReason=(metadata.get("errorCode") or ("writer_guard_rejected" if metadata.get("rejectionCodes")
                                  else "writer_fallback")) if written.get("fallbackUsed") else None,
                  rejectionCodes=metadata.get("rejectionCodes") or [],
                  output={"action": None, "legacyIntent": plan["intent"], "decision": "close" if plan.get("ending") else "continue",
                          "customerText": written["customerText"]})
    return _finish(sample, recorder.records, wall)


# ---------------------------------------------------------------- summary

def quantile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy default)."""
    data = sorted(value for value in values if value is not None)
    if not data:
        return None
    position = (len(data) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return data[low] + (data[high] - data[low]) * (position - low)


def stats(values: list[float]) -> dict:
    data = [value for value in values if value is not None]
    if not data:
        return {"n": 0, "mean": None, "median": None, "p90": None, "p95": None, "min": None, "max": None}
    return {"n": len(data), "mean": sum(data) / len(data), "median": quantile(data, .5), "p90": quantile(data, .9),
            "p95": quantile(data, .95), "min": min(data), "max": max(data)}


def summarize(samples: list[dict], meta: Mapping) -> dict:
    arms = {}
    for arm in sorted({sample["arm"] for sample in samples}):
        group = [sample for sample in samples if sample["arm"] == arm]
        records = [record for sample in group for record in sample.get("httpAttempts", [])]
        costs = [sample.get("costUsd") for sample in group]
        known_costs = [cost for cost in costs if cost is not None]
        tokens = _token_totals(records) if records else {}
        arms[arm] = {
            "causalComparison": arm in ADAPTIVE_ARMS, "samples": len(group),
            "outcomes": dict(Counter(sample.get("outcome") for sample in group)),
            "fallbackRate": sum(1 for sample in group if sample.get("outcome") == "fallback") / len(group),
            "fallbackReasons": dict(Counter(sample.get("fallbackReason") for sample in group if sample.get("fallbackReason"))),
            "httpAttempts": len(records), "retries": sum(sample.get("retryCount", 0) for sample in group),
            "failedAttempts": sum(sample.get("failedAttempts", 0) for sample in group),
            "wallClockReadySeconds": stats([sample.get("wallClockReadySeconds") for sample in group]),
            "firstAttemptLatencySeconds": stats([sample.get("firstAttemptLatencySeconds") for sample in group]),
            "requestLatencySeconds": stats([record.get("latencySeconds") for record in records]),
            "tokens": tokens,
            "tokensPerSampleMean": {name: (tokens[name] / len(group) if tokens.get(name) is not None else None)
                                    for name in ("promptTokens", "completionTokens", "reasoningTokens", "cachedTokens")} if tokens else {},
            "samplesWithReasoningTokens": sum(1 for sample in group if (sample.get("tokens") or {}).get("reasoningTokens")),
            "costUsdKnownTotal": sum(known_costs) if known_costs else None,
            "costUsdTotal": sum(known_costs) if known_costs and len(known_costs) == len(costs) else None,
            "costUsdMeanPerSample": sum(known_costs) / len(known_costs) if known_costs else None,
            "samplesMissingCost": len(costs) - len(known_costs),
            "attemptsMissingCost": sum(sample.get("costMissingAttempts", 0) for sample in group),
            "servedProviders": dict(Counter(provider for sample in group for provider in sample.get("servedProviders", []))),
            "servedModels": dict(Counter(model for sample in group for model in sample.get("servedModels", []))),
            "finishReasons": dict(Counter(reason for sample in group for reason in sample.get("finishReasons", []))),
            "actions": dict(Counter((sample.get("output") or {}).get("action") for sample in group)),
            "decisions": dict(Counter((sample.get("output") or {}).get("decision") for sample in group)),
            "softCloses": sum(1 for sample in group if sample.get("softClose")),
        }
    paired = {}
    for case_id in sorted({sample["caseId"] for sample in samples}):
        row = {}
        for arm in ADAPTIVE_ARMS:
            group = [sample for sample in samples if sample["caseId"] == case_id and sample["arm"] == arm]
            if not group:
                continue
            costs = [sample.get("costUsd") for sample in group]
            row[arm] = {"wallMedian": quantile([sample.get("wallClockReadySeconds") for sample in group], .5),
                        "costMean": (sum(costs) / len(costs)) if costs and None not in costs else None,
                        "actions": [(sample.get("output") or {}).get("action") for sample in group],
                        "decisions": [(sample.get("output") or {}).get("decision") for sample in group],
                        "outcomes": [sample.get("outcome") for sample in group]}
        if all(arm in row for arm in ADAPTIVE_ARMS):
            off, on = row["adaptive_off"], row["adaptive_on"]
            row["wallMedianDeltaOnMinusOff"] = (on["wallMedian"] - off["wallMedian"]
                                                if on["wallMedian"] is not None and off["wallMedian"] is not None else None)
            row["costRatioOnOverOff"] = (on["costMean"] / off["costMean"]
                                         if on["costMean"] is not None and off["costMean"] else None)
        paired[case_id] = row
    return {"schemaVersion": "sales-adaptive-benchmark-summary-v1", **meta, "arms": arms, "pairedByCase": paired,
            "notes": ["Generation-only fixed-prefix replay; excludes STT, Jev/Luna classification and TTS.",
                      "Only adaptive_off vs adaptive_on is a causal thinking comparison (identical payload except reasoning).",
                      "legacy_220_off differs in prompt, architecture and token budget; descriptive baseline only.",
                      "costUsd is provider-reported usage.cost; null means unknown (never zero-filled).",
                      "Ten cases are qualitative pilot coverage, not a powered quality estimate."]}


def _fmt(value, digits=3):
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.{digits}f}" if abs(value) >= 0.001 or value == 0 else f"{value:.3e}"
    return str(value)


def render_markdown(summary: Mapping, samples: list[dict]) -> str:
    lines = [f"# Adaptive Lan benchmark ({summary.get('mode')})", ""]
    if summary.get("simulated"):
        lines += ["> SIMULATED dry-run: local mock transport. Texts, latencies, tokens and costs are NOT model results.", ""]
    lines += [f"- Run: `{summary.get('runId')}`  model `{summary.get('model')}`  seed {summary.get('seed')}  repeats {summary.get('repeats')}",
              f"- Prompt SHA-256 `{summary.get('promptSha256')}`; schema SHA-256 `{summary.get('schemaSha256')}`",
              f"- Off/on payloads identical except reasoning: **{summary.get('payloadIdenticalExceptReasoning')}**",
              f"- Source hashes unchanged: **{summary.get('sourceHashesUnchanged')}**; distinct sessions: {summary.get('distinctSessions')}",
              "", "## Arms", "",
              "| arm | causal | n | model/fallback/error | retries | wall median s | wall p90 s | first-attempt median s | completion tok mean | reasoning tok mean | cost total USD | missing-cost samples | providers |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm, data in summary["arms"].items():
        outcomes = data["outcomes"]
        lines.append(f"| {arm} | {data['causalComparison']} | {data['samples']} | {outcomes.get('model', 0)}/{outcomes.get('fallback', 0)}/{outcomes.get('harness_error', 0)} "
                     f"| {data['retries']} | {_fmt(data['wallClockReadySeconds']['median'])} | {_fmt(data['wallClockReadySeconds']['p90'])} "
                     f"| {_fmt(data['firstAttemptLatencySeconds']['median'])} | {_fmt((data['tokensPerSampleMean'] or {}).get('completionTokens'), 1)} "
                     f"| {_fmt((data['tokensPerSampleMean'] or {}).get('reasoningTokens'), 1)} | {_fmt(data['costUsdTotal'], 6)} "
                     f"| {data['samplesMissingCost']} | {data['servedProviders']} |")
    lines += ["", "## Paired by case (adaptive arms)", "", "| case | off wall median | on wall median | delta on-off | cost ratio on/off | off actions | on actions |",
              "|---|---|---|---|---|---|---|"]
    for case_id, row in summary["pairedByCase"].items():
        off, on = row.get("adaptive_off", {}), row.get("adaptive_on", {})
        lines.append(f"| {case_id} | {_fmt(off.get('wallMedian'))} | {_fmt(on.get('wallMedian'))} | {_fmt(row.get('wallMedianDeltaOnMinusOff'))} "
                     f"| {_fmt(row.get('costRatioOnOverOff'), 2)} | {', '.join(map(str, off.get('actions', [])))} | {', '.join(map(str, on.get('actions', [])))} |")
    lines += ["", "## Samples", "", "| seq | case | arm | rep | outcome | action | decision | wall s | Lan |", "|---|---|---|---|---|---|---|---|---|"]
    for sample in sorted(samples, key=lambda item: item["seq"]):
        output = sample.get("output") or {}
        text = str(output.get("customerText") or "").replace("|", "/")
        lines.append(f"| {sample['seq']} | {sample['caseId']} | {sample['arm']} | {sample['repeat']} | {sample.get('outcome')} "
                     f"| {output.get('action') or output.get('legacyIntent')} | {output.get('decision')} | {_fmt(sample.get('wallClockReadySeconds'))} | {text} |")
    lines += ["", "## Notes", ""] + [f"- {note}" for note in summary["notes"]]
    lines += ["- Historical Lan replies are in `grader_reference.json` (blinded reference, never model input)."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main

def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("dry-run", "live"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--out-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--run-dir", type=Path, help="explicit output directory")
    parser.add_argument("--resume", type=Path, help="existing live run directory to continue")
    parser.add_argument("--cases", help="comma-separated case IDs (default: all)")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--include-legacy", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--reasoning-budget", type=int, default=1024, help="0 = no reasoning.max_tokens")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--legacy-timeout", type=float, default=4.0)
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--provider", default="alibaba")
    parser.add_argument("--no-provider-pin", action="store_true")
    parser.add_argument("--confirm-paid", action="store_true", help="required for live mode")
    return parser.parse_args(argv)


async def main_async(args) -> int:
    simulated = args.mode == "dry-run"
    if not simulated and not args.confirm_paid:
        print("live mode makes paid OpenRouter calls; pass --confirm-paid", file=sys.stderr)
        return 2
    case_filter = set(args.cases.split(",")) if args.cases else None
    effective, cases, checks = build_cases(args.manifest, args.project_root, case_filter)
    _write_json(args.out_root / "effective_manifest.json", effective)
    if checks["errors"]:
        print("case checks failed: " + "; ".join(checks["errors"]), file=sys.stderr)
        return 3
    settings = get_settings()
    if simulated:
        settings = replace(settings, openrouter_api_key="dry-run-no-key")
    elif not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not configured", file=sys.stderr)
        return 2
    configs = arm_configs(args)
    identical = all(
        payload_sha_without_reasoning(build_payload(item["messages"], configs["adaptive_off"], provider_preferences(settings, configs["adaptive_off"])))
        == payload_sha_without_reasoning(build_payload(item["messages"], configs["adaptive_on"], provider_preferences(settings, configs["adaptive_on"])))
        for item in cases)
    if not identical:
        print("off/on payloads differ beyond reasoning", file=sys.stderr)
        return 3
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.resume or args.run_dir or args.out_root / "runs" / f"{args.mode}-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    by_id = {item["case"]["caseId"]: item for item in cases}
    slots = schedule(sorted(by_id), args.repeats, args.seed, args.include_legacy)
    provider_extra = None if args.no_provider_pin else {"order": [args.provider], "allow_fallbacks": False,
                                                         "require_parameters": True}
    meta = {"mode": args.mode, "simulated": simulated, "runId": run_dir.name, "model": QWEN_MODEL, "seed": args.seed,
            "repeats": args.repeats, "includeLegacy": args.include_legacy, "promptSha256": PROMPT_SHA256,
            "schemaSha256": SCHEMA_SHA256, "payloadIdenticalExceptReasoning": identical,
            "sourceHashesUnchanged": all(checks["sourceHashBefore"].values()) and all(checks["sourceHashAfter"].values()),
            "distinctSessions": checks["distinctSessions"],
            "armConfigs": {arm: {**config.redacted(), "providerPreferences": provider_preferences(settings, config)}
                           for arm, config in configs.items()},
            "legacyConfig": {"max_tokens": 220, "reasoning": {"enabled": False}, "timeout_seconds": args.legacy_timeout,
                             "provider": provider_extra} if args.include_legacy else None,
            "rateCardReference": RATE_CARD_REFERENCE, "plannedRequests": len(slots)}
    _write_json(run_dir / "config.json", {**meta, "checks": checks, "schedule": slots,
                                          "effectiveManifest": str(args.out_root / "effective_manifest.json")})
    for case_id, item in by_id.items():
        _write_json(run_dir / "cases" / f"{case_id}.json", {
            "caseId": case_id, "provenance": item["rebuilt"]["provenance"], "policy": item["policy"],
            "legacyPlan": {key: item["rebuilt"]["plan"].get(key) for key in ("intent", "requiredContent", "requiredFactIds", "fallbackText")},
            "contextSha256": item["contextSha256"], "modelContext": item["context"]})
    _write_json(run_dir / "grader_reference.json", {case_id: {"historicalCustomerText": (item["case"].get("historicalReference") or {}).get("customerText"),
                                                              "caseRationale": item["case"].get("caseRationale")}
                                                    for case_id, item in by_id.items()})
    samples_path = run_dir / "samples.jsonl"
    done = {}
    if samples_path.exists():
        for line in samples_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                sample = json.loads(line)
                if sample.get("outcome") != "harness_error":
                    done[(sample["caseId"], sample["arm"], sample["repeat"])] = sample
    transport = simulated_transport() if simulated else None
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http_client:
        with samples_path.open("a", encoding="utf-8") as output:
            for slot in slots:
                key = (slot["caseId"], slot["arm"], slot["repeat"])
                if key in done:
                    continue
                item = by_id[slot["caseId"]]
                if slot["arm"] == LEGACY_ARM:
                    sample = await run_legacy_sample(item, slot, settings, http_client, provider_extra, args.legacy_timeout, simulated)
                else:
                    sample = await run_adaptive_sample(item, slot, configs[slot["arm"]], settings, http_client, simulated)
                done[key] = sample
                output.write(json.dumps(sample, ensure_ascii=False) + "\n")
                output.flush()
                print(f"[{slot['seq']}/{len(slots)}] {slot['caseId']} {slot['arm']} r{slot['repeat']} "
                      f"{sample.get('outcome')} {sample.get('wallClockReadySeconds')}s", flush=True)
    samples = sorted(done.values(), key=lambda sample: sample["seq"])
    summary = summarize(samples, meta)
    _write_json(run_dir / "summary.json", summary)
    (run_dir / "summary.md").write_text(render_markdown(summary, samples), encoding="utf-8")
    print(f"wrote {run_dir}")
    return 0


def main(argv=None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
