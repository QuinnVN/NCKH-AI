"""Replay saved problem utterances through the deployed hybrid policy. No gameplay writes."""
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings
from app.sales_arbitration import LunaSalesArbitrator, merge_adjudication, select_arbitration_labels
from app.sales_openrouter import ARBITRATOR_VERSION, JevSalesClassifier, QUESTIONS, SalesClassifierError
from app.sales_rubric import VERSIONS, evaluate, normalized_labels

ALIASES = {"apology": "acknowledgment", "emotionalAcknowledgment": "acknowledgment",
    "useOrDurationQuestion": "walkingQuestion", "fitConditionOrPreferenceQuestion": "fitQuestion",
    "verificationStep": "fitOrWalkTrial", "profanityOrInsult": "abuse", "condescending": "disrespect"}


async def replay(source, output):
    cases = json.loads(source.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    settings = get_settings()
    manifest = {"versions": VERSIONS, "arbitratorVersion": ARBITRATOR_VERSION, "questions": QUESTIONS,
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(), "source": str(source.resolve()),
        "timeouts": {"jev": settings.sales_jev_timeout_seconds, "luna": settings.sales_luna_timeout_seconds}}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    jev, luna = JevSalesClassifier(settings), LunaSalesArbitrator(settings)
    results = []
    for index, case in enumerate(cases, 1):
        state = copy.deepcopy(case["reconstructedStateBefore"])
        for phase, evidence in state.get("objectiveEvidence", {}).items():
            state["objectiveEvidence"][phase] = {ALIASES.get(name, name): value for name, value in evidence.items()
                                                if ALIASES.get(name, name) in QUESTIONS}
        state.update(VERSIONS)
        context = case["context"]
        transcript = context["current_player_utterance"]
        request_context = {"objective": state["phase"], "knownFacts": context.get("knownFacts", []),
            "history": context.get("priorDialogue", []), "unresolvedPromiseTypes": context.get("unresolvedPromiseTypes", [])}
        record = {"caseId": case["caseId"], "caseIndex": index, "transcript": transcript,
            "stateBefore": state, "originalQuality": case["original"]["turnQuality"], "versions": VERSIONS,
            "arbitratorVersion": ARBITRATOR_VERSION}
        started = time.monotonic()
        try:
            classification = await jev.classify(transcript, request_context)
            base = normalized_labels(classification)
            selected = select_arbitration_labels(state, base)
            adjudication = await luna.adjudicate(transcript, request_context, selected)
            merged = merge_adjudication(classification, adjudication)
            _, base_decision = evaluate(state, base, f"replay-{index}")
            _, hybrid_decision = evaluate(state, normalized_labels(merged), f"replay-{index}")
            record.update(jev=classification, selectedLabels=selected, luna=adjudication,
                resolvedLabels=normalized_labels(merged), jevDecision=base_decision, hybridDecision=hybrid_decision)
        except SalesClassifierError as exc:
            record["errorCode"] = exc.code
        record["durationSeconds"] = time.monotonic() - started
        (output / f"case-{index:02d}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(record)
        print(json.dumps({"case": index, "selected": record.get("selectedLabels"),
            "jev": record.get("jevDecision", {}).get("turnQuality"), "hybrid": record.get("hybridDecision", {}).get("turnQuality"),
            "error": record.get("errorCode", record.get("luna", {}).get("metadata", {}).get("errorCode")),
            "seconds": round(record["durationSeconds"], 3)}, ensure_ascii=False), flush=True)
    completed = [row for row in results if "hybridDecision" in row]
    routed = [row for row in completed if row["selectedLabels"]]
    costs = [row.get(stage, {}).get("metadata", {}).get("costUsd") for row in completed
             for stage in (("jev", "luna") if row["selectedLabels"] else ("jev",))]
    summary = {"source": str(source.resolve()), "cases": len(cases), "completed": len(completed), "routed": len(routed),
        "jevUncertain": sum(row["jevDecision"]["assessmentUncertain"] for row in completed),
        "hybridUncertain": sum(row["hybridDecision"]["assessmentUncertain"] for row in completed),
        "medianSeconds": statistics.median(row["durationSeconds"] for row in completed) if completed else None,
        "lunaMedianSeconds": statistics.median(row["luna"]["metadata"]["durationSeconds"] for row in routed) if routed else None,
        "reportedCostUsd": sum(value for value in costs if isinstance(value, (float, int))),
        "missingCostEntries": sum(value is None for value in costs), "versions": VERSIONS,
        "note": "Selected development cases, fixed historical context. No independent gold labels or headset playback measurement."}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(replay(args.cases, args.output))
