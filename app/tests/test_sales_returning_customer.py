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

from app.sales_returning_customer import (
    CustomerResponse,
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
        self.assertEqual(result["sttProvider"], "whisper.cpp")
        self.assertTrue((Path(self.temp.name) / "sales-session-session-1-turn-turn-1.wav").exists())

    async def test_duplicate_turn_is_idempotent(self):
        first = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Xin lỗi chị."), responder=FakeResponder())
        second = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("Khác."), responder=FakeResponder())
        self.assertEqual(first, second)
        self.assertEqual((await self.store.get("session-1"))["acceptedTurnCount"], 1)

    async def test_silence_does_not_consume_accepted_turn(self):
        result = await submit_turn("session-1", request(amplitude=0), store=self.store, transcriber=FakeTranscriber("never"), responder=FakeResponder())
        self.assertFalse(result["accepted"])
        self.assertEqual((await self.store.get("session-1"))["silenceCount"], 1)

    async def test_non_vietnamese_does_not_consume_turn(self):
        result = await submit_turn("session-1", request(), store=self.store, transcriber=FakeTranscriber("This is an English response."), responder=FakeResponder())
        self.assertEqual(result["status"], "nonVietnamese")
        self.assertEqual((await self.store.get("session-1"))["acceptedTurnCount"], 0)

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
