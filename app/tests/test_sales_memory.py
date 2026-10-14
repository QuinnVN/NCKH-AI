"""Customer facts and consent through the real checkpointed sales pipeline."""
import copy
import json
from unittest.mock import patch

from app.tests.test_sales_turn_pipeline import TurnPipelineHarness, SETTINGS
from app.sales_pipeline import process_turn
from app.tests.test_sales_returning_customer import request
from app.sales_openrouter import customer_text_rejections
from app.sales_memory import scenario_context, model_context


class SalesMemoryTests(TurnPipelineHarness):
    async def linked_session(self, *, customer="Lan", run="run-linked", continues=True):
        attempt = {"attemptId": "prior-sale", "salesSessionId": "linked", "runId": run,
                   "customerId": customer, "customerNeeds": "Giày nhẹ để chạy bộ mỗi sáng, thỉnh thoảng đi bộ trên đường nhựa. Ngân sách 1.500.000 đồng.",
                   "selectedShoeId": "Adudu", "availableShoes": [
                       {"shoeId": "Adudu", "name": "Adudu", "price": 1290000,
                        "details": "Chạy bộ đường nhựa, nhẹ và thoáng."}]}
        (self.directory / "sales-persuasion-prior-sale.json").write_text(json.dumps(attempt, ensure_ascii=False), encoding="utf-8")
        self.session_id = "linked"
        with patch("app.sales_returning_customer.get_settings", return_value=SETTINGS):
            return await self.store.create_or_resume("linked", "prior-sale", "run-linked",
                                                     customer_id="Lan", continues_part1_customer=continues)

    async def test_receipt_request_answers_missing_bill_and_cached_retry_does_not_repeat_actions(self):
        self.stt.text = "Chị cho em xin hóa đơn được không ạ?"
        reply = await self.turn("receiptRequest")
        self.assertIn("hóa đơn", reply["customerText"])
        self.assertIn("mất", reply["customerText"])
        state = await self.state()
        self.assertIn("receipt_status", state["investigationEvidence"])
        self.assertEqual(state["customerMemory"]["facts"]["receipt_status"]["sharedTurnId"], "t1")
        replay = await process_turn(self.session_id, request("t1"), store=self.store,
                                    transcriber=self.stt, classifier=self.classifier, writer=self.writer)
        self.assertEqual(replay["customerText"], reply["customerText"])
        self.assertEqual((await self.state())["customerMemory"]["requests"]["receipt_status"], 1)
        self.assertEqual(len(self.writer.plans), 1)

    async def test_prior_sale_facts_survive_recall_without_another_walking_question(self):
        await self.linked_session()
        await self.turn("acknowledgment", "openQuestion")
        self.stt.text = "Lần trước chị đã nói là chị cần giày nhẹ để chạy bộ và đi bộ rồi mà."
        reply = await self.turn("walkingRecall", "preferenceRecall")
        plan = self.writer.plans[-1]
        self.assertNotEqual(plan["missingPart"], "walking")
        self.assertEqual(plan["intent"], "confirm_fact")
        self.assertNotIn("chưa hỏi", reply["customerText"])
        self.assertIn("walking_routine", plan["confirmedFactIds"])
        self.assertEqual((await self.state())["customerMemory"]["facts"]["walking_routine"]["source"], "part1")

    async def test_prior_budget_is_remembered_without_assuming_a_surcharge_budget(self):
        await self.linked_session()
        reply = await self.turn("budgetQuestion")
        self.assertIn("1.500.000", reply["customerText"])
        self.assertIn("chưa xác định", reply["customerText"])

    async def test_answered_socks_remain_known_on_later_turns(self):
        first = await self.turn("socksQuestion")
        self.assertIn("socks", first["disclosedFactIds"])
        state = await self.state()
        self.assertEqual(state["customerMemory"]["facts"]["socks"]["sharedTurnId"], "t1")
        reply = await self.turn("socksQuestion")
        self.assertIn("tất mỏng", reply["customerText"])
        self.assertEqual(self.writer.plans[-1]["intent"], "answer_request")
        self.assertEqual((await self.state())["customerMemory"]["facts"]["socks"]["sharedTurnId"], "t1")

    async def test_linked_preference_does_not_import_unspoken_socks(self):
        await self.linked_session()
        reply = await self.turn("preferenceRecall")
        self.assertNotIn("tất", reply["customerText"])
        self.assertNotIn("socks", (await self.state())["investigationEvidence"])

    async def test_linked_customer_does_not_inherit_a_campus_routine_or_heavy_shoe_diagnosis(self):
        state = await self.linked_session()
        context = model_context(state)
        scenario = scenario_context(context)
        self.assertNotIn("use_duration", scenario["facts"])
        self.assertIn("chưa được xác minh", scenario["actualCause"])
        reply = await self.turn("durationQuestion")
        self.assertIn("chưa nói rõ", reply["customerText"])
        self.assertNotIn("trường", reply["customerText"])

    async def test_customer_asks_for_a_fitted_remedy_instead_of_a_lighter_pair(self):
        from app.sales_customer_lines import choose_customer_line
        from app.sales_openrouter import QUESTION_TEXT
        line = choose_customer_line({"intent": "raise_concern", "concern": 3, "missingPart": "lightweight", "hintLevel": 2})
        self.assertNotIn("nhẹ", line)
        self.assertIn("xử lý", line)
        self.assertIn("Do not require a lighter", QUESTION_TEXT["lightweightForWalking"])

    async def test_writer_cannot_claim_a_shared_routine_is_still_missing(self):
        await self.linked_session()
        await self.turn("acknowledgment", "openQuestion")
        plan = self.writer.plans[-1]
        self.assertIn("false_missing_information", customer_text_rejections(
            "Em chưa hỏi chị dùng đôi giày này vào việc gì.", plan))

    async def test_an_unverified_recall_cannot_unlock_a_private_fact(self):
        await self.turn("acknowledgment", "openQuestion")
        await self.turn("fitRecall")
        state = await self.state()
        self.assertNotIn("fit_condition", state.get("investigationEvidence", []))
        self.assertNotIn("fit_question", state["rubricComponents"])

    async def test_separate_customer_and_mismatched_link_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.linked_session(continues=False)
        with self.assertRaises(ValueError):
            await self.linked_session(customer="other")
        self.assertIsNone(await self.store.get("linked"))

    async def test_wrong_run_link_does_not_seed_facts(self):
        with self.assertRaises(ValueError):
            await self.linked_session(run="another-run")
        self.assertIsNone(await self.store.get("linked"))

    async def test_physical_requests_are_deferred_because_vr_has_no_hand_over(self):
        for label, action, text in (
                ("inspectionRequest", "inspection", "Chị đưa giày cho em xem để em kiểm tra nhé."),
                ("fitOrWalkTrial", "trial", "Chị thử đôi giày mới và đi vài vòng trong tiệm nhé."),
                ("physicalActionRequest", "handover", "Chị đưa giày cho em đi để em đưa chị đôi mới.")):
            self.stt.text = text
            result = await self.turn(label)
            self.assertIn("physical_deferral", self.writer.plans[-1]["requiredContent"])
            self.assertIn("không mang", result["customerText"])
            self.assertNotIn("đã kiểm tra", result["customerText"])
            self.assertNotIn("thử xong", result["customerText"])
            self.assertEqual((await self.state())["customerMemory"]["actions"][action], "deferred")

    async def test_first_operational_actions_do_not_end_a_legitimate_investigation(self):
        await self.turn("acknowledgment", "openQuestion")
        await self.turn("inspectionRequest")
        await self.turn("fitOrWalkTrial")
        reply = await self.turn("purchaseQuestion", "confirmationQuestion")
        self.assertFalse(reply["conversationComplete"])
        self.assertEqual((await self.state())["customerState"]["hintLevels"]["2"], 0)

    async def test_refusing_an_offered_exchange_remembers_its_withdrawal(self):
        await self.turn("exchangeOffer")
        await self.turn("refusesRemedy")
        self.assertEqual((await self.state())["customerMemory"]["actions"]["exchange"], "withdrawn")

    async def test_confirmation_question_does_not_scold_or_forget_an_earlier_answer(self):
        await self.turn("painLocationQuestion")
        self.stt.text = "Vậy là chị đau ở gót chân đúng không ạ?"
        confirmed = await self.turn("painLocationQuestion", "confirmationQuestion")
        self.assertEqual(self.writer.plans[-1]["intent"], "confirm_fact")
        self.assertIn("gót chân", confirmed["customerText"])
        self.assertNotIn("đã nói rồi", confirmed["customerText"])

    async def test_unknown_packaging_and_medical_history_do_not_invent_facts(self):
        for label in ("packagingQuestion", "medicalQuestion"):
            reply = await self.turn(label)
            self.assertEqual(self.writer.plans[-1]["intent"], "answer_request")
            self.assertNotIn("bệnh", reply["customerText"])
            self.assertEqual((await self.state())["customerMemory"]["facts"][
                "packaging_status" if label == "packagingQuestion" else "medical_history"]["certainty"], "unknown")

    async def test_resumed_session_does_not_overwrite_dialogue_memory(self):
        await self.linked_session()
        await self.turn("receiptRequest")
        before = copy.deepcopy((await self.state())["customerMemory"])
        resumed = await self.store.create_or_resume("linked", "prior-sale", "run-linked")
        self.assertEqual(resumed["customerMemory"], before)

    async def test_diagnostic_deletion_scrubs_memory_text_and_keeps_scoring_evidence(self):
        await self.linked_session()
        await self.turn("receiptRequest")
        await self.store.delete_diagnostics("linked")
        state = await self.state()
        self.assertNotIn("customerMemory", state)
        self.assertNotIn("factTexts", state["evidenceLedger"][0])
        self.assertNotIn("dialogueMemory", state["evidenceLedger"][0])
        self.assertIn("receipt_status", state["evidenceLedger"][0]["disclosedFactIds"])
