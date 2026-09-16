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
    ModelTurnDraft,
    ReturningSessionStore,
    ReturningTurnRequest,
    TrustAnalysis,
    complete_session,
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
    }
    result.update(overrides)
    return result


def response(**overrides):
    result = {"customerText": "Chị vẫn chưa rõ nguyên nhân nên cần em hỏi thêm.", "activeObjective": 1, "objectiveCompleted": False, "disclosedFactIds": [], "conversationComplete": False, "deterministicEnding": None, "turnAssessment": assessment()}
    result.update(overrides)
    return CustomerResponse.model_validate(result)


def draft(**overrides):
    result = {"customerText": "Chị vẫn chưa rõ nguyên nhân nên cần em hỏi thêm.", "disclosedFactIds": [], "turnAssessment": assessment()}
    result.update(overrides)
    return ModelTurnDraft.model_validate(result)


class FakeAnalyzer:
    async def analyze(self, session):
        return TrustAnalysis.model_validate({"trustState": "partially_restored", "emotionalHandling": True, "causeIdentification": True, "solutionSuitability": True, "trustRebuilding": False})


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
        with patch.dict(os.environ, {"AI_THINKING_SALE_PT2": "true"}):
            result = await submit_turn(
                "session-1",
                request(),
                store=self.store,
                transcriber=FakeTranscriber("Em xin lỗi chị."),
                responder=LLMSalesResponder(service),
            )

        self.assertEqual(service.calls, 2)
        self.assertTrue(all("reasoning_effort" not in item for item in service.options))
        self.assertTrue(all(not messages[-1]["content"].endswith("/no_think") for messages in service.messages))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["customerText"], response().customer_text)

    async def test_llm_responder_disables_thinking_when_configured(self):
        class CapturingService:
            configured = True

            async def generate(self, messages, **kwargs):
                self.messages = messages
                self.options = kwargs["options"]
                return draft().model_dump_json(by_alias=True)

        service = CapturingService()
        with patch.dict(os.environ, {"AI_THINKING_SALE_PT2": "false"}):
            result = await LLMSalesResponder(service).respond(
                self.session, "Em xin lỗi chị."
            )

        self.assertEqual(service.options["reasoning_effort"], "none")
        self.assertTrue(service.messages[-1]["content"].endswith("/no_think"))
        self.assertEqual(result.customer_text, draft().customer_text)

    async def test_llm_analyzer_disables_reasoning_for_short_structured_response(self):
        class CapturingService:
            configured = True

            async def generate(self, *args, **kwargs):
                self.options = kwargs["options"]
                return TrustAnalysis.model_validate(
                    {
                        "trustState": "partially_restored",
                        "emotionalHandling": True,
                        "causeIdentification": True,
                        "solutionSuitability": True,
                        "trustRebuilding": False,
                    }
                ).model_dump_json(by_alias=True)

        service = CapturingService()
        result = await LLMSalesAnalyzer(service).analyze(self.session)

        self.assertEqual(service.options["reasoning_effort"], "none")
        self.assertEqual(result.trust_state, "partially_restored")

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
            {"customerText", "disclosedFactIds", "turnAssessment"},
        )
        self.assertGreaterEqual(phase_one_properties["customerText"]["minLength"], 20)
        self.assertLessEqual(phase_one_properties["customerText"]["maxLength"], 300)

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

    async def test_llm_responder_retries_customer_text_that_fails_domain_rules(self):
        invalid_text = "Chị muốn hoàn tiền " + "ngay " * 60
        valid_content = draft().model_dump_json(by_alias=True)

        class InvalidThenValidService:
            configured = True

            def __init__(self):
                self.calls = 0
                self.system_messages = []

            async def generate(self, messages, **kwargs):
                self.calls += 1
                self.system_messages.append(messages[0]["content"])
                if self.calls == 1:
                    return draft(customerText=invalid_text).model_dump_json(by_alias=True)
                return valid_content

        service = InvalidThenValidService()
        result = await submit_turn(
            "session-1",
            request(),
            store=self.store,
            transcriber=FakeTranscriber("Em xin lỗi chị."),
            responder=LLMSalesResponder(service),
        )

        self.assertEqual(service.calls, 2)
        self.assertIn("customerText không hợp lệ", service.system_messages[1])
        self.assertEqual(result["status"], "accepted")

    async def test_llm_responder_retries_two_word_customer_fragment(self):
        class FragmentThenSentenceService:
            configured = True

            def __init__(self):
                self.calls = 0

            async def generate(self, *args, **kwargs):
                self.calls += 1
                text = "Chị ơi" if self.calls == 1 else "Chị vẫn cần em giải thích rõ hơn."
                return draft(customerText=text).model_dump_json(by_alias=True)

        service = FragmentThenSentenceService()
        result = await submit_turn(
            "session-1",
            request(),
            store=self.store,
            transcriber=FakeTranscriber("Em xin lỗi chị."),
            responder=LLMSalesResponder(service),
        )

        self.assertEqual(service.calls, 2)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["customerText"], "Chị vẫn cần em giải thích rõ hơn.")

    async def test_llm_responder_retries_customer_text_copied_from_player(self):
        transcript = (
            "Ờ VÂNG Ạ CHỊ CHỈ CẦN MANG HÀNG MANG ĐÔI GIÀY CỦA CHỊ LẠI "
            "CÒN NGUYÊN VẸN THÌ EM SẼ GỬI CHO CHỊ NGAY"
        )
        copied_reply = (
            "Chị chỉ cần mang hàng, mang đôi giày của chị lại còn nguyên vẹn "
            "thì em sẽ gửi cho chị ngay"
        )
        actual_customer_reply = (
            "Chị hiểu rồi, nhưng em cần giải thích cách kiểm tra đôi phù hợp hơn."
        )

        class EchoThenReplyService:
            configured = True

            def __init__(self):
                self.calls = 0
                self.system_messages = []

            async def generate(self, messages, **kwargs):
                self.calls += 1
                self.system_messages.append(messages[0]["content"])
                text = copied_reply if self.calls == 1 else actual_customer_reply
                return draft(customerText=text).model_dump_json(by_alias=True)

        service = EchoThenReplyService()
        result = await submit_turn(
            "session-1",
            request(),
            store=self.store,
            transcriber=FakeTranscriber(transcript),
            responder=LLMSalesResponder(service),
        )

        self.assertEqual(service.calls, 2)
        self.assertIn("không được lặp lại", service.system_messages[1])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["customerText"], actual_customer_reply)

    async def test_duplicate_turn_is_idempotent(self):
        first = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Xin lỗi chị."), responder=FakeResponder())
        second = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Khác."), responder=FakeResponder())
        self.assertEqual(first, second)
        self.assertEqual((await self.store.get("session-1"))["acceptedTurnCount"], 1)

    async def test_silence_does_not_consume_accepted_turn(self):
        result = await submit_turn("session-1", request(amplitude=0), store=self.store, transcriber=FakeTranscriber("never"), responder=FakeResponder())
        self.assertFalse(result["accepted"])
        self.assertEqual((await self.store.get("session-1"))["silenceCount"], 1)

    async def test_language_rejection_is_removed_for_controlled_candidates(self):
        result = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("This is an English response."), responder=FakeResponder())
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["language"], "vi")
        self.assertEqual((await self.store.get("session-1"))["acceptedTurnCount"], 1)

    async def test_completion_is_idempotent_and_has_only_rubric_flags(self):
        result = await complete_session("session-1", type("Req", (), {"completion_id": "completion-1", "reason": "time_limit"})(), store=self.store, analyzer=FakeAnalyzer())
        repeated = await complete_session("session-1", type("Req", (), {"completion_id": "completion-1", "reason": "time_limit"})(), store=self.store, analyzer=FakeAnalyzer())
        self.assertEqual(result, repeated)
        self.assertEqual(result["trustState"], "partially_restored")
        self.assertNotIn("score", result)

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
                return TrustAnalysis.model_validate({"trustState": "partially_restored", "emotionalHandling": True, "causeIdentification": False, "solutionSuitability": False, "trustRebuilding": False})
        analyzer = SlowAnalyzer()
        results = await asyncio.gather(*[complete_session("session-1", type("Req", (), {"completion_id": value, "reason": "time_limit"})(), store=self.store, analyzer=analyzer) for value in ("complete-a", "complete-b")])
        self.assertEqual(analyzer.calls, 1)
        self.assertEqual(results[0]["trustState"], results[1]["trustState"])


if __name__ == "__main__": unittest.main()
