import asyncio
import base64
import io
import json
import tempfile
import unittest
import wave
import os
import re
import time
from unittest.mock import patch
from pathlib import Path

from app.llm_service import LLMServiceError
from app.sales_returning_customer import (
    CustomerResponse,
    DIALOGUE_VARIANTS,
    ENDING_VARIANTS,
    RECEIPT_REQUEST_RESPONSE,
    REPEATED_INFORMATION_RESPONSE,
    LLMSalesAnalyzer,
    LLMSalesResponder,
    MANAGER_REQUIRED_RESPONSE,
    ModelTurnDraft,
    ReturningSessionStore,
    ReturningTurnRequest,
    TrustAnalysis,
    THINKING_MORE_RESPONSE,
    complete_session,
    _count_outcome,
    _rating_from_assessment,
    submit_turn,
)


def wav(amplitude: int = 1000) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes((int(amplitude).to_bytes(2, "little", signed=True)) * 160)
    return stream.getvalue()


def request(turn_id: str = "turn-1", amplitude: int = 1000) -> ReturningTurnRequest:
    return ReturningTurnRequest.model_validate({
        "turnId": turn_id,
        "audio": {"mimeType": "audio/wav", "encoding": "pcm_s16le", "sampleRateHz": 16000, "channels": 1, "dataBase64": base64.b64encode(wav(amplitude)).decode()},
    })


class FakeTranscriber:
    def __init__(self, text: str): self.text = text
    async def transcribe(self, path: Path) -> str: return self.text


class FakeResponder:
    async def respond(self, session, transcript):
        return response()


def assessment(**overrides):
    result = {
        "emotionalAcknowledgment": False, "openQuestion": False,
        "useOrDurationQuestion": False, "fitConditionOrPreferenceQuestion": False,
        "causeStatement": False, "policyExchange": False,
        "lightweightForWalking": False, "fitOrWalkTrial": False,
        "originalSaleResponsibility": False, "routineMatchExplanation": False,
        "verificationStep": False, "unauthorizedPromise": False,
        "maintainsUnauthorizedPromise": False, "managerEscalation": False,
        "abuse": False,
        "polite": True, "condescending": False,
        "apology": True, "remedy": True,
        "explanation": False, "correctiveAdvice": False,
        "reasonableReturnPolicy": False,
        "beggingWithoutExplanation": False, "apologyOnly": False,
        "profanityOrInsult": False, "repeatedQuestion": False,
        "policyViolations": [],
    }
    result.update(overrides)
    return result


def response(**overrides):
    result = {"customerText": "Chị vẫn chưa rõ nguyên nhân nên cần em hỏi thêm.", "activeObjective": 1, "objectiveCompleted": False, "disclosedFactIds": [], "conversationComplete": False, "deterministicEnding": None, "turnAssessment": assessment()}
    result.update(overrides)
    return CustomerResponse.model_validate(result)


def draft(**overrides):
    result = {"playerResponseRating": "good", "replyIntent": "good",
              "disclosedFactIds": [], "turnAssessment": assessment()}
    result.update(overrides)
    return ModelTurnDraft.model_validate(result)


class FakeAnalyzer:
    async def analyze(self, session):
        return TrustAnalysis.model_validate({"criterionScores": {"apologyAndPolicyRemedy": 35, "adaptabilityAndDeescalation": 20}, "emotionalHandling": True, "causeIdentification": True, "solutionSuitability": True, "trustRebuilding": False})


class ReturningCustomerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {
            "SALES_PIPELINE_MODE": "legacy", "DISABLE_AI_SALE_PT2": "false",
        }))
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReturningSessionStore(Path(self.temp.name))
        self.session = await self.store.create_or_resume("session-1", "part1-1")

    async def asyncTearDown(self): self.temp.cleanup()

    async def test_create_resume_preserves_client_session_and_part1_link(self):
        resumed = await self.store.create_or_resume("session-1", "part1-1")
        self.assertEqual(resumed["sessionId"], "session-1")
        self.assertEqual(resumed["part1AttemptId"], "part1-1")

    async def test_session_file_includes_participant_name_and_is_indented(self):
        session = await self.store.create_or_resume(
            "participant-session", participant_name="Nguyễn An"
        )
        path = self.store._session_path(session["sessionId"])
        contents = path.read_text(encoding="utf-8")

        self.assertEqual(session["participantName"], "Nguyễn An")
        self.assertIn('  "participantName": "Nguyễn An"', contents)
        self.assertIn("\n", contents)

    async def test_placeholder_run_is_rebound_before_the_first_part2_turn(self):
        session = await self.store.create_or_resume(
            "session-placeholder", "part1-placeholder", "session-placeholder"
        )
        self.assertEqual(session["runId"], "session-placeholder")

        rebound = await self.store.create_or_resume(
            "session-placeholder", "part1-placeholder", "simulation-run"
        )

        self.assertEqual(rebound["runId"], "simulation-run")

    async def test_placeholder_run_cannot_change_after_part2_has_turn_state(self):
        session = await self.store.create_or_resume(
            "session-with-turn", "part1-placeholder", "session-with-turn"
        )
        session["turnIds"] = ["turn-1"]
        await self.store.save(session)

        with self.assertRaisesRegex(ValueError, "session is linked to another run"):
            await self.store.create_or_resume(
                "session-with-turn", "part1-placeholder", "simulation-run"
            )

    async def test_delete_diagnostics_scrubs_session_and_blocks_turn_resurrection(self):
        await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Xin lỗi chị."), responder=FakeResponder())
        deleted = await self.store.delete_diagnostics("session-1")
        self.assertGreaterEqual(deleted, 1)
        session = await self.store.get("session-1")
        self.assertTrue(session["diagnosticsDeleted"])
        self.assertEqual(session["turns"], [])
        with self.assertRaises(RuntimeError):
            await submit_turn("session-1", request("turn-2"), store=self.store, transcriber=FakeTranscriber("Xin lỗi chị."), responder=FakeResponder())

    async def test_retention_purge_removes_old_turn_files_and_audits_session(self):
        await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Xin lỗi chị."), responder=FakeResponder())
        old = Path(self.temp.name) / "sales-session-session-1-turn-turn-1.wav"
        os.utime(old, (time.time() - 3 * 86400, time.time() - 3 * 86400))
        with patch.dict(os.environ, {"SALES_RETENTION_DAYS": "1"}):
            removed = await self.store.purge_expired()
        self.assertGreaterEqual(removed, 1)
        session = await self.store.get("session-1")
        self.assertEqual(session["diagnosticRetentionAudit"]["expiredFiles"], removed)

    async def test_turn_persists_audio_transcript_and_model_metadata(self):
        result = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Em xin lỗi chị, chị thấy khó chịu từ khi nào?"), responder=FakeResponder())
        self.assertTrue(result["accepted"])
        self.assertEqual(result["sttProvider"], "unknown")
        self.assertTrue((Path(self.temp.name) / "sales-session-session-1-turn-turn-1.wav").exists())

    async def test_manager_required_customer_reply_ends_the_session_as_lost(self):
        class ManagerRequiredResponder:
            async def respond(self, session, transcript):
                return response(customerText=MANAGER_REQUIRED_RESPONSE)

        result = await submit_turn(
            "session-1",
            request(),
            store=self.store,
            transcriber=FakeTranscriber("Em xin lỗi chị."),
            responder=ManagerRequiredResponder(),
        )

        self.assertEqual(result["customerText"], MANAGER_REQUIRED_RESPONSE)
        self.assertTrue(result["conversationComplete"])
        self.assertEqual(result["deterministicEnding"], "manager_escalation")
        session = await self.store.get("session-1")
        self.assertEqual(session["trustState"], "lost")

    async def test_thinking_more_reply_is_not_replaced_by_the_mandatory_challenge(self):
        session = await self.store.get("session-1")
        session["phase"] = 3
        await self.store.save(session)

        class ThinkingMoreResponder:
            async def respond(self, session, transcript):
                return response(
                    customerText=THINKING_MORE_RESPONSE,
                    activeObjective=4,
                    objectiveCompleted=True,
                    turnAssessment=assessment(
                        policyExchange=True,
                        lightweightForWalking=True,
                        fitOrWalkTrial=True,
                    ),
                )

        result = await submit_turn(
            "session-1",
            request(),
            store=self.store,
            transcriber=FakeTranscriber("Em sẽ đổi đôi nhẹ hơn và mời chị đi thử."),
            responder=ThinkingMoreResponder(),
        )

        self.assertEqual(result["customerText"], THINKING_MORE_RESPONSE)

    async def test_customer_response_uses_configured_llm_timeout(self):
        observed_timeouts = []
        original_wait_for = asyncio.wait_for

        async def track_timeout(awaitable, timeout):
            observed_timeouts.append(timeout)
            return await original_wait_for(awaitable, timeout)

        with (
            patch.dict(os.environ, {"LLM_READ_TIMEOUT_SECONDS": "120"}),
            patch(
                "app.sales_returning_customer.asyncio.wait_for",
                side_effect=track_timeout,
            ),
        ):
            result = await submit_turn(
                "session-1",
                request(),
                store=self.store,
                transcriber=FakeTranscriber("Em xin loi chi."),
                responder=FakeResponder(),
            )

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(observed_timeouts, [20, 120])

    async def test_llm_timeout_returns_specific_responder_error(self):
        class TimeoutResponder:
            async def respond(self, session, transcript):
                raise asyncio.TimeoutError

        result = await submit_turn(
            "session-1",
            request(),
            store=self.store,
            transcriber=FakeTranscriber("Em xin lỗi chị."),
            responder=TimeoutResponder(),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "responder_timeout")

    async def test_llm_responder_retries_an_invalid_upstream_response_once(self):
        valid_content = draft().model_dump_json(by_alias=True)

        class FlakyService:
            configured = True
            last_error = "invalid upstream response"

            def __init__(self):
                self.calls = 0
                self.options = []
                self.messages = []

            async def generate(self, *args, **kwargs):
                self.calls += 1
                self.options.append(kwargs["options"])
                self.messages.append(args[0])
                if self.calls == 1:
                    raise LLMServiceError(
                        "The language model returned an invalid response.",
                        status_code=502,
                    )
                return valid_content

        service = FlakyService()
        result = await submit_turn(
            "session-1",
            request(),
            store=self.store,
            transcriber=FakeTranscriber("Em xin lỗi chị."),
            responder=LLMSalesResponder(service),
        )

        self.assertEqual(service.calls, 2)
        self.assertTrue(all(item["reasoning_effort"] == "none" for item in service.options))
        self.assertTrue(all(messages[-1]["content"].endswith("/no_think") for messages in service.messages))
        self.assertEqual(result["status"], "accepted")
        self.assertIn(result["customerText"], DIALOGUE_VARIANTS["apology_only"])

    async def test_llm_responder_disables_thinking_for_rating_request(self):
        class CapturingService:
            configured = True

            async def generate(self, messages, **kwargs):
                self.messages = messages
                self.options = kwargs["options"]
                return draft().model_dump_json(by_alias=True)

        service = CapturingService()
        result = await LLMSalesResponder(service).respond(
            self.session, "Em xin lỗi chị."
        )

        self.assertEqual(service.options["reasoning_effort"], "none")
        self.assertEqual(service.options["max_tokens"], 500)
        self.assertTrue(service.messages[-1]["content"].endswith("/no_think"))
        self.assertIn("không viết lời thoại", service.messages[0]["content"])
        self.assertIn("khắc phục", service.messages[0]["content"])
        self.assertLess(len(service.messages[0]["content"]), 2000)
        self.assertIn(result.customer_text, DIALOGUE_VARIANTS["apology_only"])

    async def test_llm_analyzer_disables_reasoning_for_short_structured_response(self):
        class CapturingService:
            configured = True

            async def generate(self, *args, **kwargs):
                self.options = kwargs["options"]
                return TrustAnalysis.model_validate(
                    {
                        "criterionScores": {"apologyAndPolicyRemedy": 35, "adaptabilityAndDeescalation": 20},
                        "emotionalHandling": True,
                        "causeIdentification": True,
                        "solutionSuitability": True,
                        "trustRebuilding": False,
                    }
                ).model_dump_json(by_alias=True)

        service = CapturingService()
        result = await LLMSalesAnalyzer(service).analyze(self.session)

        self.assertEqual(service.options["reasoning_effort"], "none")
        self.assertEqual(result.criterion_scores.apology_and_policy_remedy, 35)
        schema = service.options["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["criterionScores"]["properties"]["apologyAndPolicyRemedy"]["maximum"], 50)

    async def test_llm_responder_schema_contains_only_model_owned_fields(self):
        class CapturingService:
            configured = True

            async def generate(self, *args, **kwargs):
                self.response_format = kwargs["options"]["response_format"]
                return draft().model_dump_json(by_alias=True)

        phase_one_service = CapturingService()
        await LLMSalesResponder(phase_one_service).respond(self.session, "Em xin lỗi chị.")
        phase_one_disclosures = phase_one_service.response_format["json_schema"]["schema"][
            "properties"
        ]["disclosedFactIds"]
        self.assertEqual(phase_one_disclosures["maxItems"], 0)
        phase_one_properties = phase_one_service.response_format["json_schema"]["schema"][
            "properties"
        ]
        self.assertEqual(
            set(phase_one_properties),
            {"playerResponseRating", "replyIntent", "disclosedFactIds", "turnAssessment"},
        )
        self.assertEqual(phase_one_properties["playerResponseRating"]["enum"], ["good", "bad"])

        phase_two_session = dict(self.session, phase=2)
        phase_two_service = CapturingService()
        await LLMSalesResponder(phase_two_service).respond(
            phase_two_session, "Chị đi đôi giày này trong bao lâu?"
        )
        phase_two_disclosures = phase_two_service.response_format["json_schema"]["schema"][
            "properties"
        ]["disclosedFactIds"]
        self.assertEqual(
            set(phase_two_disclosures["items"]["enum"]),
            {
                "walking_routine",
                "fit_condition",
                "late_discomfort",
                "lighter_preference",
                "appearance",
            },
        )

    async def test_llm_responder_builds_progression_from_assessment_in_code(self):
        class EvidenceService:
            configured = True

            async def generate(self, *args, **kwargs):
                return draft(
                    turnAssessment=assessment(
                        emotionalAcknowledgment=True,
                        openQuestion=True,
                    )
                ).model_dump_json(by_alias=True)

        result = await LLMSalesResponder(EvidenceService()).respond(
            self.session, "Em xin lỗi chị. Chị đau ở đâu ạ?"
        )

        self.assertEqual(result.active_objective, 2)
        self.assertTrue(result.objective_completed)
        self.assertFalse(result.conversation_complete)
        self.assertIsNone(result.deterministic_ending)

    async def test_llm_responder_uses_a_server_owned_good_reply(self):
        class GoodService:
            configured = True

            async def generate(self, *args, **kwargs):
                return draft(playerResponseRating="good").model_dump_json(by_alias=True)

        result = await submit_turn(
            "session-1", request(), store=self.store,
            transcriber=FakeTranscriber("Em xin lỗi chị, em sẽ đổi đôi nhẹ hơn và mời chị đi thử."),
            responder=LLMSalesResponder(GoodService()),
        )

        self.assertEqual(result["playerResponseRating"], "good")
        self.assertIn(result["customerText"], DIALOGUE_VARIANTS["good"])

    async def test_second_bad_in_first_objective_asks_about_complaint(self):
        class BadService:
            configured = True

            async def generate(self, *args, **kwargs):
                return draft(playerResponseRating="bad", turnAssessment=assessment(apology=False, remedy=False)).model_dump_json(by_alias=True)

        responder = LLMSalesResponder(BadService())
        first = await submit_turn("session-1", request("bad-1"), store=self.store, transcriber=FakeTranscriber("Em xin lỗi chị."), responder=responder)
        second = await submit_turn("session-1", request("bad-2"), store=self.store, transcriber=FakeTranscriber("Em chỉ xin lỗi chị."), responder=responder)

        self.assertIn(first["customerText"], DIALOGUE_VARIANTS["bad"])
        self.assertNotIn(second["customerText"], DIALOGUE_VARIANTS["missing_policy"])
        self.assertFalse(second["conversationComplete"])

    def test_rating_rules_reject_apology_only_and_accept_explanation_with_advice(self):
        apology_only = draft(turnAssessment=assessment(
            remedy=False,
            apologyOnly=True,
        )).turn_assessment
        explanation_and_advice = draft(turnAssessment=assessment(
            apology=False,
            remedy=False,
            explanation=True,
            correctiveAdvice=True,
        )).turn_assessment

        self.assertEqual(_rating_from_assessment(apology_only), "bad")
        self.assertEqual(_rating_from_assessment(explanation_and_advice), "good")

    def test_repeated_customer_question_is_bad_even_with_a_remedy(self):
        repeated_question = draft(turnAssessment=assessment(
            remedy=True,
            repeatedQuestion=True,
        )).turn_assessment

        self.assertEqual(_rating_from_assessment(repeated_question), "bad")

    def test_inspection_followup_counts_prior_apology_without_advancing_objective(self):
        from app.sales_returning_customer import _build_customer_response

        inspection = draft(replyIntent="check", turnAssessment=assessment(
            apology=False, remedy=True, emotionalAcknowledgment=False,
        ))
        prior = {"turnAssessment": assessment(
            apology=True, remedy=False, emotionalAcknowledgment=True,
        )}
        session = dict(self.session, turns=[prior])

        followup = _build_customer_response(session, inspection)
        no_prior_apology = _build_customer_response(dict(self.session, turns=[]), inspection)
        signoff = _build_customer_response(session, draft(
            replyIntent="bad", turnAssessment=assessment(
                apology=False, remedy=False, emotionalAcknowledgment=False,
            ),
        ))
        repeated_question = _build_customer_response(session, draft(
            replyIntent="check", turnAssessment=assessment(
                apology=False, remedy=True, repeatedQuestion=True,
            ),
        ))

        self.assertEqual(followup.player_response_rating, "good")
        self.assertEqual(followup.active_objective, 1)
        self.assertFalse(followup.objective_completed)
        self.assertEqual(no_prior_apology.player_response_rating, "bad")
        self.assertEqual(signoff.player_response_rating, "bad")
        self.assertEqual(repeated_question.player_response_rating, "bad")

    async def test_responder_receives_recent_player_questions_for_repeat_detection(self):
        class CapturingService:
            configured = True

            async def generate(self, messages, **kwargs):
                self.prompt = messages[-1]["content"]
                return draft().model_dump_json(by_alias=True)

        service = CapturingService()
        session = dict(self.session, turns=[
            {"transcript": "Chị thường đi đôi này trong bao lâu?"},
            {"transcript": "Cỡ giày hiện tại có vừa chân chị không?"},
        ])
        await LLMSalesResponder(service).respond(
            session,
            "Chị đi đôi này được bao lâu rồi ạ?",
        )

        self.assertIn("previousPlayerTranscripts", service.prompt)
        self.assertIn("Chị thường đi đôi này trong bao lâu?", service.prompt)

    async def test_receipt_demand_gets_the_requested_customer_reply(self):
        class MisclassifiedService:
            configured = True

            async def generate(self, messages, **kwargs):
                return draft(replyIntent="good").model_dump_json(by_alias=True)

        for transcript in (
            "Chị đưa hóa đơn cho em để đổi giày ạ.",
            "Chị đưa em hóa đơn được không ạ?",
            "Dạ chị có thể đưa em xem thử hóa đơn của mình để em hỗ trợ đổi hàng được không ạ?",
            "Dạ em xin lại hóa đơn ạ.",
            "CHỊ CÓ HÓA ĐƠN KHÔNG Ạ?",
            "Em cần chị mang hóa đơn đến.",
        ):
            with self.subTest(transcript=transcript):
                result = await LLMSalesResponder(MisclassifiedService()).respond(self.session, transcript)
                self.assertEqual(result.customer_text, RECEIPT_REQUEST_RESPONSE)

    async def test_waiving_receipt_does_not_trigger_receipt_demand_reply(self):
        class GoodService:
            configured = True

            async def generate(self, messages, **kwargs):
                return draft(replyIntent="good").model_dump_json(by_alias=True)

        result = await LLMSalesResponder(GoodService()).respond(
            self.session, "Chị không cần đưa hóa đơn để đổi giày đâu ạ."
        )
        self.assertNotEqual(result.customer_text, RECEIPT_REQUEST_RESPONSE)

    async def test_asking_about_a_fact_customer_already_volunteered_gets_requested_reply(self):
        class MissedRepeatService:
            configured = True

            async def generate(self, messages, **kwargs):
                return draft(replyIntent="good", turnAssessment=assessment(
                    apology=False, remedy=False, openQuestion=True, repeatedQuestion=False,
                )).model_dump_json(by_alias=True)

        session = dict(self.session, phase=2, turns=[{
            "transcript": "Em sẽ kiểm tra đôi giày trước.",
            "customerText": "Chị đi bộ nhiều giữa các lớp mỗi ngày.",
        }])
        result = await LLMSalesResponder(MissedRepeatService()).respond(
            session, "Chị thường đi lại mỗi ngày nhiều không ạ?"
        )
        self.assertTrue(result.turn_assessment.repeated_question)
        self.assertEqual(result.customer_text, REPEATED_INFORMATION_RESPONSE)
        self.assertEqual(result.player_response_rating, "bad")

    async def test_new_customer_question_does_not_count_as_a_repeat(self):
        class GoodService:
            configured = True

            async def generate(self, messages, **kwargs):
                return draft(replyIntent="good", turnAssessment=assessment(
                    apology=False, remedy=False, openQuestion=True,
                )).model_dump_json(by_alias=True)

        session = dict(self.session, phase=2, turns=[{
            "transcript": "Em sẽ kiểm tra đôi giày trước.",
            "customerText": "Chị đi bộ nhiều giữa các lớp mỗi ngày.",
        }])
        result = await LLMSalesResponder(GoodService()).respond(
            session, "Chị đau ở gót chân hay mũi chân ạ?"
        )
        self.assertFalse(result.turn_assessment.repeated_question)
        self.assertNotEqual(result.customer_text, REPEATED_INFORMATION_RESPONSE)

    async def test_hybrid_responder_grounds_receipt_and_volunteered_repeat(self):
        class SparseService:
            configured = True

            async def generate(self, messages, **kwargs):
                return json.dumps({"replyIntent": "good", "trueFlags": [],
                                   "disclosedFactIds": [], "policyViolations": []})

        with patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "true"}):
            receipt = await LLMSalesResponder(SparseService()).respond(
                self.session, "Chị mang hóa đơn đến cho em nhé."
            )
            session = dict(self.session, phase=2, turns=[{
                "transcript": "Em sẽ xem đôi giày.",
                "customerText": "Chị đi bộ nhiều giữa các lớp mỗi ngày.",
            }])
            repeated = await LLMSalesResponder(SparseService()).respond(
                session, "Chị đi bộ nhiều mỗi ngày không ạ?"
            )

        self.assertEqual(receipt.customer_text, RECEIPT_REQUEST_RESPONSE)
        self.assertEqual(repeated.customer_text, REPEATED_INFORMATION_RESPONSE)

    async def test_responder_receives_every_prior_player_transcript_without_truncation(self):
        class CapturingService:
            configured = True

            async def generate(self, messages, **kwargs):
                self.prompt = messages[-1]["content"]
                return draft().model_dump_json(by_alias=True)

        service = CapturingService()
        first = "Chị thường đi bộ bao lâu mỗi ngày? " + "chi tiết " * 65 + "Thông tin đầu tiên."
        turns = [{"transcript": first}] + [
            {"transcript": f"Lượt hỏi thứ {index}."} for index in range(2, 6)
        ]
        result = await LLMSalesResponder(service).respond(
            dict(self.session, turns=turns), "Chị thường đi bộ bao lâu mỗi ngày ạ?"
        )

        self.assertIn(first, service.prompt)
        self.assertIn("Lượt hỏi thứ 5.", service.prompt)
        self.assertEqual(result.player_response_rating, "bad")

    async def test_responder_receives_customer_history_and_dialogue_state(self):
        class CapturingService:
            configured = True

            async def generate(self, messages, **kwargs):
                self.prompt = messages[-1]["content"]
                return draft().model_dump_json(by_alias=True)

        service = CapturingService()
        previous_line = DIALOGUE_VARIANTS["good"][0]
        session = dict(self.session, phase=2, acceptedTurnCount=1,
                       investigationEvidence=["walking_routine"],
                       turns=[{"transcript": "Chị đi nhiều không?", "customerText": previous_line}])
        await LLMSalesResponder(service).respond(session, "Em sẽ kiểm tra đôi giày cho chị.")

        self.assertIn('"dialogueState"', service.prompt)
        self.assertIn('"phase":2', service.prompt)
        self.assertIn('"facts":["walking_routine"]', service.prompt)
        self.assertIn('"customerHistory"', service.prompt)
        self.assertIn(previous_line, service.prompt)

    async def test_latest_sale_run_does_not_ask_for_an_exchange_plan_again(self):
        """Replay the repeated request for a remedy from the 29 September Sale run."""
        class ReplayService:
            configured = True

            async def generate(self, messages, **kwargs):
                return draft(replyIntent="bad", turnAssessment=assessment(
                    apology=False, remedy=True, explanation=True, correctiveAdvice=True,
                )).model_dump_json(by_alias=True)

        session = dict(self.session, turns=[
            {"transcript": "Em xin lỗi chị. Chính sách bảy ngày còn nguyên vẹn, em sẽ đổi hàng cho chị.",
             "customerText": "Được, chị nghe. Bước tiếp theo cụ thể là gì?",
             "turnAssessment": assessment(policyExchange=True, reasonableReturnPolicy=True)},
            {"transcript": "Chị đưa giày cho em, em sẽ đổi hàng và tư vấn đôi phù hợp hơn.",
             "customerText": "Chị chưa yên tâm với lời giải thích đó. Em nói rõ hơn đi.",
             "turnAssessment": assessment(remedy=True)},
        ])
        with patch("app.sales_returning_customer.random.choice", side_effect=lambda options: options[2]):
            reply = await LLMSalesResponder(ReplayService()).respond(
                session,
                "Chị đưa giày cho em để em hỗ trợ đổi đôi mới theo chính sách của tiệm.",
            )

        self.assertNotIn("cần một cách xử lý cụ thể", reply.customer_text)

    def test_customer_lines_do_not_coach_the_player(self):
        from app.sales_returning_customer import REMEDY_ACKNOWLEDGMENTS

        coach_pattern = re.compile(
            r"\bem\s+(?:hãy|hỏi|tìm hiểu|xem|kiểm tra|giải thích|hướng dẫn|tư vấn|nói rõ|trả lời thẳng|nhớ)\b",
            re.IGNORECASE,
        )
        for line in (*[line for variants in DIALOGUE_VARIANTS.values() for line in variants],
                     *REMEDY_ACKNOWLEDGMENTS):
            with self.subTest(line=line):
                self.assertIsNone(coach_pattern.search(line))

        self.assertNotIn(
            "Chị muốn em hỏi thêm về chỗ đau trước khi chọn cách xử lý.",
            DIALOGUE_VARIANTS["clarify_complaint"],
        )

    def test_prior_exchange_plan_prevents_a_repeated_remedy_request(self):
        from app.sales_returning_customer import _canned_customer_response

        session = dict(self.session, phase=1, turns=[{
            "transcript": "Em sẽ đổi đôi giày này cho chị.",
            "customerText": "Chị vẫn muốn hiểu vì sao đôi này làm chị đau.",
            "turnAssessment": assessment(remedy=True, openQuestion=False),
        }])
        with patch("app.sales_returning_customer.random.choice", side_effect=lambda options: options[2]):
            reply = _canned_customer_response(session, "bad", "bad", draft(turnAssessment=assessment(
                apology=False, remedy=False, openQuestion=False,
            )).turn_assessment)

        self.assertNotIn("cần một cách xử lý cụ thể", reply)

    async def test_same_intent_uses_each_variant_once(self):
        from app.sales_returning_customer import _canned_customer_response

        session = dict(self.session, turns=[])
        lines = []
        for _ in range(len(DIALOGUE_VARIANTS["repeated_question"])):
            line = _canned_customer_response(session, "repeated_question", "bad", draft(
                turnAssessment=assessment(apology=False, remedy=False, repeatedQuestion=True)
            ).turn_assessment)
            lines.append(line)
            session["turns"].append({"customerText": line})

        self.assertEqual(len(set(lines)), len(DIALOGUE_VARIANTS["repeated_question"]))
        self.assertEqual(set(lines), set(DIALOGUE_VARIANTS["repeated_question"]))

    async def test_hybrid_responder_receives_customer_history_and_state(self):
        class CapturingService:
            configured = True

            async def generate(self, messages, **kwargs):
                self.prompt = messages[-1]["content"]
                return json.dumps({"replyIntent": "check", "trueFlags": ["apology", "remedy"],
                                   "disclosedFactIds": [], "policyViolations": []})

        service = CapturingService()
        session = dict(self.session, turns=[{"transcript": "Xin lỗi chị.",
                                            "customerText": DIALOGUE_VARIANTS["good"][0]}])
        with patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "true"}):
            await LLMSalesResponder(service).respond(session, "Em xin lỗi chị, em sẽ kiểm tra giày.")

        self.assertIn("dialogueState", service.prompt)
        self.assertIn("customerHistory", service.prompt)
        self.assertIn(DIALOGUE_VARIANTS["good"][0], service.prompt)

    async def test_hybrid_responder_accepts_flags_misplaced_in_other_arrays(self):
        class MisplacedFlagsService:
            configured = True

            def __init__(self):
                self.calls = 0

            async def generate(self, messages, **kwargs):
                self.calls += 1
                return json.dumps({
                    "replyIntent": "policy",
                    "trueFlags": ["apology", "explanation", "correctiveAdvice"],
                    "disclosedFactIds": ["causeStatement", "routineMatchExplanation"],
                    "policyViolations": ["policyExchange", "lightweightForWalking"],
                })

        service = MisplacedFlagsService()
        with patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "true"}):
            result = await LLMSalesResponder(service).respond(
                self.session,
                "Dạ, tụi em sẽ đổi giày cùng mức giá cho chị.",
            )

        self.assertEqual(service.calls, 1)
        self.assertEqual(result.disclosed_fact_ids, [])
        self.assertEqual(result.turn_assessment.policy_violations, [])

    async def test_silent_customer_reply_is_in_next_prompt(self):
        class CapturingService:
            configured = True

            async def generate(self, messages, **kwargs):
                self.prompt = messages[-1]["content"]
                return draft().model_dump_json(by_alias=True)

        service = CapturingService()
        session = dict(self.session, completedTurns={"silent-1": {
            "status": "silent", "customerText": "Chị chưa nghe rõ em, em nói lại được không?"
        }})
        await LLMSalesResponder(service).respond(session, "Em xin lỗi chị.")
        self.assertIn("Chị chưa nghe rõ em, em nói lại được không?", service.prompt)

    def test_disclosed_facts_are_actually_spoken_without_repeating(self):
        from app.sales_returning_customer import _canned_customer_response, _valid_customer_text

        session = dict(self.session, phase=2, turns=[])
        facts = ["walking_routine", "late_discomfort", "fit_condition",
                 "lighter_preference", "appearance"]
        replies = []
        for _ in range(4):
            reply = _canned_customer_response(session, "fact_disclosure", "good", draft().turn_assessment, facts)
            self.assertTrue(_valid_customer_text(reply))
            self.assertTrue(any(word in reply.lower() for word in ("đi bộ", "đi lại")))
            self.assertIn("màu", reply.lower())
            replies.append(reply)
            session["turns"].append({"customerText": reply})
        self.assertEqual(len(set(replies)), 4)

    def test_abusive_turn_does_not_disclose_model_suggested_fact(self):
        from app.sales_returning_customer import _build_customer_response

        session = dict(self.session, phase=2)
        result = _build_customer_response(session, draft(
            disclosedFactIds=["walking_routine"],
            turnAssessment=assessment(apology=False, remedy=False, polite=False,
                                      abuse=True, useOrDurationQuestion=True),
        ))
        self.assertIn(result.customer_text, DIALOGUE_VARIANTS["abuse"])
        self.assertEqual(result.disclosed_fact_ids, [])

    def test_grounding_detects_paraphrased_repeat_and_drops_unasked_color(self):
        from app.sales_returning_customer import _ground_turn_draft

        repeated = _ground_turn_draft(draft(turnAssessment=assessment(
            apology=False, remedy=False, repeatedQuestion=False,
        )), "Chị đau ở chỗ nào ạ?", ["Chị đau ở đâu ạ?"])
        self.assertTrue(repeated.turn_assessment.repeated_question)

        facts = _ground_turn_draft(draft(
            disclosedFactIds=["walking_routine", "fit_condition", "appearance"],
            turnAssessment=assessment(useOrDurationQuestion=False,
                                      fitConditionOrPreferenceQuestion=True),
        ), "Chị thường đi bộ bao lâu mỗi ngày và giày hiện tại có vừa chân không ạ?", [])
        self.assertTrue(facts.turn_assessment.use_or_duration_question)
        self.assertEqual(facts.disclosed_fact_ids, ["walking_routine", "fit_condition"])

        statement = _ground_turn_draft(draft(), "Đôi giày này không hợp đi bộ nhiều.",
                                       ["Chị thường đi bộ nhiều không ạ?"])
        self.assertFalse(statement.turn_assessment.repeated_question)

    def test_hybrid_explanation_with_corrective_advice_is_good(self):
        from app.sales_returning_customer import _hybrid_turn_draft

        cause = _hybrid_turn_draft({"replyIntent": "cause", "trueFlags": ["causeStatement"],
                                   "disclosedFactIds": [], "policyViolations": []}, 2,
                                  "Đôi giày nặng không hợp đi bộ nhiều, em sẽ tư vấn đôi nhẹ hơn.")
        self.assertEqual(cause.player_response_rating, "good")

        trust = _hybrid_turn_draft({"replyIntent": "check", "trueFlags": ["originalSaleResponsibility",
                                                  "routineMatchExplanation", "verificationStep"],
                                   "disclosedFactIds": [], "policyViolations": []}, 4,
                                  "Lần trước em chưa hỏi nhu cầu đi bộ. Đôi nhẹ phù hợp hơn, "
                                  "chị đi thử để kiểm tra nhé.")
        self.assertEqual(trust.player_response_rating, "good")

    def test_hybrid_policy_solution_can_close_before_four_turns(self):
        from app.sales_returning_customer import _build_customer_response, _hybrid_turn_draft

        session = dict(self.session, phase=2, acceptedTurnCount=1, goodResponseCount=1,
                       turns=[{"turnAssessment": assessment(emotionalAcknowledgment=True)}])
        transcript = "Em hỗ trợ đổi đôi nhẹ hợp đi bộ và mời chị đi thử để kiểm tra độ vừa."
        classified = _hybrid_turn_draft({
            "replyIntent": "check",
            "trueFlags": ["policyExchange", "lightweightForWalking", "fitOrWalkTrial"],
            "disclosedFactIds": [], "policyViolations": [],
        }, 2, transcript)
        result = _build_customer_response(session, classified)

        self.assertTrue(result.conversation_complete)
        self.assertIn(result.customer_text, ENDING_VARIANTS["good"])

    def test_replay_insults_override_false_model_flags(self):
        from app.sales_returning_customer import _ground_turn_draft, _rating_from_assessment

        for transcript in (
            "Kệ bà chứ bà già khó tính.",
            "Tôi không quan tâm, tôi kệ bà, không muốn tư vấn. Đi về đi.",
            "KỆ MẸ MÀY",
        ):
            grounded = _ground_turn_draft(draft(turnAssessment=assessment(
                abuse=False, profanityOrInsult=False, polite=True,
                openQuestion=True, remedy=True,
            )), transcript, [])
            self.assertTrue(grounded.turn_assessment.abuse)
            self.assertTrue(grounded.turn_assessment.profanity_or_insult)
            self.assertFalse(grounded.turn_assessment.polite)
            self.assertFalse(grounded.turn_assessment.open_question)
            self.assertEqual(_rating_from_assessment(grounded.turn_assessment), "bad")

    def test_replay_discount_promise_overrides_false_model_flags(self):
        from app.sales_returning_customer import _ground_turn_draft

        grounded = _ground_turn_draft(draft(turnAssessment=assessment(
            unauthorizedPromise=False, policyViolations=[],
        )), "Tôi giảm giá cho bà nha.", [])
        self.assertTrue(grounded.turn_assessment.unauthorized_promise)
        self.assertIn("unauthorized_discount", grounded.turn_assessment.policy_violations)

        denial = _ground_turn_draft(draft(turnAssessment=assessment(
            unauthorizedPromise=False, policyViolations=[],
        )), "Tiệm em không hoàn tiền hay giảm giá cho chị.", [])
        self.assertFalse(denial.turn_assessment.unauthorized_promise)
        self.assertEqual(denial.turn_assessment.policy_violations, [])

    def test_replay_stated_exchange_policy_is_not_treated_as_missing(self):
        from app.sales_returning_customer import _ground_turn_draft, _build_customer_response

        transcript = ("Chính sách đổi hàng trong bảy ngày nếu giày còn nguyên vẹn "
                      "thì em sẽ đổi giày cho chị, không hoàn tiền hay giảm giá.")
        grounded = _ground_turn_draft(draft(turnAssessment=assessment(
            apology=False, remedy=False, policyExchange=False,
            reasonableReturnPolicy=False,
        )), transcript, [])
        self.assertTrue(grounded.turn_assessment.reasonable_return_policy)
        session = dict(self.session, phase=1, missingReturnPolicyCount=2)
        reply = _build_customer_response(session, grounded)
        self.assertNotIn(reply.customer_text, DIALOGUE_VARIANTS["missing_policy"])
        self.assertFalse(reply.objective_completed)

    def test_latest_sale_replies_to_policy_and_pain_question(self):
        from app.sales_returning_customer import _build_customer_response

        session = dict(self.session, phase=1, turns=[])
        policy = draft(replyIntent="policy", turnAssessment=assessment(
            apology=False, remedy=True, policyExchange=True,
            reasonableReturnPolicy=True, openQuestion=False,
        ))
        first = _build_customer_response(session, policy)
        self.assertIn(first.customer_text, DIALOGUE_VARIANTS["policy"])
        session["turns"].append({"customerText": first.customer_text})

        question = draft(replyIntent="pain_location", turnAssessment=assessment(
            apology=False, remedy=False, openQuestion=True,
        ))
        second = _build_customer_response(session, question)
        self.assertIn(second.customer_text, DIALOGUE_VARIANTS["pain_location"])
        self.assertNotIn("Em nói nhiều", second.customer_text)

    def test_phase_one_accepts_apology_then_pain_question_across_turns(self):
        from app.sales_returning_customer import _build_customer_response

        prior = {"objectiveActiveDuringTurn": 1,
                 "turnAssessment": assessment(apology=True, remedy=False,
                                               emotionalAcknowledgment=True)}
        session = dict(self.session, phase=1, turns=[prior])
        question = draft(replyIntent="pain_location", turnAssessment=assessment(
            apology=False, remedy=False, emotionalAcknowledgment=False,
            openQuestion=True,
        ))
        result = _build_customer_response(session, question)
        self.assertEqual(result.active_objective, 2)
        self.assertTrue(result.objective_completed)

    async def test_hybrid_unknown_intent_is_grounded_to_pain_question(self):
        class NoisyService:
            configured = True

            async def generate(self, *args, **kwargs):
                return json.dumps({
                    "replyIntent": "openQuestion",
                    "trueFlags": ["apology", "repeatedQuestion", "check"],
                    "disclosedFactIds": [], "policyViolations": [],
                })

        with patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "true"}):
            result = await LLMSalesResponder(NoisyService()).respond(
                self.session, "ỦA GÌ DẠ CHỊ ĐAU CHỖ NÀO VẬY CHỊ"
            )
        self.assertIn(result.customer_text, DIALOGUE_VARIANTS["pain_location"])
        self.assertEqual(result.player_response_rating, "good")
        self.assertTrue(result.turn_assessment.open_question)
        self.assertFalse(result.turn_assessment.apology)
        self.assertFalse(result.turn_assessment.repeated_question)

    async def test_apology_only_flag_cannot_override_a_policy_compliant_remedy(self):
        class ContradictoryService:
            configured = True

            async def generate(self, *args, **kwargs):
                return draft(turnAssessment=assessment(
                    apology=True,
                    remedy=True,
                    reasonableReturnPolicy=True,
                    apologyOnly=True,
                )).model_dump_json(by_alias=True)

        result = await LLMSalesResponder(ContradictoryService()).respond(
            self.session,
            "Dạ, bên em sẽ hỗ trợ đổi đôi nguyên vẹn sang đôi cùng phân khúc giá. "
            "Tiệm không hoàn tiền, em xin lỗi và sẽ tư vấn size kỹ cho chị ạ.",
        )

        self.assertEqual(result.player_response_rating, "good")
        self.assertIn(result.customer_text, DIALOGUE_VARIANTS["policy"])

    async def test_abusive_turn_uses_warning_reply_and_records_violation(self):
        class AbusiveService:
            configured = True

            async def generate(self, *args, **kwargs):
                return draft(turnAssessment=assessment(
                    apology=False,
                    remedy=False,
                    polite=False,
                    abuse=True,
                    profanityOrInsult=True,
                )).model_dump_json(by_alias=True)

        result = await submit_turn(
            "session-1", request(), store=self.store,
            transcriber=FakeTranscriber("Khách gì mà phiền quá."),
            responder=LLMSalesResponder(AbusiveService()),
        )
        session = await self.store.get("session-1")

        self.assertEqual(result["playerResponseRating"], "bad")
        self.assertIn(result["customerText"], DIALOGUE_VARIANTS["abuse"])
        self.assertFalse(result["conversationComplete"])
        self.assertIsNone(result["deterministicEnding"])
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["policyViolations"], [
            {"turnId": "turn-1", "code": "abusive_language"},
        ])

    async def test_duplicate_turn_is_idempotent(self):
        first = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Xin lỗi chị."), responder=FakeResponder())
        second = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Khác."), responder=FakeResponder())
        self.assertEqual(first, second)
        self.assertEqual((await self.store.get("session-1"))["acceptedTurnCount"], 1)

    async def test_fourth_accepted_turn_requests_completion(self):
        for index in range(4):
            result = await submit_turn(
                "session-1", request(f"turn-{index + 1}"), store=self.store,
                transcriber=FakeTranscriber("Em cần hỏi thêm."), responder=FakeResponder(),
            )
            self.assertEqual(result["status"], "accepted")

        session = await self.store.get("session-1")
        self.assertEqual(session["acceptedTurnCount"], 4)
        self.assertEqual(session["status"], "awaitingCompletion")

    async def test_resolved_customer_can_end_after_second_accepted_turn(self):
        from app.sales_returning_customer import _build_customer_response

        session = await self.store.get("session-1")
        session.update({"phase": 2, "acceptedTurnCount": 1, "goodResponseCount": 1,
                        "turns": [{"turnId": "first", "transcript": "Em xin lỗi chị.",
                                   "customerText": "Chị muốn em xử lý đôi giày này.",
                                   "turnAssessment": assessment(emotionalAcknowledgment=True)}]})
        await self.store.save(session)

        class ResolvingResponder:
            async def respond(self, current, transcript):
                return _build_customer_response(current, draft(turnAssessment=assessment(
                    apology=False, emotionalAcknowledgment=False, policyExchange=True,
                    lightweightForWalking=True, fitOrWalkTrial=True,
                    reasonableReturnPolicy=True,
                )))

        result = await submit_turn(
            "session-1", request("second"), store=self.store,
            transcriber=FakeTranscriber("Em sẽ đổi đôi nhẹ hợp đi bộ, rồi mời chị đi thử."),
            responder=ResolvingResponder(),
        )
        saved = await self.store.get("session-1")
        self.assertTrue(result["conversationComplete"])
        self.assertEqual(saved["acceptedTurnCount"], 2)
        self.assertEqual(saved["status"], "awaitingCompletion")
        self.assertIn(result["customerText"], ENDING_VARIANTS["good"])

        completed = await complete_session(
            "session-1", type("Req", (), {"completion_id": "early", "reason": "natural"})(),
            store=self.store, analyzer=FakeAnalyzer(),
        )
        self.assertEqual(completed["finalCustomerText"], result["customerText"])

    async def test_natural_completion_accepts_an_active_session_after_one_turn(self):
        await submit_turn("session-1", request(), store=self.store,
                          transcriber=FakeTranscriber("Em xin lỗi chị."), responder=FakeResponder())
        completed = await complete_session(
            "session-1", type("Req", (), {"completion_id": "early-stop", "reason": "natural"})(),
            store=self.store, analyzer=FakeAnalyzer(),
        )
        self.assertEqual(completed["acceptedTurnCount"], 1)
        self.assertEqual(completed["completionReason"], "natural")

    async def test_natural_completion_requires_an_accepted_turn(self):
        with self.assertRaisesRegex(RuntimeError, "completion_not_ready"):
            await complete_session(
                "session-1", type("Req", (), {"completion_id": "too-early", "reason": "natural"})(),
                store=self.store, analyzer=FakeAnalyzer(),
            )

    async def test_silence_does_not_consume_accepted_turn(self):
        result = await submit_turn("session-1", request(amplitude=0), store=self.store, transcriber=FakeTranscriber("never"), responder=FakeResponder())
        self.assertFalse(result["accepted"])
        self.assertEqual((await self.store.get("session-1"))["silenceCount"], 1)

    async def test_language_rejection_is_removed_for_controlled_candidates(self):
        result = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("This is an English response."), responder=FakeResponder())
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["language"], "vi")
        self.assertEqual((await self.store.get("session-1"))["acceptedTurnCount"], 1)

    async def test_completion_is_idempotent_and_returns_score_and_customer_rating(self):
        result = await complete_session("session-1", type("Req", (), {"completion_id": "completion-1", "reason": "time_limit"})(), store=self.store, analyzer=FakeAnalyzer())
        repeated = await complete_session("session-1", type("Req", (), {"completion_id": "completion-1", "reason": "time_limit"})(), store=self.store, analyzer=FakeAnalyzer())
        self.assertEqual(result, repeated)
        self.assertEqual(result["trustState"], "partially_restored")
        self.assertEqual(result["score"], 55)
        self.assertEqual(result["customerRating"], "considering")
        self.assertEqual(result["criterionScores"], {"apologyAndPolicyRemedy": 35, "adaptabilityAndDeescalation": 20})

    async def test_policy_violations_each_deduct_ten_points(self):
        session = await self.store.get("session-1")
        session.update({
            "status": "finished",
            "goodResponseCount": 0,
            "badResponseCount": 1,
            "policyViolations": [
                {"turnId": "turn-1", "code": "unauthorized_refund"},
                {"turnId": "turn-1", "code": "unauthorized_discount"},
            ],
        })
        await self.store.save(session)

        class OvergenerousAnalyzer:
            async def analyze(self, session):
                return TrustAnalysis.model_validate({"criterionScores": {"apologyAndPolicyRemedy": 50, "adaptabilityAndDeescalation": 50}, "emotionalHandling": True, "causeIdentification": True, "solutionSuitability": True, "trustRebuilding": True})

        result = await complete_session("session-1", type("Req", (), {"completion_id": "failed-ending", "reason": "natural"})(), store=self.store, analyzer=OvergenerousAnalyzer())

        self.assertEqual(result["rawScore"], 100)
        self.assertEqual(result["policyViolationPenalty"], 20)
        self.assertEqual(result["score"], 80)
        self.assertEqual(result["customerRating"], "bad")
        self.assertEqual(result["trustState"], "lost")
        self.assertEqual(len(result["policyViolations"]), 2)

    async def test_good_majority_controls_trust_state_and_final_customer_line(self):
        session = await self.store.get("session-1")
        session.update({"goodResponseCount": 3, "badResponseCount": 1})
        await self.store.save(session)

        result = await complete_session(
            "session-1",
            type("Req", (), {"completion_id": "good-ending", "reason": "time_limit"})(),
            store=self.store,
            analyzer=FakeAnalyzer(),
        )

        self.assertEqual(result["trustState"], "restored")
        self.assertEqual(result["customerRating"], "good")
        self.assertIn(result["finalCustomerText"], ENDING_VARIANTS["good"])

    def test_good_bad_counts_map_to_customer_rating(self):
        self.assertEqual(_count_outcome(2, 1), ("good", "restored"))
        self.assertEqual(_count_outcome(1, 2), ("bad", "lost"))
        self.assertEqual(_count_outcome(2, 2), ("considering", "partially_restored"))

    async def test_later_phase_guess_cannot_advance_without_disclosed_investigation(self):
        class GuessingResponder:
            async def respond(_, session, transcript):
                return response(activeObjective=3, objectiveCompleted=True, turnAssessment=assessment(causeStatement=True))
        session = await self.store.get("session-1")
        session["phase"] = 2
        await self.store.save(session)
        result = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Giày nặng không hợp chị."), responder=GuessingResponder())
        self.assertEqual(result["status"], "failed")
        self.assertEqual((await self.store.get("session-1"))["phase"], 2)

    async def test_phase_two_requires_prior_use_and_secondary_fact_before_cause(self):
        session = await self.store.get("session-1")
        session.update({"phase": 2, "investigationEvidence": ["walking_routine", "lighter_preference"]})
        await self.store.save(session)
        class CauseResponder:
            async def respond(_, session, transcript):
                return response(activeObjective=3, objectiveCompleted=True, customerText="Chị hiểu vì sao đôi giày này không phù hợp.", turnAssessment=assessment(causeStatement=True))
        result = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Giày nặng không phù hợp với việc chị đi bộ hằng ngày."), responder=CauseResponder())
        self.assertEqual(result["activeObjective"], 3)
        self.assertTrue(result["objectiveCompleted"])

    async def test_customer_reply_cannot_disclose_later_fact_or_markdown(self):
        class InvalidResponder:
            async def respond(_, session, transcript):
                return response(customerText="- Chị bị đau do lỗi giày.", disclosedFactIds=["walking_routine"])
        result = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Em xin lỗi chị."), responder=InvalidResponder())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "invalid_customer_text")

    async def test_semantic_unauthorized_promise_is_challenged_then_maintained_ends_lost(self):
        class PromiseResponder:
            def __init__(self): self.calls = 0
            async def respond(self, session, transcript):
                self.calls += 1
                return response(turnAssessment=assessment(unauthorizedPromise=True, maintainsUnauthorizedPromise=self.calls == 2))
        responder = PromiseResponder()
        first = await submit_turn("session-1", request("turn-promise-1"), store=self.store, transcriber=FakeTranscriber("Em bảo đảm chị sẽ không đau chân."), responder=responder)
        second = await submit_turn("session-1", request("turn-promise-2"), store=self.store, transcriber=FakeTranscriber("Em vẫn bảo đảm chị sẽ không đau chân."), responder=responder)
        self.assertTrue((await self.store.get("session-1"))["unauthorizedPromiseChallenged"])
        self.assertEqual(first["deterministicEnding"], None)
        self.assertEqual(second["deterministicEnding"], "maintained_unauthorized_promise")
        self.assertEqual((await self.store.get("session-1"))["trustState"], "lost")

    async def test_completed_turn_hash_rejects_changed_retry_and_session_cache_prevents_double_count(self):
        first = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Em xin lỗi chị."), responder=FakeResponder())
        self.assertEqual(first["status"], "accepted")
        with self.assertRaises(ValueError):
            await submit_turn("session-1", request(amplitude=2000), store=self.store, transcriber=FakeTranscriber("Em xin lỗi chị."), responder=FakeResponder())
        session = await self.store.get("session-1")
        self.assertEqual(session["acceptedTurnCount"], 1)
        self.assertIn("turn-1", session["completedTurns"])

    async def test_completion_serializes_concurrent_requests_and_persists_timeout_failure(self):
        class SlowAnalyzer:
            def __init__(self): self.calls = 0
            async def analyze(self, session):
                self.calls += 1
                await asyncio.sleep(.02)
                return TrustAnalysis.model_validate({"criterionScores": {"apologyAndPolicyRemedy": 15, "adaptabilityAndDeescalation": 10}, "emotionalHandling": True, "causeIdentification": False, "solutionSuitability": False, "trustRebuilding": False})
        analyzer = SlowAnalyzer()
        results = await asyncio.gather(*[complete_session("session-1", type("Req", (), {"completion_id": value, "reason": "time_limit"})(), store=self.store, analyzer=analyzer) for value in ("complete-a", "complete-b")])
        self.assertEqual(analyzer.calls, 1)
        self.assertEqual(results[0]["trustState"], results[1]["trustState"])


if __name__ == "__main__": unittest.main()
