"""The public HTTP contract is the main boundary for Sales v3 regression tests."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from app import main
from app.config import get_settings
from app.sales_openrouter import RUBRIC_VERSION
from app.sales_returning_customer import ReturningSessionStore
from app.sales_rubric import ORDINARY, SAFETY
from app.tests.test_sales_returning_customer import FakeTranscriber, request


def classification(*true, **values):
    labels = {name: {"noul": 1.0 if name in true else 0.0, "status": "true" if name in true else "false"}
              for name in ORDINARY + SAFETY}
    for name, value in values.items():
        labels[name] = {"noul": value, "status": "true"}
    return {"labels": labels, "metadata": {"requestedModel": "fake-jev", "usage": {"cost": 0.001}}}


class Classifier:
    def __init__(self):
        self.next = classification()
        self.calls = 0
        self.fail = False
        self.delay = 0
    async def classify(self, transcript, context):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider contained secret text")
        return self.next


class Writer:
    def __init__(self):
        self.calls = 0
        self.fail = False
    async def write(self, plan, transcript, history):
        self.calls += 1
        if self.fail:
            raise RuntimeError("writer offline")
        return {"customerText": plan["fallbackText"], "metadata": {"usage": {"cost": 0.002}}, "fallbackUsed": False}


class SalesPipelineApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_exchange_after_hearing_complaint_can_end_at_four_without_full_trust(self):
        await self.turn("disrespect")
        offered = await self.turn("acknowledgment", "exchangeOffer", "exchangeConditions")
        self.assertFalse(offered["conversationComplete"])
        await self.turn("openQuestion", "painLocationQuestion")
        accepted = await self.turn("exchangeOffer", "exchangeConditions")
        self.assertTrue(accepted["conversationComplete"])
        self.assertEqual(accepted["endingReason"], "exchange_accepted")
        self.assertEqual(accepted["remainingTurns"], 4)
        self.assertIn("đổi", accepted["customerText"].lower())
        final = await self.complete()
        self.assertEqual(final["trustState"], "partially_restored")
        self.assertTrue(final["resolutionAccepted"])
        self.assertFalse(final["causeIdentification"])
        self.assertFalse(final["trustRebuilding"])
        self.assertEqual(final["score"], 20)
        after = await self.client.post("/api/sales/sessions/v2/turns", json=request("after-exchange").model_dump(by_alias=True))
        self.assertEqual(after.status_code, 409)

    async def test_early_exchange_requires_clear_policy_and_safe_current_reply(self):
        await self.turn("acknowledgment", "openQuestion", "painLocationQuestion")
        no_policy = await self.turn("exchangeOffer")
        self.assertFalse(no_policy["conversationComplete"])
        uncertain = await self.turn("exchangeOffer", values={"exchangeConditions": .5})
        self.assertFalse(uncertain["conversationComplete"])
        refused = await self.turn("exchangeOffer", "exchangeConditions", "refusesRemedy")
        self.assertFalse(refused["conversationComplete"])
        promised = await self.turn("exchangeOffer", "exchangeConditions", "absoluteGuarantee")
        self.assertFalse(promised["conversationComplete"])

    async def test_neutral_followup_does_not_repeat_exchange_acknowledgment_from_old_offer(self):
        await self.turn("acknowledgment", "openQuestion")
        first = await self.turn("exchangeOffer")
        second = await self.turn("exchangeOffer")
        self.assertNotEqual(first["customerText"].split('.')[0], second["customerText"].split('.')[0])
        self.assertNotIn("Đổi cho chị thì được", second["customerText"])

    async def test_invariant_review_item_does_not_block_forced_ending(self):
        await self.turn("acknowledgment")
        await self.turn("absoluteGuarantee", values={"beggingWithoutExplanation": .42})
        last = await self.turn("absoluteGuarantee", "maintainsUnauthorizedPromise")
        original = await self.store.get("v2")
        self.assertTrue(last["conversationComplete"])
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "completed")
        self.assertEqual((final["score"], final["trustState"]), (0, "lost"))
        state = await self.store.get("v2")
        self.assertEqual(state["assessmentReviewItems"], [])
        self.assertEqual(state["assessmentReviewResolution"]["reason"], "outcome_invariant")
        self.assertEqual(state["turns"], original["turns"])
        self.assertEqual(state["completedTurns"], original["completedTurns"])
        self.assertEqual(state["assessmentResolvedReviewItems"][0]["labels"], ["beggingWithoutExplanation"])

    async def test_sensitive_review_rearbitrates_original_turn_once_and_is_idempotent(self):
        self.stt.text = "Em xin lỗi chị vì đã tư vấn sai"
        await self.turn("acknowledgment", values={"abuse": .5})
        await self.turn()
        await self.enable_luna()
        calls = []
        async def arbitrate(transcript, context, selected):
            calls.append((transcript, context, selected))
            return {"labels": {"abuse": {"status": "false", "evidence": ""}}, "metadata": {}}
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", side_effect=arbitrate):
            final = await self.complete()
            repeated = await self.complete("retry-final")
        self.assertEqual(final["assessmentStatus"], "completed")
        self.assertEqual(final["score"], 10)
        self.assertEqual(repeated["completionId"], "final")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "Em xin lỗi chị vì đã tư vấn sai")
        self.assertEqual(calls[0][1]["objective"], 1)
        self.assertEqual(calls[0][2], ["abuse"])
        state = await self.store.get("v2")
        self.assertEqual(state["turns"][0]["turnAssessment"]["abuse"]["status"], "uncertain")
        self.assertNotIn("assessmentReviewDecisions", final)

    async def test_sensitive_review_failure_remains_review_without_repeated_paid_calls(self):
        await self.turn("acknowledgment", values={"abuse": .5})
        await self.enable_luna()
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", return_value={"labels": {}, "metadata": {"errorCode": "arbitrator_timeout"}}) as call:
            final = await self.complete()
            await self.complete("retry-final")
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertIsNone(final.get("score"))
        self.assertEqual(call.call_count, 1)

    async def test_review_does_not_hide_possible_violation_when_score_already_zero(self):
        await self.turn("abuse", values={"unauthorizedDiscount": .5})
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertIsNone(final.get("score"))
        state = await self.store.get("v2")
        self.assertEqual(state["assessmentReviewResolution"]["reason"], "outcome_sensitive")

    async def test_review_cannot_reclassify_turn_under_a_different_historical_objective(self):
        await self.turn("acknowledgment", values={"openQuestion": .5})
        await self.turn("openQuestion")
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "needs-review")
        state = await self.store.get("v2")
        self.assertEqual(state["assessmentReviewResolution"]["reason"], "context_changed")

    async def test_deleting_diagnostics_during_completion_review_never_resurrects_quotes(self):
        await self.turn("acknowledgment", values={"abuse": .5})
        await self.enable_luna()
        async def deleting(*args):
            await self.store.delete_diagnostics("v2")
            return {"labels": {"abuse": {"status": "false", "evidence": ""}}, "metadata": {"rejectedDecisions": {"abuse": "private quote"}}}
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", side_effect=deleting):
            response = await self.client.post("/api/sales/sessions/v2/complete", json={"completionId": "deleted"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "diagnostics_deleted")
        state = await self.store.get("v2")
        self.assertEqual(state["assessmentReviewMetadata"], {})
        self.assertNotIn("private quote", json.dumps(state))

    async def test_transcription_capture_failure_allows_new_recording_without_losing_session(self):
        with patch.object(self.stt, "transcribe", side_effect=RuntimeError("bad capture")):
            failed = await self.turn()
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["retryRecording"])
        state = await self.store.get("v2")
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["dialogueTurnCount"], 0)
        self.assertEqual(state["turnCheckpoints"], {})
        accepted = await self.turn("acknowledgment", "openQuestion")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["activeObjective"], 2)

    async def test_customer_fallbacks_avoid_administrative_acknowledgment(self):
        for acts in (("acknowledgment",), ("exchangeOffer",), ("acknowledgment", "openQuestion")):
            turn = await self.turn(*acts)
            self.assertNotIn("Chị ghi nhận", turn["customerText"])
            self.assertNotIn("Chị xác nhận", turn["customerText"])

    async def test_relevant_question_softens_customer_but_refusal_gets_pushback(self):
        plans = []
        original = self.writer.write
        async def capture(plan, transcript, history):
            plans.append(plan)
            return await original(plan, transcript, history)
        with patch.object(self.writer, "write", side_effect=capture):
            await self.turn("acknowledgment", "openQuestion")
            answer = await self.turn("walkingQuestion")
            refusal = await self.turn("refusesRemedy")
        self.assertEqual((plans[0]["intent"], plans[0]["concern"], plans[0]["requiredContent"]), ("raise_concern", 2, []))
        self.assertEqual(plans[1]["customerDisposition"], "receptive")
        self.assertIn("bến xe", answer["customerText"])
        self.assertEqual(plans[2]["customerDisposition"], "irritated")
        self.assertEqual(refusal["playerResponseRating"], "bad")

    async def test_classifier_response_without_usable_evidence_keeps_null_rating(self):
        self.classifier.next = {"labels": {name: {"noul": None} for name in ORDINARY + SAFETY}}
        turn = (await self.client.post("/api/sales/sessions/v2/turns", json=request().model_dump(by_alias=True))).json()
        self.assertEqual(turn["turnQuality"], "uncertain")
        self.assertIsNone(turn["playerResponseRating"])

    async def test_luna_service_error_leaves_rating_null_and_records_error(self):
        await self.enable_luna()
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", return_value={
                "labels": {}, "metadata": {"errorCode": "arbitrator_invalid_schema", "requestedModel": "openai/gpt-6-luna",
                                           "selectedLabels": ["refusesRemedy"]}}):
            turn = await self.turn(values={"refusesRemedy": .5})
        self.assertEqual(turn["status"], "accepted")
        self.assertEqual(turn["turnQuality"], "uncertain")
        self.assertIsNone(turn["playerResponseRating"])
        self.assertEqual(turn["assessmentError"]["code"], "arbitrator_invalid_schema")
        self.assertEqual(turn["assessmentError"]["stage"], "arbitration")
        self.assertEqual(turn["assessmentError"]["labels"], ["refusesRemedy"])

    async def test_luna_timeout_fails_retryably_and_retry_resumes_at_arbitration(self):
        await self.enable_luna()
        calls = []
        async def adjudicate(*args):
            calls.append(args)
            if len(calls) == 1:
                return {"labels": {}, "metadata": {"errorCode": "arbitrator_timeout", "requestedModel": "openai/gpt-6-luna"}}
            return {"labels": {"refusesRemedy": {"status": "true", "evidence": "đổi"}}, "metadata": {}}
        self.stt.text = "Em không đổi giày cho chị"
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", side_effect=adjudicate):
            failed = await self.turn(values={"refusesRemedy": .5})
            self.assertEqual((failed["status"], failed["error"]["code"], failed["error"]["stage"]),
                             ("failed", "arbitrator_timeout", "arbitration"))
            self.assertTrue(failed["retryTurn"])
            self.assertFalse(failed["retryRecording"])
            self.assertEqual(failed["error"]["details"]["requestedModel"], "openai/gpt-6-luna")
            state = await self.store.get("v2")
            self.assertEqual((state["dialogueTurnCount"], self.writer.calls), (0, 0))
            retried = (await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))).json()
        self.assertEqual(retried["status"], "accepted")
        self.assertEqual(retried["playerResponseRating"], "bad")
        self.assertEqual((len(calls), self.classifier.calls, self.writer.calls), (2, 1, 1))

    async def enable_luna(self):
        state = await self.store.get("v2")
        state.update(lunaArbitrationEnabled=True, lunaDeadlineSeconds=.02)
        await self.store.save(state)

    async def test_luna_resolves_refusal_and_cached_http_retry_never_calls_models_twice(self):
        await self.enable_luna()
        self.stt.text = "Em không đổi giày cho chị"
        calls = []
        async def adjudicate(transcript, context, selected):
            calls.append(selected)
            return {"labels": {"refusesRemedy": {"status": "true", "evidence": "không đổi giày"}}, "metadata": {}}
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", side_effect=adjudicate):
            turn = await self.turn(values={"refusesRemedy": .5})
            replay = (await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))).json()
        self.assertEqual(replay, turn)
        self.assertEqual(turn["playerResponseRating"], "bad")
        self.assertEqual(turn["turnAssessment"]["refusesRemedy"]["noul"], .5)
        self.assertEqual(turn["turnAssessment"]["refusesRemedy"]["source"], "luna")
        self.assertEqual((len(calls), self.classifier.calls, self.writer.calls), (1, 1, 1))
        self.assertEqual((await self.store.get("v2"))["badResponseCount"], 1)

    async def test_stuck_luna_watchdog_is_a_retryable_timeout_not_a_spent_turn(self):
        await self.enable_luna()
        async def stuck(*args):
            await asyncio.sleep(1)
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", side_effect=stuck):
            turn = await self.turn("abuse", values={"refusesRemedy": .5})
        self.assertEqual((turn["status"], turn["error"]["code"]), ("failed", "arbitrator_timeout"))
        self.assertTrue(turn["retryTurn"])
        state = await self.store.get("v2")
        self.assertEqual(state["turnCheckpoints"]["turn-1"]["timeoutRetries"][0]["stage"], "arbitration")
        self.assertEqual((state["dialogueTurnCount"], state["badResponseCount"]), (0, 0))

    async def test_luna_checkpoint_survives_failure_after_arbitration(self):
        await self.enable_luna()
        self.stt.text = "Em không đổi giày cho chị"
        calls = []
        async def adjudicate(*args):
            calls.append(args)
            return {"labels": {"refusesRemedy": {"status": "true", "evidence": "không đổi giày"}}, "metadata": {}}
        with patch("app.sales_arbitration.LunaSalesArbitrator.adjudicate", side_effect=adjudicate):
            with patch("app.sales_pipeline.plan_reply", side_effect=RuntimeError("stage interrupted")):
                failed = await self.turn(values={"refusesRemedy": .5})
            self.assertEqual(failed["status"], "failed")
            retried = (await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))).json()
        self.assertEqual(retried["playerResponseRating"], "bad")
        self.assertEqual((len(calls), self.classifier.calls), (1, 1))
        state = await self.store.get("v2")
        self.assertEqual((state["dialogueTurnCount"], state["badResponseCount"]), (1, 1))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReturningSessionStore(Path(self.temp.name))
        self.classifier, self.writer = Classifier(), Writer()
        self.stt = FakeTranscriber("Em xin lỗi chị, chị có thể nói rõ vấn đề không?")
        values = dict(vars(get_settings()))
        values.update(sales_pipeline_mode="openrouter", sales_max_turns=8, sales_luna_arbitration_enabled=False,
                      sales_jev_timeout_seconds=0.02, sales_writer_timeout_seconds=0.02)
        self.settings = SimpleNamespace(**values)
        self.patches = [patch.object(main, "sales_returning_store", self.store),
            patch.object(main, "sales_returning_classifier", self.classifier),
            patch.object(main, "sales_returning_writer", self.writer),
            patch.object(main, "sales_returning_transcriber", self.stt),
            patch("app.sales_returning_customer.get_settings", return_value=self.settings),
            patch.dict(os.environ, {"BACKEND_API_TOKEN": "test", "SALES_DIAGNOSTIC_TOKEN": "diagnostic"})]
        for item in self.patches:
            item.start()
        self.client = AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test",
                                  headers={"Authorization": "Bearer test"})
        self.index = 0
        created = await self.client.post("/api/sales/sessions", json={"sessionId": "v2", "runId": "run-v2"})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["maxTurns"], 8)
        self.assertEqual(created.json()["remainingTurns"], 8)
    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()
    async def turn(self, *labels, payload=None, values=None):
        self.index += 1
        self.classifier.next = classification(*labels, **(values or {}))
        response = await self.client.post("/api/sales/sessions/v2/turns",
            json=payload or request(f"turn-{self.index}").model_dump(by_alias=True))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()
    def capture_plans(self):
        plans = []
        original = self.writer.write
        async def capture(plan, transcript, history):
            plans.append(plan)
            return await original(plan, transcript, history)
        self.writer.write = capture
        return plans
    async def complete(self, completion_id="final"):
        response = await self.client.post("/api/sales/sessions/v2/complete", json={"completionId": completion_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()
    async def successful_prefix(self, expect_challenge=True):
        turn = await self.turn("acknowledgment", "openQuestion")
        self.assertEqual(turn["activeObjective"], 2)
        await self.turn("walkingQuestion")
        await self.turn("fitQuestion")
        turn = await self.turn("causeStatement")
        self.assertEqual(turn["activeObjective"], 3)
        turn = await self.turn("exchangeOffer", "exchangeConditions", "lightweightForWalking", "fitOrWalkTrial", "routineMatchExplanation")
        self.assertEqual(turn["activeObjective"], 4)
        self.assertEqual(turn["conversationComplete"], not expect_challenge)
        return turn

    async def test_exchange_without_policy_raises_the_missing_investigation_part(self):
        plans = self.capture_plans()
        await self.turn("acknowledgment", "openQuestion")
        await self.turn("fitQuestion", "painLocationQuestion")
        self.stt.text = "Dạ em sẽ đổi hàng cho chị."
        turn = await self.turn("exchangeOffer")
        self.assertEqual((plans[-1]["intent"], plans[-1]["concern"], plans[-1]["missingPart"]), ("raise_concern", 2, "walking"))
        self.assertNotIn("chưa rõ", turn["customerText"])
        self.assertEqual(turn["activeObjective"], 2)
        self.assertEqual(turn["disclosedFactIds"], [])
        self.assertEqual(turn["playerResponseRating"], "neutral")
        await self.turn("walkingQuestion")
        turn = await self.turn("exchangeOffer")
        self.assertEqual((plans[-1]["concern"], plans[-1]["missingPart"]), (2, "cause"))
        self.assertNotIn("chưa rõ", turn["customerText"])

    async def test_early_remedy_is_credited_while_lan_raises_the_unheard_concern(self):
        plans = self.capture_plans()
        for labels, values in ((["acknowledgment", "exchangeOffer"], {}),
                               ([], {"exchangeOffer": .55}),
                               (["exchangeOffer"], {}),
                               (["exchangeOffer", "exchangeConditions"], {})):
            turn = await self.turn(*labels, values=values)
            self.assertEqual((plans[-1]["concern"], plans[-1]["missingPart"]), (1, "problem_question"))
            self.assertNotEqual(turn["customerText"], "Chị vẫn đang chờ em giải quyết chuyện đôi giày này.")
            self.assertEqual(turn["activeObjective"], 1)
            self.assertEqual(turn["disclosedFactIds"], [])
            self.assertFalse(turn["conversationComplete"])
        self.assertEqual([plan["hintLevel"] for plan in plans], [0, 1, 2, 2])
        state = await self.store.get("v2")
        self.assertEqual(state["completedObjectives"], [])
        # An early policy-compliant offer counts even before Lan feels heard.
        self.assertEqual(set(state["rubricComponents"]), {"acknowledgment", "policy"})

    async def test_concern_reply_in_natural_wording_is_not_rejected_for_missing_keywords(self):
        async def generic(plan, transcript, history):
            return {"customerText": "Chị vẫn đang chờ em giải quyết chuyện đôi giày này."}
        with patch.object(self.writer, "write", side_effect=generic):
            turn = await self.turn("acknowledgment", "exchangeOffer")
        self.assertFalse(turn["fallbackUsed"])
        self.assertEqual(turn["customerText"], "Chị vẫn đang chờ em giải quyết chuyện đôi giày này.")

    async def test_first_open_question_is_answered_when_it_completes_emotional_objective(self):
        turn = await self.turn("acknowledgment", "openQuestion", "walkingQuestion")
        self.assertEqual(turn["activeObjective"], 2)
        self.assertEqual(turn["disclosedFactIds"], ["walking_routine"])
        self.assertIn("bến xe", turn["customerText"])

    async def test_phase_one_writer_cannot_repeat_previous_followup(self):
        await self.turn("acknowledgment", "exchangeOffer")
        previous = (await self.turn("exchangeOffer"))["customerText"]
        async def repeated(plan, transcript, history):
            return {"customerText": previous}
        with patch.object(self.writer, "write", side_effect=repeated):
            turn = await self.turn("exchangeOffer", "exchangeConditions")
        self.assertTrue(turn["fallbackUsed"])
        self.assertNotEqual(turn["customerText"], previous)
        self.assertIn("repeated_reply", turn["writerMetadata"]["rejectionCodes"])

    async def test_failed_writer_followups_do_not_repeat_the_previous_reply(self):
        self.writer.fail = True
        spoken = []
        for labels in (("acknowledgment", "exchangeOffer"), ("exchangeOffer",), ("exchangeOffer",)):
            turn = await self.turn(*labels)
            self.assertTrue(turn["fallbackUsed"])
            self.assertNotIn(turn["customerText"], spoken[-3:])
            spoken.append(turn["customerText"])

    async def test_known_abuse_is_warned_and_recorded_even_if_manager_is_uncertain(self):
        turn = await self.turn("abuse", values={"managerEscalation": .19})
        self.assertIn("thiếu tôn trọng", turn["customerText"])
        self.assertNotIn("quản lý", turn["customerText"])
        self.assertEqual(turn["policyViolations"], ["abusive_language"])
        self.assertFalse(turn["conversationComplete"])
        self.assertEqual(turn["remainingTurns"], 7)
        self.assertEqual((await self.complete())["assessmentStatus"], "needs-review")

    async def test_bad_quality_survives_unrelated_critical_uncertainty(self):
        turn = await self.turn(values={"disrespect": .82, "abuse": .5})
        self.assertEqual(turn["turnQuality"], "bad")
        self.assertEqual(turn["playerResponseRating"], "bad")
        self.assertTrue(turn["assessmentUncertain"])
        self.assertEqual(turn["policyViolations"], [])
        self.assertEqual(turn["remainingTurns"], 7)
        self.assertEqual(turn["acceptedTurnCount"], 0)
        self.assertEqual((await self.complete())["assessmentStatus"], "needs-review")

    async def test_refusing_remedy_is_bad_and_does_not_acknowledge_an_old_exchange(self):
        await self.turn("acknowledgment", "exchangeOffer")
        await self.turn("openQuestion", "fitQuestion")
        self.stt.text = "Em không đổi hàng cho chị nữa, chị đi về đi."
        turn = await self.turn("refusesRemedy")
        self.assertEqual(turn["playerResponseRating"], "bad")
        self.assertIn("từ chối", turn["customerText"])
        self.assertNotIn("ghi nhận phương án đổi", turn["customerText"])
        self.assertEqual(turn["policyViolations"], [])
        self.assertFalse(turn["conversationComplete"])

    async def test_explicit_neutral_rating_does_not_block_completion(self):
        turn = await self.turn()
        self.assertEqual(turn["playerResponseRating"], "neutral")
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "completed")
        self.assertEqual(final["endingReason"], "stopped_early")

    async def test_review_continues_but_all_classified_turns_obey_dialogue_limit(self):
        for index in range(3):
            turn = await self.turn(values={"abuse": .5})
            self.assertFalse(turn["conversationComplete"])
            self.assertEqual(turn["remainingTurns"], 7 - index)
        self.assertEqual(turn["assessmentStatus"], "needs-review")
        clear = await self.turn("acknowledgment", "openQuestion")
        self.assertEqual(clear["activeObjective"], 2)
        self.assertFalse(clear["conversationComplete"])
        resumed = (await self.client.get("/api/sales/sessions/v2")).json()
        self.assertEqual(resumed["status"], "active")
        self.assertFalse(resumed["conversationComplete"])
        # Each turn discloses a new fact, so the limit is reached before a stalemate.
        for label in ("painLocationQuestion", "painTimingQuestion", "walkingQuestion", "appearanceQuestion"):
            turn = await self.turn(label)
        self.assertEqual(turn["dialogueTurnCount"], 8)
        self.assertEqual(turn["evaluableTurnCount"], 5)
        self.assertEqual(turn["remainingTurns"], 0)
        self.assertTrue(turn["conversationComplete"])
        self.assertEqual(turn["endingReason"], "turn_limit")
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertEqual(final["status"], "awaitingCompletion")
        self.assertIsNone(final.get("score"))
        response = await self.client.post("/api/sales/sessions/v2/turns", json=request("ninth").model_dump(by_alias=True))
        self.assertEqual(response.status_code, 409)
        cached = await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-3").model_dump(by_alias=True))
        self.assertEqual(cached.status_code, 200)
        self.assertEqual((await self.store.get("v2"))["dialogueTurnCount"], 8)

    async def test_unresolved_turn_evidence_survives_clear_turn_and_early_stop(self):
        await self.turn(values={"abuse": .5})
        await self.turn("acknowledgment", "openQuestion")
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertEqual(final["endingReason"], "stopped_early")
        self.assertTrue(final["conversationComplete"])
        self.assertIsNone(final.get("score"))
        state = await self.store.get("v2")
        self.assertEqual(state["assessmentReviewItems"], [{"turnId": "turn-1", "objective": 1, "labels": ["abuse"]}])
        self.assertNotIn("assessmentReviewItems", final)

    async def test_all_uncertain_dialogue_ends_at_limit_without_claiming_trust(self):
        for _ in range(7):
            await self.turn(values={"abuse": .5})
        async def fabricated_trust(plan, transcript, history):
            return {"customerText": "Giờ chị thấy yên tâm hơn. Chị hài lòng, cảm ơn em, chị chào em nhé."}
        with patch.object(self.writer, "write", side_effect=fabricated_trust):
            turn = await self.turn(values={"abuse": .5})
        self.assertTrue(turn["fallbackUsed"])
        self.assertEqual(turn["remainingTurns"], 0)
        self.assertEqual(turn["evaluableTurnCount"], 0)
        self.assertTrue(turn["conversationComplete"])
        self.assertNotIn("hài lòng", turn["customerText"])
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertTrue(final["conversationComplete"])
        self.assertIsNone(final.get("score"))

    async def test_reviewed_dialogue_can_finish_objectives_before_limit(self):
        await self.turn(values={"abuse": .5})
        await self.successful_prefix()
        turn = await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
        self.assertTrue(turn["conversationComplete"])
        self.assertEqual(turn["endingReason"], "objectives_completed")
        self.assertEqual(turn["remainingTurns"], 1)
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertIsNone(final.get("trustState"))

    async def test_uncertain_refusal_clarifies_withdrawal_without_acknowledging_old_offer(self):
        await self.turn("acknowledgment", "exchangeOffer")
        await self.turn("openQuestion", "fitQuestion")
        turn = await self.turn(values={"refusesRemedy": .68})
        self.assertEqual(turn["turnQuality"], "uncertain")
        self.assertEqual(turn["playerResponseRating"], "uncertain")
        self.assertIn("từ chối", turn["customerText"])
        self.assertNotIn("ghi nhận", turn["customerText"])
        self.assertIn("refusesRemedy", turn["clarificationReason"])

    async def test_pressure_to_continue_without_explanation_is_bad_and_challenged(self):
        turn = await self.turn("beggingWithoutExplanation")
        self.assertEqual(turn["playerResponseRating"], "bad")
        self.assertIn("tiếp tục", turn["customerText"])
        self.assertIn("?", turn["customerText"])
        self.assertNotIn("ghi nhận", turn["customerText"])

    async def test_writer_cannot_assert_an_uncertain_refusal(self):
        async def assert_refusal(plan, transcript, history):
            return {"customerText": "Chị không chấp nhận việc em từ chối. Em sẽ xử lý khiếu nại thế nào?"}
        with patch.object(self.writer, "write", side_effect=assert_refusal):
            turn = await self.turn(values={"refusesRemedy": .68})
        self.assertTrue(turn["fallbackUsed"])
        self.assertIn("hay", turn["customerText"])

    async def test_real_writer_timeout_preserves_adapter_diagnostics(self):
        import httpx
        from dataclasses import replace
        from app.sales_openrouter import QwenSalesWriter
        async def handler(request):
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = QwenSalesWriter(replace(get_settings(), openrouter_api_key="offline-test-key",
                                             sales_writer_timeout_seconds=.02), client=client)
            with patch.object(main, "sales_returning_writer", adapter):
                turn = await self.turn("acknowledgment", "openQuestion")
                calls = self.classifier.calls
            retried = (await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))).json()
        self.assertEqual((turn["status"], turn["error"]["code"], turn["error"]["stage"]), ("failed", "writer_timeout", "writer"))
        self.assertTrue(turn["retryTurn"])
        self.assertEqual(turn["error"]["details"]["errorCode"], "openrouter_timeout")
        self.assertIn("durationSeconds", turn["error"]["details"])
        self.assertEqual(retried["status"], "accepted")
        self.assertFalse(retried["fallbackUsed"])
        self.assertEqual(self.classifier.calls, calls)

    async def test_early_success_keeps_unused_turns_and_rejects_further_turns(self):
        await self.successful_prefix()
        turn = await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
        self.assertEqual(turn["remainingTurns"], 2)
        self.assertTrue(turn["conversationComplete"])
        final = await self.complete()
        self.assertEqual(final["evaluableTurnCount"], 6)
        self.assertEqual(final["endingReason"], "objectives_completed")
        response = await self.client.post("/api/sales/sessions/v2/turns", json=request("after-end").model_dump(by_alias=True))
        self.assertEqual(response.status_code, 409)

    async def test_explicit_manager_transfer_ends_before_limit_even_with_other_uncertainty(self):
        turn = await self.turn("managerEscalation", values={"abuse": .5})
        self.assertTrue(turn["conversationComplete"])
        self.assertEqual(turn["endingReason"], "manager_escalation")
        self.assertEqual(turn["remainingTurns"], 7)
        # The dialogue ends; unresolved evidence still prevents a final score.
        final = await self.complete()
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertIsNone(final.get("score"))

    async def test_clear_manager_transfer_finalizes_after_one_turn(self):
        turn = await self.turn("managerEscalation")
        self.assertTrue(turn["conversationComplete"])
        self.assertEqual(turn["remainingTurns"], 7)
        final = await self.complete()
        self.assertEqual(final["evaluableTurnCount"], 1)
        self.assertEqual(final["endingReason"], "manager_escalation")
        self.assertEqual(final["trustState"], "lost")
        self.assertEqual(final["assessmentStatus"], "completed")

    async def test_writer_cannot_end_the_conversation_outside_the_plan(self):
        await self.turn("acknowledgment", "openQuestion")
        async def ending(plan, transcript, history):
            return {"customerText": "Chị đồng ý đổi, chị về nhé."}
        with patch.object(self.writer, "write", side_effect=ending):
            turn = await self.turn("exchangeOffer", "exchangeConditions")
        self.assertFalse(turn["conversationComplete"])
        self.assertTrue(turn["fallbackUsed"])
        self.assertEqual(turn["writerMetadata"]["errorCode"], "writer_guard_rejected")
        self.assertIn("unplanned_ending", turn["writerMetadata"]["rejectionCodes"])

    async def test_investigation_and_post_challenge_restore_deterministic_trust(self):
        challenge = await self.successful_prefix()
        self.assertIn("lần trước", challenge["customerText"])
        turn = await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
        self.assertTrue(turn["conversationComplete"])
        final = await self.complete()
        self.assertEqual((final["rawScore"], final["score"], final["trustState"]), (100, 100, "restored"))
        self.assertEqual(final["criterionScores"], {"apologyAndPolicyRemedy": 50, "adaptabilityAndDeescalation": 50})
        self.assertEqual(final["assessmentStatus"], "completed")
        self.assertEqual(final["endingReason"], "objectives_completed")
        retry = await self.complete("another-id")
        self.assertEqual(final, retry)
    async def test_last_turn_ends_without_exceeding_backend_limit(self):
        state = await self.store.get("v2")
        state["maxTurns"] = 5
        await self.store.save(state)
        challenge = await self.successful_prefix(expect_challenge=False)
        self.assertFalse(challenge["supplementalTurnGranted"])
        self.assertEqual(challenge["remainingTurns"], 0)
        self.assertTrue(challenge["conversationComplete"])
        self.assertEqual(challenge["endingReason"], "turn_limit")
        final = await self.complete()
        self.assertEqual(final["evaluableTurnCount"], 5)
        self.assertEqual(final["trustState"], "partially_restored")
        self.assertFalse(final["trustRebuilding"])
        self.assertFalse((await self.store.get("v2")).get("challengeShown", False))

    async def test_challenge_on_seventh_turn_can_finish_within_eight_turn_limit(self):
        await self.turn()
        await self.turn()
        challenge = await self.successful_prefix()
        self.assertEqual(challenge["remainingTurns"], 1)
        turn = await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
        self.assertTrue(turn["conversationComplete"])
        self.assertEqual(turn["remainingTurns"], 0)
        final = await self.complete()
        self.assertEqual(final["evaluableTurnCount"], 8)
        self.assertEqual(final["endingReason"], "objectives_completed")
        self.assertEqual(final["trustState"], "restored")
    async def test_apology_accumulates_neutral_is_not_bad_repetition_gives_no_points(self):
        first = await self.turn("acknowledgment")
        self.assertEqual(first["activeObjective"], 1)
        neutral = await self.turn()
        self.assertEqual(neutral["turnQuality"], "neutral")
        self.assertEqual(neutral["playerResponseRating"], "neutral")
        await self.turn("acknowledgment")
        final = await self.complete()
        self.assertEqual(final["rawScore"], 10)
        self.assertEqual(final["badResponseCount"], 0)
        self.assertEqual(final["endingReason"], "stopped_early")
        self.assertEqual(final["trustState"], "lost")
    async def test_cause_before_customer_facts_does_not_advance_or_earn_points(self):
        await self.turn("acknowledgment", "openQuestion")
        turn = await self.turn("causeStatement", "walkingQuestion", "fitQuestion")
        self.assertEqual(turn["activeObjective"], 2)
        final = await self.complete()
        self.assertFalse(final["causeIdentification"])
        self.assertEqual(final["criterionScores"]["adaptabilityAndDeescalation"], 20)
        self.assertNotIn("cause", final["rubricComponents"])
    async def test_uncertain_or_invalid_label_is_not_penalty_and_needs_review_after_two_clarifications(self):
        for index, value in enumerate((0.5, 2, None)):
            turn = await self.turn(values={"unauthorizedRefund": value})
            self.assertEqual(turn["turnQuality"], "uncertain")
            self.assertEqual(turn["remainingTurns"], 7 - index)
            self.assertEqual(turn["policyViolations"], [])
        self.assertEqual(turn["assessmentStatus"], "needs-review")
        self.assertFalse(turn["conversationComplete"])
        await self.turn("acknowledgment", "openQuestion")
        final = await self.complete("after-clear-turn")
        self.assertEqual(final["assessmentStatus"], "needs-review")
        self.assertIsNone(final.get("score"))
    async def test_missing_schema_labels_cannot_become_false_or_keyword_violations(self):
        self.classifier.next = {"labels": {"acknowledgment": {"noul": 1}}}
        self.stt.text = "Chị nói hoàn tiền nhưng em không hứa hoàn tiền."
        response = await self.client.post("/api/sales/sessions/v2/turns", json=request().model_dump(by_alias=True))
        self.assertEqual(response.json()["turnQuality"], "uncertain")
        self.assertEqual(response.json()["policyViolations"], [])
    async def test_technical_failure_retains_transcript_checkpoint_for_restart_and_same_hash_retry(self):
        self.classifier.fail = True
        failed = await self.turn("acknowledgment", "openQuestion")
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["playerResponseRating"])
        self.assertNotIn("secret", json.dumps(failed))
        self.assertEqual((await self.complete())["assessmentStatus"], "pending")
        new_store = ReturningSessionStore(Path(self.temp.name))
        main.sales_returning_store = new_store
        self.classifier.fail = False
        self.classifier.next = classification("acknowledgment", "openQuestion")
        with patch.object(self.stt, "transcribe", side_effect=AssertionError("STT must be cached")):
            response = await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))
        self.assertEqual(response.json()["activeObjective"], 2)
        calls = self.classifier.calls, self.writer.calls
        repeated = await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))
        self.assertEqual(response.json(), repeated.json())
        self.assertEqual(calls, (self.classifier.calls, self.writer.calls))
        changed = await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1", 2000).model_dump(by_alias=True))
        self.assertEqual(changed.status_code, 422)
    async def test_classifier_deadline_does_not_spend_turn_or_finalize(self):
        self.classifier.delay = 0.1
        turn = await self.turn("acknowledgment")
        self.assertEqual(turn["error"]["code"], "classifier_timeout")
        self.assertTrue(turn["retryTurn"])
        self.assertEqual(turn["acceptedTurnCount"], 0)
        self.assertIsNone((await self.complete()).get("score"))
    async def test_writer_failure_uses_backend_plan_without_changing_assessment(self):
        self.writer.fail = True
        turn = await self.turn("acknowledgment", "openQuestion")
        self.assertEqual(turn["activeObjective"], 2)
        self.assertTrue(turn["fallbackUsed"])
        self.assertTrue(turn["customerText"])
        self.assertEqual((await self.complete())["rawScore"], 10)
    async def test_independent_violations_dedupe_abuse_and_retraction_keeps_event(self):
        turn = await self.turn("acknowledgment", "unauthorizedRefund", "unauthorizedDiscount", "abuse")
        self.assertEqual(set(turn["policyViolations"]), {"unauthorized_refund", "unauthorized_discount", "abusive_language"})
        await self.turn("retractsUnauthorizedRefund", "retractsUnauthorizedDiscount", "acknowledgment", "openQuestion")
        final = await self.complete()
        self.assertEqual(len(final["policyViolations"]), 3)
        self.assertEqual(final["unresolvedPromises"], [])
    async def test_explicit_maintained_promise_ends_lost_after_challenge(self):
        await self.turn("unauthorizedRefund")
        turn = await self.turn("maintainsUnauthorizedPromise")
        self.assertTrue(turn["conversationComplete"])
        final = await self.complete()
        self.assertEqual(final["endingReason"], "maintained_unauthorized_promise")
        self.assertEqual(final["trustState"], "lost")
        self.assertEqual(len(final["policyViolations"]), 1)
    async def test_negation_quote_self_correction_and_manager_question_follow_independent_labels(self):
        texts = ("Em không hứa hoàn tiền.", "Chị vừa nói 'hoàn tiền' đúng không?",
                 "Em hoàn tiền, à em sửa lại, mình đổi đúng chính sách.", "Chị đã hỏi quản lý trước chưa?")
        for index, text in enumerate(texts):
            if index == 2:
                # Progress on concern 1 keeps the dialogue short of a stalemate.
                await self.turn("acknowledgment", "openQuestion")
            self.stt.text = text
            turn = await self.turn()
            self.assertEqual(turn["turnQuality"], "neutral")
            self.assertFalse(turn["conversationComplete"])
            self.assertEqual(turn["policyViolations"], [])

    async def test_valid_capture_silence_is_distinct_from_zero_pcm_and_stt_failure(self):
        silent = request("zero", 0).model_dump(by_alias=True)
        turn = await self.turn(payload=silent)
        self.assertEqual(turn["error"]["code"], "microphone_no_signal")
        for index in range(2):
            body = request(f"silence-{index}").model_dump(by_alias=True)
            body["capture"] = {"deviceReady": True, "permissionGranted": True, "speechDetected": False}
            turn = await self.turn(payload=body)
            self.assertEqual(turn["status"], "silent")
        # The zero-device checkpoint must be retried or abandoned; it cannot
        # manufacture a terminal result. Remove it to model explicit recovery.
        state = await self.store.get("v2")
        state["turnCheckpoints"] = {}
        await self.store.save(state)
        final = await self.complete()
        self.assertEqual(final["endingReason"], "second_silence")
        self.assertEqual(final["trustState"], "lost")
    async def test_diagnostic_deletion_scrubs_all_checkpoints_and_preserves_only_references(self):
        self.stt.text = "Một transcript riêng tư duy nhất"
        await self.turn("acknowledgment")
        self.classifier.fail = True
        await self.turn()
        state = await self.store.get("v2")
        state.update(assessmentReviewDecisions={"turn-1": {"abuse": {"status": "true", "evidence": self.stt.text}}},
                     assessmentReviewMetadata={"turn-1": {"rejectedDecisions": {"abuse": self.stt.text}}})
        await self.store.save(state)
        deleted = await self.client.delete("/api/sales/sessions/v2/diagnostics", headers={"Authorization": "Bearer diagnostic"})
        self.assertEqual(deleted.status_code, 200)
        serialized = (Path(self.temp.name) / "sales-session-v2.json").read_text(encoding="utf-8")
        self.assertNotIn(self.stt.text, serialized)
        state = json.loads(serialized)
        self.assertEqual(state["turnCheckpoints"], {})
        self.assertEqual(state["evidenceLedger"][0]["transcriptRef"], "turn-1")
    async def test_session_versions_mode_and_budget_are_frozen_on_resume(self):
        self.settings.sales_pipeline_mode = "legacy"
        self.settings.sales_max_turns = 4
        response = await self.client.post("/api/sales/sessions", json={"sessionId": "v2", "runId": "run-v2"})
        self.assertEqual(response.json()["pipelineVersion"], "sales-openrouter-v3")
        self.assertEqual(response.json()["maxTurns"], 8)

    async def test_dismissive_and_patronizing_language_gets_firm_reply_without_abuse_penalty(self):
        self.stt.text = "Kệ bà."
        turn = await self.turn("disrespect")
        self.assertEqual(turn["turnQuality"], "bad")
        self.assertEqual(turn["policyViolations"], [])
        self.assertIn("thiếu tôn trọng", turn["customerText"])
        self.assertFalse(turn["conversationComplete"])
        turn = await self.turn("disrespect")
        self.assertEqual(turn["policyViolations"], [])
        self.assertEqual((await self.complete())["policyViolationPenalty"], 0)

    async def test_retraction_uses_shared_ordinary_threshold(self):
        await self.turn("unauthorizedRefund")
        turn = await self.turn(values={"retractsUnauthorizedRefund": 0.85})
        self.assertNotEqual(turn["turnQuality"], "uncertain")
        self.assertEqual((await self.complete())["unresolvedPromises"], [])

    async def test_huge_integer_probability_is_uncertain_and_never_an_exception(self):
        turn = await self.turn(values={"unauthorizedRefund": 10 ** 500})
        self.assertEqual(turn["turnQuality"], "uncertain")
        self.assertEqual(turn["policyViolations"], [])

    async def test_missing_fields_and_malformed_response_never_finalize_or_spend_budget(self):
        self.classifier.next = {"legacy": True}
        response = await self.client.post("/api/sales/sessions/v2/turns", json=request().model_dump(by_alias=True))
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error"]["code"], "classifier_invalid")
        self.assertEqual(response.json()["remainingTurns"], 8)
        self.assertIsNone((await self.complete()).get("score"))

    async def test_restart_after_writer_checkpoint_does_not_repeat_classification_or_usage(self):
        original = self.store.finalize_turn
        failed_commit = False
        async def fail_once(*args, **kwargs):
            nonlocal failed_commit
            if not failed_commit:
                failed_commit = True
                raise OSError("simulate interrupted commit")
            return await original(*args, **kwargs)
        with patch.object(self.store, "finalize_turn", side_effect=fail_once):
            failed = await self.turn("acknowledgment", "openQuestion")
        self.assertEqual(failed["status"], "failed")
        classifier_calls, writer_calls = self.classifier.calls, self.writer.calls
        main.sales_returning_store = ReturningSessionStore(Path(self.temp.name))
        response = await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))
        self.assertEqual(response.json()["status"], "accepted")
        self.assertEqual((classifier_calls, writer_calls), (self.classifier.calls, self.writer.calls))
        state = await self.store.get("v2")
        self.assertEqual(state["acceptedTurnCount"], 1)
        self.assertEqual(len(state["evidenceLedger"]), 1)

    async def test_deletion_during_classifier_cannot_resurrect_private_checkpoint(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        async def blocked(transcript, context):
            entered.set()
            await resume.wait()
            return classification("acknowledgment")
        # Freeze a longer deadline just for this explicit concurrency test.
        state = await self.store.get("v2")
        state["jevDeadlineSeconds"] = 2
        await self.store.save(state)
        self.stt.text = "Nội dung riêng tư không được phục hồi"
        with patch.object(self.classifier, "classify", side_effect=blocked):
            task = asyncio.create_task(self.client.post("/api/sales/sessions/v2/turns", json=request().model_dump(by_alias=True)))
            await entered.wait()
            await self.store.delete_diagnostics("v2")
            resume.set()
            response = await task
        self.assertEqual(response.status_code, 409)
        serialized = (Path(self.temp.name) / "sales-session-v2.json").read_text(encoding="utf-8")
        self.assertNotIn(self.stt.text, serialized)

    async def test_shadow_classification_records_proposal_but_preserves_legacy_control(self):
        from app.tests.test_sales_returning_customer import FakeResponder
        self.settings.sales_pipeline_mode = "shadow"
        created = await self.client.post("/api/sales/sessions", json={"sessionId": "shadow"})
        self.assertEqual(created.json()["pipelineVersion"], "sales-legacy-v1")
        self.classifier.next = classification("acknowledgment", "openQuestion")
        with patch.object(main, "LLMSalesResponder", return_value=FakeResponder()):
            response = await self.client.post("/api/sales/sessions/shadow/turns", json=request().model_dump(by_alias=True))
        self.assertEqual(response.json()["activeObjective"], 1)
        state = await self.store.get("shadow")
        self.assertEqual(state["phase"], 1)
        self.assertEqual(state["shadowState"]["phase"], 2)
        self.assertEqual(state["shadowAssessments"]["turn-1"]["proposedOutcome"]["rawScore"], 10)

    async def test_backend_openrouter_authority_beats_all_client_fragments_and_blocks_pending_run(self):
        from app.run_results import RunResultStore, Participant, project_sales_part2
        runs = RunResultStore(Path(self.temp.name))
        participant = Participant("Synthetic fixture participant", "synthetic-participant-session")
        part1 = {"salesSessionId": "v2", "attemptId": "synthetic-part1", "selectedShoeId": "a", "bestFitShoeId": "a",
                 "transcript": "Synthetic Part 1 transcript", "score": 80, "feedbackVi": "Synthetic fixture",
                 "recordingAtUtc": "2026-10-03T00:00:00Z", "assessmentCompletedAtUtc": "2026-10-03T00:00:01Z"}
        await runs.accept_fragment({"runId": "run-v2", "gameId": "sale", "fragmentId": "p1", "data": {"part1": part1}}, participant=participant)
        forged = {"runId": "run-v2", "gameId": "sale", "fragmentId": "forged", "complete": True,
                  "data": {"part2": {"score": 100, "rawScore": 100, "assessmentStatus": "completed", "trustState": "restored"}}}
        pending = await runs.accept_fragment(forged, participant=participant)
        self.assertEqual(pending["status"], "draft")
        self.assertEqual(pending["data"]["part2"]["assessmentStatus"], "pending")
        await self.successful_prefix()
        await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
        await self.complete()
        forged["fragmentId"] = "forged-after-real-completion"
        forged["data"]["part2"]["score"] = 0
        final = await runs.accept_fragment(forged, participant=participant)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["data"]["part2"]["score"], 100)
        self.assertEqual(final["data"]["part2"]["rubricVersion"], RUBRIC_VERSION)
        self.assertEqual(final["data"]["part2"]["assessmentStatus"], "completed")
        self.assertEqual(len(final["data"]["turns"]), 6)

    async def test_retry_cannot_change_capture_to_skip_cached_transcript(self):
        payload = request("capture-retry").model_dump(by_alias=True)
        payload["capture"] = {"deviceReady": True, "permissionGranted": True, "speechDetected": True}
        self.classifier.fail = True
        self.assertEqual((await self.turn(payload=payload))["status"], "failed")
        self.classifier.fail = False
        payload["capture"]["speechDetected"] = False
        rejected = await self.client.post("/api/sales/sessions/v2/turns", json=payload)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual((await self.store.get("v2"))["silenceCount"], 0)
        payload["capture"]["speechDetected"] = True
        with patch.object(self.stt, "transcribe", side_effect=AssertionError("STT is cached")):
            accepted = await self.turn("acknowledgment", "openQuestion", payload=payload)
        self.assertEqual(accepted["activeObjective"], 2)
        self.assertEqual(self.classifier.calls, 2)

    async def test_overlapping_uncertain_labels_do_not_reset_clarification_budget(self):
        for values in ({"unauthorizedRefund": .5}, {"unauthorizedRefund": .5, "unauthorizedDiscount": .5},
                       {"unauthorizedRefund": .5}):
            turn = await self.turn(values=values)
        self.assertEqual(turn["assessmentStatus"], "needs-review")
        self.assertEqual(turn["remainingTurns"], 5)
        self.assertIsNone((await self.complete()).get("score"))

    async def test_uncertain_acknowledgment_requires_clarification_unless_already_accepted(self):
        ambiguous = await self.turn("openQuestion", values={"acknowledgment": .5})
        self.assertEqual(ambiguous["turnQuality"], "uncertain")
        self.assertEqual(ambiguous["remainingTurns"], 7)
        await self.turn("acknowledgment")
        resolved = await self.turn("openQuestion", values={"acknowledgment": .5})
        self.assertEqual(resolved["activeObjective"], 2)
        self.assertNotEqual(resolved["turnQuality"], "uncertain")

    async def test_recognized_headset_question_clarifies_meaning_without_claiming_microphone_failure(self):
        self.stt.text = "Dạ, chị đau ở đâu để em tư vấn tiếp với chị?"
        for index, value in enumerate((.58, .26)):
            payload = request(f"heard-question-{index}").model_dump(by_alias=True)
            payload["capture"] = {"deviceReady": True, "permissionGranted": True, "speechDetected": True}
            turn = await self.turn("openQuestion", "painLocationQuestion",
                                   values={"acknowledgment": value}, payload=payload)
            self.assertEqual(turn["status"], "accepted")
            self.assertEqual(turn["transcript"], self.stt.text)
            self.assertEqual(turn["clarificationReason"], "acknowledgment")
            self.assertNotIn("nghe rõ", turn["customerText"])
            self.assertIn("?", turn["customerText"])
            # A fact question in a turn that is not bad is answered at once.
            self.assertEqual(turn["disclosedFactIds"], ["pain_location"] if index == 0 else [])
            self.assertEqual(turn["turnQuality"], "uncertain")
            self.assertEqual(turn["activeObjective"], 1)
            self.assertEqual(turn["remainingTurns"], 7 - index)
            self.assertEqual(turn["policyViolations"], [])
        self.assertEqual((await self.store.get("v2"))["silenceCount"], 0)

    async def test_uncertain_manager_intent_asks_about_manager_without_claiming_unheard_audio(self):
        turn = await self.turn("openQuestion", values={"managerEscalation": .44})
        self.assertEqual(turn["clarificationReason"], "managerEscalation")
        self.assertIn("quản lý", turn["customerText"])
        self.assertNotIn("nghe rõ", turn["customerText"])
        self.assertFalse(turn["conversationComplete"])
        self.assertEqual(turn["policyViolations"], [])

    async def test_writer_cannot_turn_semantic_uncertainty_into_false_hearing_failure(self):
        async def unheard(plan, transcript, history):
            return {"customerText": "Chị không nghe rõ ý em vừa nói, em nói lại cho chị được không?"}
        with patch.object(self.writer, "write", side_effect=unheard):
            turn = await self.turn("openQuestion", values={"acknowledgment": .58})
        self.assertTrue(turn["fallbackUsed"])
        self.assertNotIn("nghe rõ", turn["customerText"])
        self.assertEqual(turn["turnQuality"], "uncertain")

    async def test_partial_retraction_preserves_other_promise_and_penalties(self):
        await self.turn("unauthorizedRefund", "unauthorizedDiscount")
        await self.turn("retractsUnauthorizedRefund", "acknowledgment", "openQuestion")
        state = await self.store.get("v2")
        self.assertEqual(state["unresolvedPromises"], ["unauthorized_discount"])
        self.assertEqual(state["phase"], 1)
        self.assertEqual(len(state["policyViolations"]), 2)
        self.assertEqual(set(state["evidenceLedger"][-1]["unresolvedPromiseTypesBefore"]),
                         {"unauthorizedRefund", "unauthorizedDiscount"})
        self.assertFalse(state["evidenceLedger"][-1]["challengeShownBefore"])
        turn = await self.turn("maintainsUnauthorizedPromise")
        self.assertTrue(turn["conversationComplete"])
        self.assertEqual((await self.complete())["trustState"], "lost")

    async def test_untyped_multiple_retraction_requests_clarification(self):
        await self.turn("unauthorizedRefund", "unauthorizedDiscount")
        turn = await self.turn(values={"retractsUnauthorizedRefund": .5, "retractsUnauthorizedDiscount": .5})
        self.assertEqual(turn["turnQuality"], "uncertain")
        self.assertEqual((await self.store.get("v2"))["unresolvedPromises"], ["unauthorized_discount", "unauthorized_refund"])

    async def test_second_silence_keeps_one_committed_terminal_text(self):
        for index in range(2):
            payload = request(f"silent-{index}").model_dump(by_alias=True)
            payload["capture"] = {"deviceReady": True, "permissionGranted": True, "speechDetected": False}
            turn = await self.turn(payload=payload)
        resumed = (await self.client.get("/api/sales/sessions/v2")).json()
        self.assertEqual(resumed["lastCustomerText"], turn["customerText"])
        final = await self.complete()
        self.assertEqual(final["finalCustomerText"], turn["customerText"])
        self.assertEqual(final["trustState"], "lost")

    async def test_malformed_noul_diagnostics_survive_evidence_normalization(self):
        self.classifier.next = classification()
        self.classifier.next["labels"]["unauthorizedRefund"] = {
            "noul": None, "rawNoul": "malformed-value", "responseIssue": "invalid_noul"}
        turn = (await self.client.post("/api/sales/sessions/v2/turns", json=request().model_dump(by_alias=True))).json()
        label = turn["turnAssessment"]["unauthorizedRefund"]
        self.assertEqual((label["status"], label["rawNoul"], label["responseIssue"]),
                         ("uncertain", "malformed-value", "invalid_noul"))
        state = await self.store.get("v2")
        self.assertEqual(state["evidenceLedger"][0]["labels"]["unauthorizedRefund"], label)

    async def test_backend_complete_finalizes_ready_run_without_client_terminal_event(self):
        from app.run_results import RunResultStore, Participant
        runs = RunResultStore(Path(self.temp.name))
        participant = Participant("Synthetic participant", "synthetic-session")
        part1 = {"salesSessionId": "v2", "attemptId": "p1", "selectedShoeId": "a", "bestFitShoeId": "a",
                 "transcript": "Synthetic transcript", "score": 80, "feedbackVi": "Synthetic fixture",
                 "recordingAtUtc": "2026-10-03T00:00:00Z", "assessmentCompletedAtUtc": "2026-10-03T00:00:01Z"}
        await runs.accept_fragment({"runId": "run-v2", "gameId": "sale", "fragmentId": "p1",
                                    "data": {"part1": part1}}, participant=participant)
        with patch.object(main, "run_result_store", runs), patch.object(main.participant_manager, "active", participant):
            pending = await self.complete("premature")
            self.assertEqual(pending["assessmentStatus"], "pending")
            self.assertFalse(runs._path("run-v2", ".json").exists())
            await self.successful_prefix()
            await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
            await self.complete()
        final = runs._read(runs._path("run-v2", ".json"))
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["data"]["part2"]["score"], 100)
        self.assertEqual(len(final["data"]["turns"]), 6)
        state = await self.store.get("v2")
        self.assertTrue(state["evidenceLedger"][-1]["challengeShownBefore"])

    async def test_legacy_and_shadow_budget_decrements_and_completion_is_completed(self):
        from app.tests.test_sales_returning_customer import FakeResponder, FakeAnalyzer
        for mode in ("legacy", "shadow"):
            self.settings.sales_pipeline_mode = mode
            await self.client.post("/api/sales/sessions", json={"sessionId": mode})
            with patch.object(main, "LLMSalesResponder", return_value=FakeResponder()):
                turn = (await self.client.post(f"/api/sales/sessions/{mode}/turns", json=request().model_dump(by_alias=True))).json()
            self.assertEqual(turn["remainingTurns"], 3)
            resumed = (await self.client.get(f"/api/sales/sessions/{mode}")).json()
            self.assertEqual(resumed["remainingTurns"], 3)
            with patch.object(main, "LLMSalesAnalyzer", return_value=FakeAnalyzer()):
                completion = await self.client.post(f"/api/sales/sessions/{mode}/complete", json={"completionId": "final", "reason": "time_limit"})
                self.assertEqual(completion.status_code, 200, completion.text)
                final = completion.json()
            self.assertEqual(final["assessmentStatus"], "completed")
            self.assertEqual(final["completionStatus"], "completed")

    async def test_unsupported_frozen_versions_fail_without_new_model_calls_or_rules(self):
        accepted = await self.turn("acknowledgment")
        state = await self.store.get("v2")
        state["questionSetVersion"] = "historical-questions"
        state["promptVersion"] = "historical-prompt"
        await self.store.save(state)
        model_calls = self.classifier.calls, self.writer.calls
        resumed = (await self.client.get("/api/sales/sessions/v2")).json()
        self.assertFalse(resumed["pipelineSupported"])
        self.assertEqual(resumed["pipelineUnavailableReason"], "pipeline_version_unsupported")
        replay = (await self.client.post("/api/sales/sessions/v2/turns", json=request("turn-1").model_dump(by_alias=True))).json()
        self.assertEqual(replay, accepted)
        with patch.object(self.stt, "transcribe", side_effect=AssertionError("Unsupported schema must not transcribe")):
            failed = await self.turn("acknowledgment", "openQuestion")
        self.assertEqual(failed["error"]["code"], "pipeline_version_unsupported")
        self.assertEqual((self.classifier.calls, self.writer.calls), model_calls)
        completion = await self.client.post("/api/sales/sessions/v2/complete", json={"completionId": "unsupported"})
        self.assertEqual(completion.status_code, 503)
        self.assertEqual(completion.json()["detail"], "pipeline_version_unsupported")
        after = await self.store.get("v2")
        for key in ("phase", "acceptedTurnCount", "evaluableTurnCount", "rubricComponents", "policyViolations"):
            self.assertEqual(after[key], state[key])
        self.assertNotIn("score", after)

    async def test_completed_historical_result_remains_authoritative_after_version_change(self):
        await self.successful_prefix()
        await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
        await self.complete()
        state = await self.store.get("v2")
        state["questionSetVersion"] = "historical-questions"
        state["promptVersion"] = "historical-prompt"
        await self.store.save(state)
        final = await self.complete("historical-replay")
        self.assertEqual(final["score"], 100)
        self.assertEqual(final["questionSetVersion"], "historical-questions")
        self.assertEqual(final["promptVersion"], "historical-prompt")
        self.assertEqual(final["completionId"], "final")

    async def test_unsupported_shadow_diagnostics_leave_legacy_control_running(self):
        from app.tests.test_sales_returning_customer import FakeResponder
        self.settings.sales_pipeline_mode = "shadow"
        await self.client.post("/api/sales/sessions", json={"sessionId": "shadow-old"})
        state = await self.store.get("shadow-old")
        state["shadowVersions"]["questionSetVersion"] = "historical-questions"
        await self.store.save(state)
        model_calls = self.classifier.calls
        with patch.object(main, "LLMSalesResponder", return_value=FakeResponder()):
            response = await self.client.post("/api/sales/sessions/shadow-old/turns", json=request().model_dump(by_alias=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["acceptedTurnCount"], 1)
        self.assertEqual(self.classifier.calls, model_calls)
        state = await self.store.get("shadow-old")
        self.assertEqual(state["shadowAssessments"]["turn-1"]["error"], "shadow_version_unsupported")
        self.assertEqual(state["shadowVersions"]["questionSetVersion"], "historical-questions")
