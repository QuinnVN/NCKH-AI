import asyncio
import base64
import io
import tempfile
import unittest
import wave
import os
import time
from unittest.mock import patch
from pathlib import Path

from app.llm_service import LLMServiceError
from app.sales_returning_customer import (
    CustomerResponse,
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
    result = {"playerResponseRating": "good", "disclosedFactIds": [], "turnAssessment": assessment()}
    result.update(overrides)
    return ModelTurnDraft.model_validate(result)


class FakeAnalyzer:
    async def analyze(self, session):
        return TrustAnalysis.model_validate({"criterionScores": {"apologyAndPolicyRemedy": 35, "adaptabilityAndDeescalation": 20}, "emotionalHandling": True, "causeIdentification": True, "solutionSuitability": True, "trustRebuilding": False})


class ReturningCustomerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
        self.assertEqual(result["customerText"], "Chính sách đổi trả của tiệm là như thế nào? Có cho chị hoàn tiền không?")

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
        self.assertIn("không được tự viết lời thoại", service.messages[0]["content"])
        self.assertIn("biện pháp khắc phục", service.messages[0]["content"])
        self.assertEqual(result.customer_text, "Chính sách đổi trả của tiệm là như thế nào? Có cho chị hoàn tiền không?")

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
            {"playerResponseRating", "disclosedFactIds", "turnAssessment"},
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
            self.session, "Em xin lỗi chị."
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
        self.assertEqual(result["customerText"], "Chính sách đổi trả của tiệm là như thế nào? Có cho chị hoàn tiền không?")

    async def test_second_bad_without_return_policy_requests_policy(self):
        class BadService:
            configured = True

            async def generate(self, *args, **kwargs):
                return draft(playerResponseRating="bad", turnAssessment=assessment(apology=False, remedy=False)).model_dump_json(by_alias=True)

        responder = LLMSalesResponder(BadService())
        first = await submit_turn("session-1", request("bad-1"), store=self.store, transcriber=FakeTranscriber("Em xin lỗi chị."), responder=responder)
        second = await submit_turn("session-1", request("bad-2"), store=self.store, transcriber=FakeTranscriber("Em chỉ xin lỗi chị."), responder=responder)

        self.assertIn(first["customerText"], (
            "Lần trước em thuyết phục đôi này hợp chị nhất, giờ nó thành ra thế này thì sao chị tin em được nữa?",
            "Lỡ như giờ em lừa chị tiếp thì sao chị tin hả em?",
        ))
        self.assertEqual(second["customerText"], "Tiệm mấy người làm ăn kiểu gì kì cục vậy, nhân viên thì không biết tư vấn. Em trả lời cho chị chính sách đền bù, đừng có dài dòng.")
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
        self.assertIn(result.customer_text, (
            "Chính sách đổi trả của tiệm là như thế nào? Có cho chị hoàn tiền không?",
            "Nếu lần tiếp đến cũng như thế thì em giải quyết thế nào?",
            "Tư vấn cho chị vài mẫu mã khác đi. Tư vấn đàng hoàng đấy nhé.",
            "Chị có được nhận voucher đền bù hay giảm giá mua tiếp theo không em?",
        ))

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
        self.assertEqual(result["customerText"], "Nhân viên nói chuyện kiểu đó với khách hàng đấy hả? Có tin tôi đánh giá xấu không?")
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
        self.assertIn(result["finalCustomerText"], (
            "Được, lần sau chị vẫn sẽ đến.",
            "Ok, chị sẽ về suy nghĩ thêm về việc mua tiếp.",
        ))

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
