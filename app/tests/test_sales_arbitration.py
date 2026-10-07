"""Focused hybrid-routing and evidence regression checks, with no paid calls."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from app.config import get_settings
from app.sales_arbitration import LunaSalesArbitrator, select_arbitration_labels, merge_adjudication
from app.sales_openrouter import QUESTIONS, LUNA_MODEL
from app.sales_rubric import evaluate, normalized_labels


def labels(**overrides):
    result = {k: {"status": "false", "noul": 0.0} for k in QUESTIONS}
    for k, status in overrides.items():
        result[k] = {"status": status, "noul": {"true": 1.0, "false": 0.0, "uncertain": .5}[status]}
    return result


def state(**changes):
    return {"phase": 2, "pipelineMode": "openrouter", "maxTurns": 8, "dialogueTurnCount": 1,
            "investigationEvidence": [], "objectiveEvidence": {}, **changes}


class RoutingTests(unittest.TestCase):
    def test_question_about_a_known_fact_earns_nothing_without_arbitration(self):
        # The backend decides repetition from known facts; Jev's repeat label is not consulted.
        current = state(investigationEvidence=["walking_routine"])
        evidence = labels(walkingQuestion="true", repeatedQuestion="uncertain")
        self.assertEqual(select_arbitration_labels(current, evidence), [])
        candidate, decision = evaluate(current, evidence, "t")
        self.assertFalse(decision["assessmentUncertain"])
        self.assertEqual(decision["disclosedFactIds"], [])
        self.assertNotIn("use_question", candidate.get("rubricComponents", {}))

    def test_only_relevant_uncertainty_and_subtle_negative_judgments_are_routed(self):
        self.assertEqual(select_arbitration_labels(state(), labels(appearanceQuestion="uncertain")), [])
        selected = select_arbitration_labels(state(), labels(abuse="uncertain", refusesRemedy="uncertain", walkingQuestion="uncertain"))
        self.assertEqual(set(selected), {"abuse", "refusesRemedy", "walkingQuestion"})
        self.assertEqual(select_arbitration_labels(state(), labels(disrespect="true")), ["disrespect"])

    def test_completed_evidence_and_known_facts_do_not_repeat_arbitration(self):
        earlier = {"turnId": "earlier", "labels": labels(acknowledgment="true", openQuestion="true"),
                   "factsKnownBefore": [], "challengeShownBefore": False, "disclosedFactIds": ["pain_location"]}
        current = state(phase=1, investigationEvidence=["pain_location"], evidenceLedger=[earlier])
        self.assertEqual(select_arbitration_labels(current, labels(acknowledgment="uncertain", openQuestion="uncertain", painLocationQuestion="uncertain")), [])

    def test_contradictory_offer_refusal_is_routed_together_and_budget_is_bounded(self):
        self.assertEqual(set(select_arbitration_labels(state(), labels(exchangeOffer="true", refusesRemedy="true"))), {"exchangeOffer", "refusesRemedy"})
        self.assertLessEqual(len(select_arbitration_labels(state(), labels(**{k: "uncertain" for k in QUESTIONS}))), 8)

    def test_provenance_keeps_jev_noul_and_normalization_accepts_validated_luna_status(self):
        base = {"labels": labels(refusesRemedy="uncertain"), "metadata": {}}
        adjudication = {"labels": {"refusesRemedy": {"status": "true", "evidence": "em không đổi"}}, "metadata": {"requestedModel": LUNA_MODEL}}
        merged = merge_adjudication(base, adjudication)
        normalized = normalized_labels(merged)
        self.assertEqual(normalized["refusesRemedy"]["noul"], .5)
        self.assertEqual(normalized["refusesRemedy"]["status"], "true")
        self.assertEqual(normalized["refusesRemedy"]["source"], "luna")
        self.assertEqual(base["labels"]["refusesRemedy"]["status"], "uncertain")
        _, decision = evaluate(state(), normalized, "t")
        self.assertEqual(decision["turnQuality"], "bad")

    def test_unknown_luna_does_not_erase_certain_jev_evidence(self):
        base = {"labels": labels(refusesRemedy="true"), "metadata": {}}
        merged = merge_adjudication(base, {"labels": {"refusesRemedy": {"status": "uncertain", "evidence": "không"}}, "metadata": {}})
        self.assertEqual(normalized_labels(merged)["refusesRemedy"]["status"], "true")


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, body, requested=("abuse", "refusesRemedy"), model=LUNA_MODEL, finish="stop"):
        with patch.dict("os.environ", {}, clear=True):
            settings = replace(get_settings(), openrouter_api_key="test-secret")
        requests = []
        def handler(req):
            requests.append(json.loads(req.content))
            return httpx.Response(200, json={"model": model, "provider": "OpenAI", "choices": [{"finish_reason": finish, "message": {"content": json.dumps(body)}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await LunaSalesArbitrator(settings, client=client).adjudicate("Em không đổi giày cho chị", {"objective": 2, "realName": "PRIVATE", "runId": "PRIVATE-ID", "audio": "PRIVATE-AUDIO"}, requested)
        return result, requests[0]

    async def test_small_strict_schema_and_privacy_with_valid_grounded_decisions(self):
        result, payload = await self.call({"abuse": {"status": "false", "evidence": ""}, "refusesRemedy": {"status": "true", "evidence": "không đổi giày"}})
        self.assertEqual(result["labels"]["refusesRemedy"]["status"], "true")
        self.assertEqual(payload["model"], LUNA_MODEL)
        self.assertEqual(set(payload["response_format"]["json_schema"]["schema"]["required"]), {"abuse", "refusesRemedy"})
        self.assertEqual(payload["provider"]["data_collection"], "deny")
        self.assertTrue(payload["provider"]["require_parameters"])
        self.assertNotIn("PRIVATE", json.dumps(payload))
        self.assertNotIn("rating", json.dumps(payload["response_format"]))

    async def test_invalid_span_does_not_discard_other_valid_decision(self):
        result, _ = await self.call({"abuse": {"status": "false", "evidence": ""}, "refusesRemedy": {"status": "true", "evidence": "đi về đi"}})
        self.assertEqual(result["labels"]["abuse"]["status"], "false")
        self.assertNotIn("refusesRemedy", result["labels"])
        self.assertEqual(result["metadata"]["rejectedLabels"], ["refusesRemedy"])

    async def test_schema_smuggling_and_unknown_model_are_rejected(self):
        result, _ = await self.call({"abuse": {"status": "false", "evidence": ""}, "score": 100}, requested=("abuse",))
        self.assertEqual(result["labels"], {})
        self.assertEqual(result["metadata"]["errorCode"], "arbitrator_invalid_schema")

    async def test_wrong_model_and_truncated_output_never_override_jev(self):
        body = {"abuse": {"status": "false", "evidence": ""}}
        for changes, code in (({"model": "unrequested/model"}, "arbitrator_model_mismatch"),
                              ({"finish": "length"}, "arbitrator_incomplete_response")):
            result, _ = await self.call(body, requested=("abuse",), **changes)
            self.assertEqual(result["labels"], {})
            self.assertEqual(result["metadata"]["errorCode"], code)

    async def test_invalid_status_shape_is_rejected_per_label(self):
        result, _ = await self.call({"abuse": {"status": [], "evidence": ""},
            "refusesRemedy": {"status": "true", "evidence": "không đổi giày"}})
        self.assertEqual(result["metadata"]["rejectedLabels"], ["abuse"])
        self.assertEqual(result["labels"]["refusesRemedy"]["status"], "true")

    async def test_punctuation_only_quote_changes_use_original_transcript_span(self):
        result, _ = await self.call({"refusesRemedy": {"status": "true", "evidence": "không đổi giày, cho chị."}}, requested=("refusesRemedy",))
        self.assertEqual(result["labels"]["refusesRemedy"]["evidence"], "không đổi giày cho chị")
        self.assertEqual(result["metadata"]["rejectedLabels"], [])

    async def test_rejected_quote_is_repaired_once_without_reasking_accepted_label(self):
        settings = replace(get_settings(), openrouter_api_key="test-secret", sales_luna_timeout_seconds=1)
        requests = []
        def handler(req):
            requests.append(json.loads(req.content))
            decisions = ({"abuse": {"status": "false", "evidence": ""},
                          "refusesRemedy": {"status": "true", "evidence": "đi về đi test-secret"}}
                         if len(requests) == 1 else {"refusesRemedy": {"status": "true", "evidence": "không đổi giày"}})
            return httpx.Response(200, json={"model": LUNA_MODEL, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decisions)}}], "usage": {"cost": .001}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await LunaSalesArbitrator(settings, client=client).adjudicate("Em không đổi giày cho chị", {}, ("abuse", "refusesRemedy"))
        self.assertEqual(len(requests), 2)
        self.assertEqual(set(requests[1]["response_format"]["json_schema"]["schema"]["required"]), {"refusesRemedy"})
        self.assertEqual(set(result["labels"]), {"abuse", "refusesRemedy"})
        self.assertEqual(result["metadata"]["attempts"][0]["rejectionReasons"]["refusesRemedy"], "evidence_not_in_transcript")
        self.assertEqual(result["metadata"]["costUsd"], .002)
        self.assertNotIn("test-secret", json.dumps(result))

    async def test_rejected_decision_diagnostics_survive_failed_repair(self):
        result, _ = await self.call({"refusesRemedy": {"status": "true", "evidence": "đi về đi"}}, requested=("refusesRemedy",))
        self.assertEqual(len(result["metadata"]["attempts"]), 2)
        self.assertEqual(result["metadata"]["rejectionReasons"]["refusesRemedy"], "evidence_not_in_transcript")
        self.assertIn("đi về đi", result["metadata"]["rejectedDecisions"]["refusesRemedy"])

    async def test_quote_matching_never_removes_words_or_vietnamese_diacritics(self):
        for quote in ("không đổi cho chị", "khong doi giay"):
            result, _ = await self.call({"refusesRemedy": {"status": "true", "evidence": quote}}, requested=("refusesRemedy",))
            self.assertEqual(result["labels"], {})

    async def test_repair_timeout_keeps_previously_valid_label_and_shared_deadline(self):
        import asyncio
        import time
        settings = replace(get_settings(), openrouter_api_key="test-secret", sales_luna_timeout_seconds=.3)
        calls = 0
        async def handler(req):
            nonlocal calls
            calls += 1
            if calls == 2:
                await asyncio.sleep(2)
            decisions = {"abuse": {"status": "false", "evidence": ""}, "refusesRemedy": {"status": "true", "evidence": "đi về đi"}}
            return httpx.Response(200, json={"model": LUNA_MODEL, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decisions)}}]})
        start = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await LunaSalesArbitrator(settings, client=client).adjudicate("Em không đổi giày cho chị", {}, ("abuse", "refusesRemedy"))
        self.assertEqual(calls, 2)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(result["labels"]["abuse"]["status"], "false")
        self.assertEqual(result["metadata"]["errorCode"], "arbitrator_timeout")
