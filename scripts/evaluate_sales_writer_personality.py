"""Replay customer-writing plans only; never change recorded gameplay decisions."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings
from app.sales_openrouter import QwenSalesWriter, PROMPT_VERSION
from app.sales_pipeline import plan_reply
from app.sales_rubric import evaluate, initialize, normalized_labels, ORDINARY, SAFETY


async def replay(source, output):
    data = json.loads(source.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    state = {"phase": 1, "status": "active", "turns": [], "investigationEvidence": [], "policyViolations": []}
    initialize(state, get_settings())
    state["pipelineMode"] = "openrouter"
    writer = QwenSalesWriter()
    probes = [(t["transcript"], t["turnAssessment"], t["customerText"]) for t in data["turns"]]
    for utterance, acts in (("Chị thường đi bộ hằng ngày bao lâu ạ?", ["walkingQuestion"]),
                            ("Em không đổi hàng cho chị đâu, chị đi về đi", ["refusesRemedy"])):
        probes.append((utterance, {name: {"noul": float(name in acts)} for name in ORDINARY + SAFETY}, None))
    records = []
    for index, (transcript, raw, old_text) in enumerate(probes, 1):
        labels = normalized_labels({"labels": raw})
        candidate, decision = evaluate(state, labels, f"probe-{index}")
        plan = plan_reply(state, candidate, decision, labels)
        written = await writer.write(plan, transcript, state["turns"])
        record = {"index": index, "sourceSession": data["sessionId"], "transcript": transcript,
            "previousCustomerText": old_text, "plan": plan, "written": written, "promptVersion": PROMPT_VERSION}
        (output / f"case-{index:02d}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        records.append(record)
        print(json.dumps({"case": index, "disposition": plan["customerDisposition"], "reply": written["customerText"],
            "fallback": written["fallbackUsed"], "seconds": written["metadata"]["durationSeconds"]}, ensure_ascii=False), flush=True)
        candidate["turns"] = state["turns"] + [{"transcript": transcript, "customerText": written["customerText"]}]
        state = candidate
    summary = {"cases": len(records), "fallbackCount": sum(r["written"]["fallbackUsed"] for r in records),
        "reportedCostUsd": sum(r["written"]["metadata"].get("costUsd") or 0 for r in records),
        "promptVersion": PROMPT_VERSION,
        "note": "Writer-only development replay; synthetic focused-question/refusal controls; no headset verification or gameplay edits."}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(replay(args.source, args.output))
