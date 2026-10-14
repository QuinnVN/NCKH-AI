from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import wave

from app.run_results import RunResultStore
from app.sales_concerns import RETRACTIONS
from app.sales_openrouter import QUESTION_TEXT
from app.sales_returning_customer import ENDING_VARIANTS
from app.sales_rubric import VERSIONS as SALES_VERSIONS
from scripts.repair_simulation_results import (
    _draft_display_date,
    _interactive_reevaluation,
    _reevaluation_candidates,
    SalesPipeline,
    apply_plan,
    main,
    plan_maintenance,
    reevaluate_draft,
)


class FakeAdmin:
    def command(self, name: str) -> dict:
        if name != "ping":
            raise ValueError(name)
        return {"ok": 1}


class FakeClient:
    admin = FakeAdmin()


class FakeMongo:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.documents: dict[str, dict] = {}
        self.client = FakeClient()
        self.indexes_ensured = False

    def ensure_indexes(self) -> None:
        self.indexes_ensured = True

    def upsert(self, aggregate) -> None:
        run_id = aggregate["runId"]
        if run_id in self.failures:
            raise RuntimeError("mongodb://user:secret@example.invalid upload failed")
        self.documents[run_id] = deepcopy(dict(aggregate))


class FakeLLMService:
    configured = True
    model = "test-model"

    async def generate(self, messages, *, options=None, max_message_chars=None) -> str:
        schema_name = options["response_format"]["json_schema"]["name"]
        if schema_name == "lawyer_defense_assessment":
            return json.dumps({
                "evidence_use": 30,
                "logical_connections": 25,
                "conclusion_fidelity": 12,
                "clarity_and_persuasiveness": 8,
                "feedback_vi": "Lập luận rõ ràng.",
            })
        if schema_name == "sales_part1_rubric":
            return json.dumps({
                "recommendsShoe": {"status": "true", "evidence": "Em đề xuất Giày A"},
                "needsMatch": {"status": "true", "evidence": "nhẹ và phù hợp chạy bộ"},
                "productFacts": {"status": "true", "evidence": "nhẹ"},
                "objectionResponse": {"status": "false", "evidence": ""},
                "nextStep": {"status": "false", "evidence": ""},
                "falseProductClaim": {"status": "false", "evidence": ""},
                "disrespectOrPressure": {"status": "false", "evidence": ""},
            }, ensure_ascii=False)
        if schema_name == "sales_conversation_analysis":
            return json.dumps({
                "criterionScores": {
                    "apologyAndPolicyRemedy": 40,
                    "adaptabilityAndDeescalation": 35,
                },
                "emotionalHandling": True,
                "causeIdentification": True,
                "solutionSuitability": True,
                "trustRebuilding": True,
            })
        raise AssertionError(schema_name)


def lawyer_payload(transcript: str = "Bào chữa hợp lệ.") -> dict:
    return {
        "roundId": "round-1",
        "caseId": "case-1",
        "interviewRestartCount": 0,
        "finalClueSet": [],
        "completedEvidenceLinks": [],
        "transcript": transcript,
        "criterionScores": {
            "evidenceUse": 20,
            "logicalConnections": 20,
            "conclusionFidelity": 10,
            "clarityAndPersuasiveness": 10,
        },
        "rawScore": 60,
        "restartPenaltyPercent": 0,
        "finalScore": 60,
        "feedbackVi": "Hợp lệ.",
        "recordingAtUtc": "2026-09-16T01:00:00Z",
        "assessmentCompletedAtUtc": "2026-09-16T01:01:00Z",
        "completionStatus": "completed",
    }


def draft(run_id: str, participant: str = "Participant", *, status: str = "aborted", game_id: str = "lawyer") -> dict:
    legacy = lawyer_payload()
    return {
        "runId": run_id,
        "participantName": participant,
        "participantSessionId": "session-1",
        "gameId": game_id,
        "status": status,
        "startedAtUtc": "2026-09-16T00:00:00Z",
        "updatedAtUtc": "2026-09-16T01:02:00Z",
        "data": {**legacy, "lawyer": {"roundId": "round-1", "transcript": ""}},
        "fragments": {"fragment-1": {"kind": "lawyer.defense_completed"}},
        "requiredFields": ["lawyer"],
        "completionRequested": True,
    }


def aggregate(run_id: str, participant: str = "Participant", *, legacy: bool = False) -> dict:
    payload = lawyer_payload()
    data = {**payload, "lawyer": {"roundId": "round-1", "transcript": ""}} if legacy else {"lawyer": payload}
    return {
        "_id": run_id,
        "runId": run_id,
        "participantName": participant,
        "participantSessionId": "session-1",
        "gameId": "lawyer",
        "status": "completed",
        "startedAtUtc": "2026-09-16T00:00:00Z",
        "completedAtUtc": "2026-09-16T01:01:00Z",
        "data": data,
    }


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes((4000).to_bytes(2, "little", signed=True) * 1600)


class FakeSpeech:
    def __init__(self) -> None:
        self.closed = False

    async def initialize(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


class RepairSimulationResultsTests(unittest.TestCase):
    def test_interactive_reevaluation_uses_detached_llm_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "simulation-results"
            results.mkdir()
            session_id = "b" * 32
            record = draft("lawyer-detached")
            write_json(results / "run-lawyer-detached.draft.json", record)
            write_json(
                root / f"lawyer-defense-{session_id}.json",
                {"roundId": session_id, "transcript": "Lời bào chữa."},
            )
            answers = iter(("1", session_id, "y"))

            with (
                patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "false"}),
                patch(
                    "scripts.repair_simulation_results._llm_endpoint_reachable",
                    return_value=False,
                ),
                patch(
                    "scripts.repair_simulation_results.AIServerManager.start_detached",
                    new=AsyncMock(return_value=("llama",)),
                ) as start_detached,
                patch(
                    "scripts.repair_simulation_results.reevaluate_draft",
                    new=AsyncMock(return_value=record),
                ),
            ):
                completed = _interactive_reevaluation(
                    results,
                    root,
                    input_fn=lambda _: next(answers),
                )

            self.assertTrue(completed)
            start_detached.assert_awaited_once_with("llama")

    def test_reevaluation_candidates_are_newest_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            oldest = draft("oldest")
            oldest["updatedAtUtc"] = "2026-09-16T08:00:00Z"
            middle = draft("middle")
            middle.pop("updatedAtUtc")
            middle["startedAtUtc"] = "2026-09-17T08:00:00Z"
            newest = draft("newest")
            newest["updatedAtUtc"] = "2026-09-18T08:00:00Z"
            write_json(directory / "run-oldest.draft.json", oldest)
            write_json(directory / "run-middle.draft.json", middle)
            write_json(directory / "run-newest.draft.json", newest)

            candidates = _reevaluation_candidates(directory)

            self.assertEqual(
                [path.name for path, _ in candidates],
                [
                    "run-newest.draft.json",
                    "run-middle.draft.json",
                    "run-oldest.draft.json",
                ],
            )

            output = io.StringIO()
            with redirect_stdout(output):
                selected = _interactive_reevaluation(
                    directory,
                    directory,
                    input_fn=lambda _: "",
                )

            self.assertFalse(selected)
            listing = output.getvalue()
            expected_date = _draft_display_date(
                directory / "run-newest.draft.json", newest
            )
            self.assertIn(
                f"run-newest.draft.json [{expected_date}] (lawyer, Participant)",
                listing,
            )

    def test_reevaluates_lawyer_transcript_from_recording_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            recordings = Path(temporary)
            session_id = "a" * 32
            record = {
                "roundId": session_id,
                "caseId": "case-1",
                "interviewRestartCount": 0,
                "assessmentContext": {
                    "caseSummary": "Tóm tắt",
                    "investigationObjective": "Mục tiêu",
                    "defenseConclusion": "Không đủ chứng cứ",
                    "orderedEvidence": [],
                    "reasoningCards": [],
                    "sampleAnswers": [],
                },
                "transcript": "Lời bào chữa đầy đủ.",
                "createdAtUtc": "2026-09-16T01:00:00Z",
            }
            write_json(recordings / f"lawyer-defense-{session_id}.json", record)
            original = draft("lawyer-run")
            original["data"] = {}
            original["requiredFields"] = []
            original["completionRequested"] = False

            repaired = asyncio.run(
                reevaluate_draft(original, recordings, session_id, FakeLLMService())
            )

            result = repaired["data"]["lawyer"]
            self.assertEqual(result["roundId"], session_id)
            self.assertEqual(result["transcript"], "Lời bào chữa đầy đủ.")
            self.assertEqual(result["rawScore"], 75)
            self.assertEqual(result["finalScore"], 75)
            self.assertEqual(repaired["requiredFields"], ["lawyer"])
            self.assertTrue(repaired["completionRequested"])

    def test_reevaluates_current_part1_and_legacy_part2_from_session_transcripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            recordings = Path(temporary)
            session_id = "session-1"
            attempt_id = "attempt-1"
            sales_session = {
                "sessionId": session_id,
                "pipelineMode": "legacy",
                "part1AttemptId": attempt_id,
                "turns": [{
                    "turnId": "turn-1",
                    "transcript": "Em xin lỗi và sẽ kiểm tra đôi giày.",
                    "customerText": ENDING_VARIANTS["good"][0],
                    "playerResponseRating": "good",
                    "policyViolations": [],
                }],
                "policyViolations": [],
                "acceptedTurnCount": 1,
                "goodResponseCount": 1,
                "badResponseCount": 0,
                "completionReason": "completed",
                "updatedAtUtc": "2026-09-16T02:00:00Z",
            }
            part1 = {
                "attemptId": attempt_id,
                "selectedShoeId": "shoe-1",
                "bestFitShoeId": "shoe-1",
                "customerNeeds": "Cần giày chạy bộ.",
                "objection": "Mẫu còn đơn giản.",
                "availableShoes": [{"shoeId": "shoe-1", "name": "Giày A", "price": 1200000,
                                    "details": "Nhẹ, phù hợp chạy bộ."}],
                "transcript": "Em đề xuất Giày A vì nhẹ và phù hợp chạy bộ.",
                "createdAtUtc": "2026-09-16T01:00:00Z",
            }
            write_json(recordings / f"sales-session-{session_id}.json", sales_session)
            write_json(recordings / f"sales-persuasion-{attempt_id}.json", part1)
            original = draft("sale-run", game_id="sale")
            original["data"] = {}
            original["requiredFields"] = []
            original["completionRequested"] = False

            with patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "false"}):
                repaired = asyncio.run(
                    reevaluate_draft(original, recordings, session_id, FakeLLMService())
                )

            self.assertEqual(repaired["data"]["part1"]["score"], 60)
            self.assertTrue(repaired["data"]["part1"]["feedbackVi"])
            self.assertEqual(repaired["data"]["part2"]["score"], 75)
            self.assertEqual(repaired["data"]["part2"]["customerRating"], "good")
            self.assertIn(repaired["data"]["part2"]["finalCustomerText"], ENDING_VARIANTS["good"])
            self.assertNotEqual(repaired["data"]["part2"]["finalCustomerText"], ENDING_VARIANTS["good"][0])
            self.assertEqual(repaired["data"]["turns"][0]["turnId"], "turn-1")
            self.assertEqual(repaired["requiredFields"], ["part1", "part2"])

    def _openrouter_sale_fixture(self, recordings: Path) -> tuple[str, dict]:
        session_id = "c" * 32
        attempt_id = "attempt-or"
        session = {
            "sessionId": session_id, "runId": "sale-run", "part1AttemptId": attempt_id,
            "pipelineMode": "openrouter", "maxTurns": 8, "status": "awaitingCompletion",
            "assessmentStatus": "needs-review", "endingReason": "stalemate",
            "assessmentReviewItems": [{"turnId": "turn-1", "objective": 1, "labels": ["openQuestion"]}],
            "evidenceLedger": [], "policyViolations": [], "silenceCount": 0,
            "acceptedTurnCount": 2, "goodResponseCount": 0, "badResponseCount": 0,
            **{name: "old-v1" for name in SALES_VERSIONS},
            "turns": [
                {"turnId": "turn-1", "accepted": True, "transcript": "Em xin lỗi chị.",
                 "customerText": "Chị vẫn đau chân."},
                {"turnId": "turn-2", "accepted": True, "transcript": "Chị đi bao lâu thì đau ạ?",
                 "customerText": "Chị đi mới ba ngày."},
            ],
        }
        part1 = {
            "attemptId": attempt_id, "salesSessionId": session_id, "runId": "sale-run",
            "customerId": "Lan", "selectedShoeId": "shoe-1", "bestFitShoeId": "shoe-1",
            "customerNeeds": "Cần giày chạy bộ.", "objection": "Mẫu còn đơn giản.",
            "availableShoes": [{"shoeId": "shoe-1", "name": "Giày A", "price": 1200000,
                                "details": "Nhẹ, phù hợp chạy bộ."}],
            "transcript": "Em đề xuất Giày A vì nhẹ và phù hợp chạy bộ.",
            "createdAtUtc": "2026-09-16T01:00:00Z",
        }
        write_json(recordings / f"sales-session-{session_id}.json", session)
        write_json(recordings / f"sales-persuasion-{attempt_id}.json", part1)
        original = draft("sale-run", game_id="sale")
        original["data"] = {}
        original["requiredFields"] = []
        return session_id, original

    def test_needs_review_sale_rerun_replays_audio_through_full_pipeline(self):
        class FakeTranscriber:
            def __init__(self, texts):
                self.texts = texts

            async def transcribe(self, path):
                return next(text for turn_id, text in self.texts.items()
                            if Path(path).stem.endswith("-turn-" + turn_id))

        class FakeJev:
            def __init__(self):
                self.transcripts = []

            async def classify(self, transcript, context):
                self.transcripts.append(transcript)
                self.versions = {name: context.get(name) for name in SALES_VERSIONS}
                return {"labels": {name: {"noul": 0.95 if name == "acknowledgment" else 0.0}
                                   for name in (*QUESTION_TEXT, *RETRACTIONS)}}

        class FakeLuna:
            async def adjudicate(self, transcript, context, requested):
                return {"labels": {}, "metadata": {}}

        class FakeWriter:
            def __init__(self):
                self.calls = 0

            async def write(self, plan, transcript, history):
                self.calls += 1
                return {"customerText": plan["fallbackText"], "metadata": {}, "fallbackUsed": False}

        with tempfile.TemporaryDirectory() as temporary:
            recordings = Path(temporary)
            session_id, original = self._openrouter_sale_fixture(recordings)
            for turn_id in ("turn-1", "turn-2"):
                write_wav(recordings / f"sales-session-{session_id}-turn-{turn_id}.wav")
            transcriber = FakeTranscriber({"turn-1": "STT một", "turn-2": "STT hai"})
            jev, writer = FakeJev(), FakeWriter()
            session_path = recordings / f"sales-session-{session_id}.json"
            recorded = session_path.read_bytes()

            with patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "false", "PIPELINE_MODE": "openrouter"}):
                repaired = asyncio.run(reevaluate_draft(
                    original, recordings, session_id, FakeLLMService(),
                    pipeline=SalesPipeline(transcriber, jev, writer, FakeLuna()),
                ))

            part2 = repaired["data"]["part2"]
            self.assertEqual(jev.transcripts, ["STT một", "STT hai"])
            self.assertEqual(writer.calls, 2)
            self.assertEqual(jev.versions, SALES_VERSIONS)
            self.assertEqual([turn["transcript"] for turn in repaired["data"]["turns"]], ["STT một", "STT hai"])
            self.assertEqual(part2["rubricVersion"], SALES_VERSIONS["rubricVersion"])
            self.assertEqual(part2["assessmentStatus"], "completed")
            self.assertEqual(part2["repair"]["method"], "full_pipeline_rerun")
            self.assertEqual(part2["repair"]["recordedVersions"]["rubricVersion"], "old-v1")
            self.assertEqual(part2["repair"]["turnsReplayed"], 2)
            self.assertTrue(part2["emotionalHandling"])
            self.assertTrue(RunResultStore._ready_to_finalize(repaired))
            self.assertEqual(session_path.read_bytes(), recorded)

    def test_needs_review_sale_rerun_requires_stored_turn_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            recordings = Path(temporary)
            session_id, original = self._openrouter_sale_fixture(recordings)

            with (
                patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "false", "PIPELINE_MODE": "openrouter"}),
                self.assertRaisesRegex(FileNotFoundError, "turn-1"),
            ):
                asyncio.run(reevaluate_draft(
                    original, recordings, session_id, FakeLLMService(),
                    pipeline=SalesPipeline(transcriber=object()),
                ))

    def test_needs_review_sale_without_rerun_replays_recorded_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            recordings = Path(temporary)
            session_id, original = self._openrouter_sale_fixture(recordings)

            with patch.dict(os.environ, {"LLM_USE_AMD_HYBRID": "false"}):
                repaired = asyncio.run(reevaluate_draft(original, recordings, session_id, FakeLLMService()))

            part2 = repaired["data"]["part2"]
            self.assertEqual(part2["rubricVersion"], "old-v1")
            self.assertEqual(part2["assessmentStatus"], "completed")
            self.assertEqual(part2["repair"]["method"], "ledger_replay")
            self.assertEqual(part2["repair"]["unresolvedReviewItems"], 1)
            self.assertTrue(RunResultStore._ready_to_finalize(repaired))

    def test_interactive_reevaluation_offers_pipeline_rerun_for_needs_review_sale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "simulation-results"
            results.mkdir()
            session_id, original = self._openrouter_sale_fixture(root)
            write_json(results / "run-sale-run.draft.json", original)
            answers = iter(("1", session_id, "", "y"))
            prompts = []

            def answer(prompt):
                prompts.append(prompt)
                return next(answers)

            transcriber = FakeSpeech()

            with (
                patch("scripts.repair_simulation_results._llm_endpoint_reachable", return_value=True),
                patch(
                    "scripts.repair_simulation_results.reevaluate_draft",
                    new=AsyncMock(return_value=original),
                ) as reevaluate,
                redirect_stdout(io.StringIO()) as output,
            ):
                completed = _interactive_reevaluation(
                    results, root, input_fn=answer, transcriber_factory=lambda: transcriber,
                )

            self.assertTrue(completed)
            self.assertIn("STT, Jev, Luna, writer", prompts[2])
            self.assertIn("rubricVersion: old-v1 -> " + SALES_VERSIONS["rubricVersion"], output.getvalue())
            self.assertIs(reevaluate.await_args.kwargs["pipeline"].transcriber, transcriber)
            self.assertTrue(transcriber.closed)

    def test_interactive_reevaluation_does_not_offer_rerun_without_needs_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "simulation-results"
            results.mkdir()
            session_id, original = self._openrouter_sale_fixture(root)
            session_path = root / f"sales-session-{session_id}.json"
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session.update(assessmentStatus="pending", assessmentReviewItems=[])
            write_json(session_path, session)
            write_json(results / "run-sale-run.draft.json", original)
            answers = iter(("1", session_id, "y"))

            with (
                patch("scripts.repair_simulation_results._llm_endpoint_reachable", return_value=True),
                patch(
                    "scripts.repair_simulation_results.reevaluate_draft",
                    new=AsyncMock(return_value=original),
                ) as reevaluate,
                redirect_stdout(io.StringIO()),
            ):
                completed = _interactive_reevaluation(results, root, input_fn=lambda _: next(answers))

            self.assertTrue(completed)
            self.assertIsNone(reevaluate.await_args.kwargs["pipeline"])

    def test_dry_run_reports_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "run-run-1.draft.json"
            original = draft("run-1")
            write_json(path, original)

            summary = plan_maintenance(directory).summary

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertFalse((directory / "run-run-1.json").exists())
            self.assertEqual(summary.finalized_runs, 1)
            self.assertEqual(summary.upload_ready, 1)

    def test_repairs_legacy_lawyer_and_finalizes_aborted_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = draft("run-2", game_id="completed")
            write_json(directory / "run-run-2.draft.json", record)
            mongo = FakeMongo()

            summary = apply_plan(plan_maintenance(directory), mongo)

            repaired = json.loads((directory / "run-run-2.draft.json").read_text(encoding="utf-8"))
            completed = json.loads((directory / "run-run-2.json").read_text(encoding="utf-8"))
            self.assertEqual(repaired["gameId"], "lawyer")
            self.assertEqual(repaired["status"], "finalized")
            self.assertNotIn("transcript", repaired["data"])
            self.assertEqual(repaired["data"]["lawyer"]["transcript"], "Bào chữa hợp lệ.")
            self.assertEqual(completed["completedAtUtc"], "2026-09-16T01:01:00Z")
            self.assertEqual(mongo.documents["run-2"], completed)
            self.assertEqual(summary.uploaded, 1)

    def test_repairs_completed_noncanonical_aggregate(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = aggregate("run-3", legacy=True)
            del record["completedAtUtc"]
            del record["_id"]
            write_json(directory / "run-run-3.json", record)
            mongo = FakeMongo()

            summary = apply_plan(plan_maintenance(directory), mongo)

            repaired = json.loads((directory / "run-run-3.json").read_text(encoding="utf-8"))
            self.assertNotIn("criterionScores", repaired["data"])
            self.assertEqual(repaired["_id"], "run-3")
            self.assertEqual(repaired["data"]["lawyer"]["finalScore"], 60)
            self.assertEqual(repaired["completedAtUtc"], "2026-09-16T01:01:00Z")
            self.assertEqual(summary.repaired_files, 1)
            self.assertEqual(summary.uploaded, 1)

    def test_incomplete_aborted_run_remains_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = draft("run-4")
            record["data"]["completionStatus"] = "processing"
            record["data"]["lawyer"] = {"roundId": "round-1", "transcript": ""}
            path = directory / "run-run-4.draft.json"
            write_json(path, record)

            summary = apply_plan(plan_maintenance(directory), FakeMongo())

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
            self.assertFalse((directory / "run-run-4.json").exists())
            self.assertEqual(summary.finalized_runs, 0)

    def test_warns_when_finalized_sales_draft_is_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = {
                "runId": "sale-omitted", "participantName": "Participant",
                "participantSessionId": "session-1", "gameId": "sale", "status": "finalized",
                "startedAtUtc": "2026-09-16T00:00:00Z", "updatedAtUtc": "2026-09-16T00:03:00Z",
                "completionRequested": True, "requiredFields": ["part1", "part2"],
                "fragments": {"completion": {"kind": "sales.part2.completed"}},
                "data": {
                    "part1": {
                        "attemptId": "attempt-1", "selectedShoeId": "shoe-1", "bestFitShoeId": "shoe-1",
                        "transcript": "Tư vấn", "score": 80, "feedbackVi": "Tốt",
                        "recordingAtUtc": "2026-09-16T00:01:00Z",
                        "assessmentCompletedAtUtc": "2026-09-16T00:02:00Z",
                    },
                    "turns": [{"turnId": "turn-1", "transcript": "Xin lỗi"}],
                    "part2": {
                        "score": 0, "customerRating": "bad",
                        "criterionScores": {"apologyAndPolicyRemedy": 30, "adaptabilityAndDeescalation": 40},
                        "trustState": "lost", "completionReason": "manager_escalation",
                        "emotionalHandling": True, "causeIdentification": True,
                        "solutionSuitability": False, "trustRebuilding": False,
                        "acceptedTurnCount": 1,
                    },
                },
            }
            write_json(directory / "run-sale-omitted.draft.json", record)

            summary = plan_maintenance(directory).summary

            self.assertEqual(summary.upload_ready, 0)
            self.assertIn(
                "WARNING run-sale-omitted.draft.json needs re-evaluation: "
                "sales part 2 score (0) does not match penalty-adjusted "
                "score (70 - 0 = 70).",
                summary.actions,
            )

            record["data"]["part2"].update(
                rawScore=70,
                policyViolationPenalty=10,
                score=60,
            )
            write_json(directory / "run-sale-omitted.draft.json", record)

            adjusted = plan_maintenance(directory).summary

            self.assertEqual(adjusted.upload_ready, 1)
            self.assertFalse(any("needs re-evaluation" in action for action in adjusted.actions))

    def test_dry_run_warns_when_aborted_lawyer_draft_needs_reevaluation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = draft("lawyer-incomplete")
            record["completionRequested"] = False
            record["data"] = {"lawyer": {"roundId": "round-1", "transcript": ""}}
            write_json(directory / "run-lawyer-incomplete.draft.json", record)

            summary = plan_maintenance(directory).summary

            self.assertIn(
                "WARNING run-lawyer-incomplete.draft.json needs re-evaluation: "
                "completion was not requested.",
                summary.actions,
            )

    def test_dry_run_does_not_warn_when_completed_aggregate_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = draft("lawyer-published", status="finalized")
            record["completionRequested"] = False
            write_json(directory / "run-lawyer-published.draft.json", record)
            write_json(directory / "run-lawyer-published.json", aggregate("lawyer-published"))

            summary = plan_maintenance(directory).summary

            self.assertFalse(any("needs re-evaluation" in action for action in summary.actions))

    def test_completed_sales_result_allows_penalty_adjusted_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            record = {
                "_id": "sale-1", "runId": "sale-1", "participantName": "Participant",
                "participantSessionId": "session-1", "gameId": "sale", "status": "completed",
                "startedAtUtc": "2026-09-16T00:00:00Z",
                "data": {
                    "part1": {
                        "attemptId": "attempt-1", "selectedShoeId": "shoe-1", "bestFitShoeId": "shoe-1",
                        "transcript": "Tư vấn", "score": 80, "feedbackVi": "Tốt",
                        "recordingAtUtc": "2026-09-16T00:01:00Z",
                        "assessmentCompletedAtUtc": "2026-09-16T00:02:00Z",
                    },
                    "turns": [{"turnId": "turn-1", "transcript": "Xin lỗi"}],
                    "part2": {
                        "score": 0, "customerRating": "bad",
                        "criterionScores": {"apologyAndPolicyRemedy": 16, "adaptabilityAndDeescalation": 14},
                        "trustState": "lost", "completionReason": "manager_escalation",
                        "emotionalHandling": True, "causeIdentification": True,
                        "solutionSuitability": False, "trustRebuilding": False,
                        "acceptedTurnCount": 1, "completedAtUtc": "2026-09-16T00:03:00Z",
                    },
                },
            }
            write_json(directory / "run-sale-1.json", record)
            mongo = FakeMongo()

            summary = apply_plan(plan_maintenance(directory), mongo)

            repaired = json.loads((directory / "run-sale-1.json").read_text(encoding="utf-8"))
            self.assertEqual(repaired["completedAtUtc"], "2026-09-16T00:03:00Z")
            self.assertIn("sale-1", mongo.documents)
            self.assertEqual(summary.invalid_files, 0)

    def test_test_run_deletes_all_local_artifacts_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            run_id = "test-run"
            write_json(directory / f"run-{run_id}.draft.json", draft(run_id, "  TeSt  ", status="finalized"))
            write_json(directory / f"run-{run_id}.json", aggregate(run_id, "test"))
            write_json(directory / f"run-{run_id}.sync.json", {"runId": run_id, "synchronized": True})
            write_json(directory / "run-tester.json", aggregate("tester", "Tester"))
            mongo = FakeMongo()

            summary = apply_plan(plan_maintenance(directory), mongo)

            self.assertFalse(any(directory.glob(f"run-{run_id}*.json")))
            self.assertTrue((directory / "run-tester.json").exists())
            self.assertNotIn(run_id, mongo.documents)
            self.assertIn("tester", mongo.documents)
            self.assertEqual(summary.test_runs, 1)

    def test_invalid_files_are_deleted_without_discarding_valid_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "run-run-5.draft.json").write_text("{bad", encoding="utf-8")
            write_json(directory / "run-run-5.json", aggregate("run-5"))
            mismatched = aggregate("different-id")
            write_json(directory / "run-run-6.json", mismatched)
            mongo = FakeMongo()

            summary = apply_plan(plan_maintenance(directory), mongo)

            self.assertFalse((directory / "run-run-5.draft.json").exists())
            self.assertTrue((directory / "run-run-5.json").exists())
            self.assertFalse((directory / "run-run-6.json").exists())
            self.assertIn("run-5", mongo.documents)
            self.assertEqual(summary.invalid_files, 2)

    def test_rebuilds_bad_aggregate_from_finalized_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "run-run-7.draft.json", draft("run-7", status="finalized"))
            (directory / "run-run-7.json").write_text("[]", encoding="utf-8")
            write_json(directory / "run-run-7.sync.json", {"runId": "run-7", "synchronized": True})
            mongo = FakeMongo()

            summary = apply_plan(plan_maintenance(directory), mongo)

            rebuilt = json.loads((directory / "run-run-7.json").read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["status"], "completed")
            self.assertIn("run-7", mongo.documents)
            self.assertEqual(summary.uploaded, 1)

    def test_upload_failure_writes_safe_sidecar_and_rerun_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "run-run-8.json", aggregate("run-8"))

            failed = apply_plan(plan_maintenance(directory), FakeMongo({"run-8"}))
            sidecar = json.loads((directory / "run-run-8.sync.json").read_text(encoding="utf-8"))
            self.assertEqual(failed.upload_failed, 1)
            self.assertFalse(sidecar["synchronized"])
            self.assertNotIn("secret", sidecar["lastError"])

            mongo = FakeMongo()
            succeeded = apply_plan(plan_maintenance(directory), mongo)
            sidecar = json.loads((directory / "run-run-8.sync.json").read_text(encoding="utf-8"))
            self.assertEqual(succeeded.uploaded, 1)
            self.assertTrue(sidecar["synchronized"])
            self.assertIn("run-8", mongo.documents)

    def test_apply_uploads_only_pending_runs_unless_all_are_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "run-synced.json", aggregate("synced"))
            write_json(directory / "run-synced.sync.json", {"runId": "synced", "synchronized": True})
            write_json(directory / "run-fresh.json", aggregate("fresh"))
            sidecar = (directory / "run-synced.sync.json").read_bytes()

            plan = plan_maintenance(directory)
            self.assertEqual(plan.pending_uploads, ["fresh"])
            mongo = FakeMongo()
            summary = apply_plan(plan, mongo)

            self.assertEqual(sorted(mongo.documents), ["fresh"])
            self.assertEqual(summary.uploaded, 1)
            self.assertEqual((directory / "run-synced.sync.json").read_bytes(), sidecar)

            mongo = FakeMongo()
            apply_plan(plan_maintenance(directory), mongo, upload_all=True)
            self.assertEqual(sorted(mongo.documents), ["fresh", "synced"])

    def test_reevaluation_replaces_existing_aggregate_and_marks_it_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "simulation-results"
            results.mkdir()
            session_id = "b" * 32
            record = draft("lawyer-redo", status="finalized")
            write_json(results / "run-lawyer-redo.draft.json", record)
            stale = aggregate("lawyer-redo")
            stale["data"]["lawyer"]["finalScore"] = 1
            write_json(results / "run-lawyer-redo.json", stale)
            write_json(results / "run-lawyer-redo.sync.json", {"runId": "lawyer-redo", "synchronized": True})
            write_json(root / f"lawyer-defense-{session_id}.json", {"roundId": session_id, "transcript": "Lời bào chữa."})
            redone = deepcopy(record)
            redone["data"] = {"lawyer": {**lawyer_payload(), "finalScore": 75}}
            answers = iter(("1", session_id, "y"))

            with (
                patch("scripts.repair_simulation_results._llm_endpoint_reachable", return_value=True),
                patch("scripts.repair_simulation_results.reevaluate_draft", new=AsyncMock(return_value=redone)),
                redirect_stdout(io.StringIO()),
            ):
                self.assertTrue(_interactive_reevaluation(results, root, input_fn=lambda _: next(answers)))

            stored = json.loads((results / "run-lawyer-redo.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["data"]["lawyer"]["finalScore"], 75)
            self.assertEqual(plan_maintenance(results).pending_uploads, ["lawyer-redo"])

    def test_interactive_cli_applies_the_chosen_action(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "run-run-9.json", aggregate("run-9"))

            def run(answers, mongo):
                answers = iter(answers)
                with redirect_stdout(io.StringIO()):
                    return main(["--results-dir", str(directory)],
                                input_fn=lambda _: next(answers), mongo_factory=lambda: mongo)

            self.assertEqual(run(("q",), None), 0)
            self.assertFalse((directory / "run-run-9.sync.json").exists())
            self.assertEqual(run(("1", "y", "q"), None), 1)
            failing = FakeMongo({"run-9"})
            self.assertEqual(run(("1", "y", "q"), failing), 1)
            self.assertTrue(failing.indexes_ensured)
            mongo = FakeMongo()
            self.assertEqual(run(("1", "y", "q"), mongo), 0)
            self.assertIn("run-9", mongo.documents)

if __name__ == "__main__":
    unittest.main()
