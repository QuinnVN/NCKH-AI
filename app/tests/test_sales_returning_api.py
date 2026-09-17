import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from app import main
from app.sales_returning_customer import ReturningSessionStore, submit_turn, complete_session, CompletionRequest
from app.sales_persuasion import SalesAttemptStore
from app.tests.test_sales_returning_customer import request, response, assessment, FakeTranscriber, FakeResponder, FakeAnalyzer


class ReturningApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReturningSessionStore(Path(self.temp.name))
        self.attempts = SalesAttemptStore(Path(self.temp.name))
        self.patches = [patch.object(main, "sales_returning_store", self.store),
            patch.object(main, "sales_attempt_store", self.attempts),
            patch.object(main, "sales_returning_transcriber", FakeTranscriber("Em xin lỗi chị.")),
            patch.object(main, "LLMSalesResponder", return_value=FakeResponder()),
            patch.dict(os.environ, {"BACKEND_API_TOKEN": "gameplay-test", "SALES_DIAGNOSTIC_TOKEN": "diagnostic-test"})]
        for item in self.patches: item.start()
        self.client = AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")
        self.auth = {"Authorization": "Bearer gameplay-test"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    async def test_routes_match_unity_wire_and_hide_diagnostic_audio(self):
        denied = await self.client.post("/api/sales/sessions", json={"sessionId": "wire-session"})
        self.assertEqual(denied.status_code, 401)
        created = await self.client.post("/api/sales/sessions", headers=self.auth, json={"sessionId": "wire-session"})
        self.assertEqual(created.status_code, 200)
        linked = await self.client.post("/api/sales/sessions", headers=self.auth, json={"sessionId": "wire-session", "part1AttemptId": "p1"})
        self.assertEqual(linked.json()["part1AttemptId"], "p1")
        turn = await self.client.post("/api/sales/sessions/wire-session/turns", headers=self.auth,
            json=request().model_dump(by_alias=True))
        self.assertEqual(turn.status_code, 200)
        self.assertEqual(turn.json()["status"], "accepted")
        state = await self.client.get("/api/sales/sessions/wire-session", headers=self.auth)
        self.assertEqual(state.json()["activeObjective"], 1)
        self.assertNotIn("completedTurns", state.json())
        self.assertNotIn("dataBase64", turn.text + state.text)
        self.assertNotIn(self.temp.name, turn.text + state.text)
        forbidden = await self.client.delete("/api/sales/sessions/wire-session/diagnostics", headers=self.auth)
        self.assertEqual(forbidden.status_code, 401)
        deleted = await self.client.delete("/api/sales/sessions/wire-session/diagnostics", headers={"Authorization": "Bearer diagnostic-test"})
        self.assertEqual(deleted.status_code, 200)
        retried = await self.client.post("/api/sales/sessions/wire-session/turns", headers=self.auth, json=request().model_dump(by_alias=True))
        self.assertEqual(retried.status_code, 409)

    async def test_diagnostic_token_is_required_even_when_gameplay_auth_is_disabled(self):
        await self.store.create_or_resume("private")
        with patch.dict(os.environ, {"BACKEND_API_TOKEN": ""}):
            denied = await self.client.delete("/api/sales/sessions/private/diagnostics")
        self.assertEqual(denied.status_code, 401)

    async def test_linked_part1_retention_scrubs_audio_and_transcript(self):
        import json
        record = {"attemptId": "old", "salesSessionId": "expired", "createdAtUtc": "2020-01-01T00:00:00Z", "transcript": "Lời nói riêng tư"}
        path = Path(self.temp.name) / "sales-persuasion-old.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        audio = self.attempts.audio_path("old")
        audio.write_bytes(b"wav")
        self.assertEqual(await self.attempts.purge_expired_session_diagnostics(), 1)
        self.assertFalse(audio.exists())
        self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["transcript"])

    async def test_scripted_conversation_obeys_four_turn_limit_and_mandatory_challenge(self):
        from app.sales_returning_customer import MANDATORY_CHALLENGE, TrustAnalysis
        await self.store.create_or_resume("scripted")
        replies = [
            response(activeObjective=2, objectiveCompleted=True, turnAssessment=assessment(emotionalAcknowledgment=True, openQuestion=True)),
            response(activeObjective=2, disclosedFactIds=["walking_routine", "fit_condition", "lighter_preference"], turnAssessment=assessment(useOrDurationQuestion=True, fitConditionOrPreferenceQuestion=True)),
            response(activeObjective=3, objectiveCompleted=True, turnAssessment=assessment(causeStatement=True)),
            response(activeObjective=4, objectiveCompleted=True, turnAssessment=assessment(policyExchange=True, lightweightForWalking=True, fitOrWalkTrial=True)),
        ]
        class ScriptedResponder:
            async def respond(self, session, transcript): return replies.pop(0)
        responder = ScriptedResponder()
        for index in range(4):
            turn = await submit_turn("scripted", request("turn-" + str(index)), store=self.store,
                transcriber=FakeTranscriber("Em xin lỗi chị."), responder=responder)
            self.assertEqual(turn["status"], "accepted")
            if index == 3: self.assertEqual(turn["customerText"], MANDATORY_CHALLENGE)
        class RestoredAnalyzer:
            async def analyze(self, session):
                return TrustAnalysis(criterionScores={"apologyAndPolicyRemedy": 45, "adaptabilityAndDeescalation": 40}, emotionalHandling=True, causeIdentification=True, solutionSuitability=True, trustRebuilding=True)
        result = await complete_session("scripted", CompletionRequest(completionId="final"), store=self.store, analyzer=RestoredAnalyzer())
        self.assertEqual(result["trustState"], "restored")
        self.assertEqual(result["score"], 85)
        self.assertEqual(result["customerRating"], "good")
        self.assertEqual(result["acceptedTurnCount"], 4)

    async def test_terminal_turn_retry_remains_idempotent(self):
        await self.store.create_or_resume("terminal")
        class EndingResponder:
            async def respond(self, session, text):
                return response(conversationComplete=True, deterministicEnding="manager_escalation", turnAssessment=assessment(managerEscalation=True))
        first = await submit_turn("terminal", request(), store=self.store, transcriber=FakeTranscriber("Em nhờ người quản lý giúp chị."), responder=EndingResponder())
        repeat = await submit_turn("terminal", request(), store=self.store, transcriber=FakeTranscriber("unused"), responder=FakeResponder())
        self.assertEqual(first, repeat)
        self.assertEqual((await self.store.get("terminal"))["acceptedTurnCount"], 1)

    async def test_policy_mention_is_not_manager_escalation(self):
        await self.store.create_or_resume("policy")
        turn = await submit_turn("policy", request(), store=self.store,
            transcriber=FakeTranscriber("Em không gọi quản lý, em sẽ tự xử lý cho chị."), responder=FakeResponder())
        self.assertEqual(turn["status"], "accepted")
        self.assertIsNone((await self.store.get("policy"))["trustState"])

    async def test_delete_during_transcription_cannot_restore_transcript(self):
        await self.store.create_or_resume("race")
        started, proceed = asyncio.Event(), asyncio.Event()
        class WaitingTranscriber:
            async def transcribe(self, path):
                started.set()
                await proceed.wait()
                return "Em xin lỗi chị."
        task = asyncio.create_task(submit_turn("race", request(), store=self.store, transcriber=WaitingTranscriber(), responder=FakeResponder()))
        await started.wait()
        await self.store.delete_diagnostics("race")
        proceed.set()
        with self.assertRaises(RuntimeError): await task
        state = await self.store.get("race")
        self.assertEqual(state["turns"], [])
        self.assertEqual(state["completedTurns"], {})

    async def test_session_id_containing_turn_marker_does_not_delete_another_session(self):
        for sid in ("a", "a-turn-b"):
            await self.store.create_or_resume(sid)
            await submit_turn(sid, request(), store=self.store, transcriber=FakeTranscriber("Em xin lỗi chị."), responder=FakeResponder())
        await self.store.delete_diagnostics("a")
        self.assertTrue(self.store._turn_path("a-turn-b", "turn-1").exists())

    async def test_new_completion_id_returns_existing_trust_state(self):
        await self.store.create_or_resume("final")
        first = await complete_session("final", CompletionRequest(completionId="first", reason="time_limit"), store=self.store, analyzer=FakeAnalyzer())
        class MustNotRun:
            async def analyze(self, session): raise AssertionError("final analysis reran")
        repeat = await complete_session("final", CompletionRequest(completionId="second", reason="time_limit"), store=self.store, analyzer=MustNotRun())
        self.assertEqual(first["trustState"], repeat["trustState"])
