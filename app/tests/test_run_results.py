import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.run_results import (
    ParticipantManager,
    RunResultStore,
    normalize_participant_name,
    translate_unity_result_fragment,
)


class FakeMongo:
    def __init__(self, failures=0):
        self.failures = failures
        self.documents = {}

    def ensure_indexes(self):
        pass

    def upsert(self, aggregate):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary mongo failure")
        self.documents[aggregate["runId"]] = dict(aggregate)


class RunResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_normalization_and_repeated_assignment(self):
        manager = ParticipantManager()
        first, changed = manager.assign("  Nguyễn   Văn A  ")
        self.assertTrue(changed)
        second, changed = manager.assign("Nguyễn Văn A")
        self.assertFalse(changed)
        self.assertEqual(first, second)
        self.assertEqual(normalize_participant_name("e\u0301"), "é")

    async def test_idempotent_fragment_and_finalized_aggregate(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ParticipantManager()
            participant, _ = manager.assign("Test User")
            mongo = FakeMongo()
            store = RunResultStore(Path(directory), mongo=mongo)
            await store.begin("run-1", "clinic", participant)
            fragment = {"runId": "run-1", "gameId": "clinic", "fragmentId": "patient-1", "data": {"patients": [{"patientId": "p1"}]}}
            await store.accept_fragment(fragment)
            await store.accept_fragment(fragment)
            result = await store.accept_fragment({**fragment, "fragmentId": "complete", "data": {"finalScore": 5}, "complete": True})
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(mongo.documents), 1)
            self.assertNotIn("fragments", json.loads((Path(directory) / "simulation-results" / "run-run-1.json").read_text()))

    async def test_sync_failure_is_sidecar_state_and_manual_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ParticipantManager()
            participant, _ = manager.assign("A")
            mongo = FakeMongo(failures=1)
            store = RunResultStore(Path(directory), mongo=mongo)
            await store.begin("run-2", "doctor", participant)
            await store.accept_fragment({"runId": "run-2", "gameId": "doctor", "fragmentId": "case", "data": {"case": 1}, "complete": True})
            sidecar = json.loads((Path(directory) / "simulation-results" / "run-run-2.sync.json").read_text())
            self.assertEqual(sidecar["attemptCount"], 1)
            self.assertEqual((await store.sync_all())["synchronized"], 1)

    async def test_unity_clinic_fragments_merge_and_finalize_to_fake_mongo(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ParticipantManager()
            participant, _ = manager.assign("Người thử")
            mongo = FakeMongo()
            store = RunResultStore(Path(directory), mongo=mongo)
            run_id = "clinic-run"
            for patient_id in ("p1", "p2"):
                dto = {"fragmentId": f"patient-{patient_id}", "runId": run_id,
                       "participantName": participant.name, "participantSessionId": participant.session_id,
                       "gameId": "clinic", "fragmentType": "clinic.patient_resolved",
                       "occurredAtUtc": "2026-09-14T00:00:00Z", "payload": {"patientId": patient_id,
                           "sicknessName": "flu", "severity": 1, "bedIndex": 0, "resolution": "cured",
                           "actionableAtUtc": "2026-09-14T00:00:00Z", "resolvedAtUtc": "2026-09-14T00:00:10Z",
                           "resolutionDurationSeconds": 10, "scoreDelta": 5, "scoreTotal": 5}}
                await store.accept_fragment(translate_unity_result_fragment(dto), participant=participant)
            finish = {"fragmentId": "round", "runId": run_id,
                      "participantName": participant.name, "participantSessionId": participant.session_id,
                      "gameId": "clinic", "fragmentType": "clinic.round_finished",
                      "occurredAtUtc": "2026-09-14T00:01:00Z", "payload": {"roundId": "r1", "scoreTotal": 10}}
            result = await store.accept_fragment(translate_unity_result_fragment(finish), participant=participant)
            self.assertEqual(result["status"], "completed")
            self.assertEqual([item["patientId"] for item in result["data"]["patientResults"]], ["p1", "p2"])
            self.assertIn(run_id, mongo.documents)

    async def test_sales_parts_and_turns_share_one_run_and_keep_order(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ParticipantManager()
            participant, _ = manager.assign("Sales participant")
            store = RunResultStore(Path(directory), mongo=FakeMongo())
            run_id = "sales-run"
            part1 = {"fragmentId": "part1", "runId": run_id, "participantName": participant.name,
                     "participantSessionId": participant.session_id, "gameId": "sale",
                     "fragmentType": "sales.part1_recording_uploaded", "occurredAtUtc": "x",
                     "payload": {"attemptId": "attempt-1", "selectedShoeId": "a", "bestFitShoeId": "b",
                                 "transcript": "được", "score": 80, "feedbackVi": "Tốt",
                                 "recordingAtUtc": "recorded", "assessmentCompletedAtUtc": "assessed"}}
            await store.accept_fragment(translate_unity_result_fragment(part1), participant=participant)
            for turn_id in ("t1", "t2"):
                turn = {**part1, "fragmentId": turn_id, "fragmentType": "sales.part2.turn_accepted",
                        "payload": {"turnId": turn_id, "transcript": turn_id}}
                await store.accept_fragment(translate_unity_result_fragment(turn), participant=participant)
            done = {**part1, "fragmentId": "done", "fragmentType": "sales.part2.completed",
                    "payload": {"score": 85, "customerRating": "good", "criterionScores": {"apologyAndPolicyRemedy": 45, "adaptabilityAndDeescalation": 40}, "trustState": "restored", "completionReason": "normal",
                                "emotionalHandling": True, "causeIdentification": True,
                                "solutionSuitability": True, "trustRebuilding": True,
                                "acceptedTurnCount": 2}}
            result = await store.accept_fragment(translate_unity_result_fragment(done), participant=participant)
            self.assertEqual(result["status"], "completed")
            self.assertEqual([turn["turnId"] for turn in result["data"]["turns"]], ["t1", "t2"])
            self.assertEqual(result["data"]["part1"]["selectedShoeId"], "a")

    async def test_sales_waits_for_part1_transcript_and_assessment_before_finalizing(self):
        with tempfile.TemporaryDirectory() as directory:
            participant, _ = ParticipantManager().assign("Sales participant")
            store = RunResultStore(Path(directory), mongo=FakeMongo())
            run_id = "sales-pending"
            await store.begin(run_id, "sale", participant)
            await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "part1-pending",
                "data": {"part1": {"attemptId": "attempt-1", "selectedShoeId": "shoe-a",
                                     "bestFitShoeId": "shoe-b", "transcript": None,
                                     "score": None, "feedbackVi": None,
                                     "recordingAtUtc": "recorded",
                                     "assessmentCompletedAtUtc": None}},
            }, participant=participant)
            pending = await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "part2-complete",
                "requiredFields": ["part1", "part2"], "complete": True,
                "data": {"part2": {"score": 85, "customerRating": "good", "criterionScores": {"apologyAndPolicyRemedy": 45, "adaptabilityAndDeescalation": 40}, "trustState": "restored", "completionReason": "natural",
                                     "emotionalHandling": True, "causeIdentification": True,
                                     "solutionSuitability": True, "trustRebuilding": True,
                                     "acceptedTurnCount": 0}},
            }, participant=participant)
            self.assertEqual(pending["status"], "draft")

            completed = await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "part1-assessed",
                "data": {"part1": {"attemptId": "attempt-1", "selectedShoeId": "shoe-a",
                                     "bestFitShoeId": "shoe-b", "transcript": "Tôi đề xuất đôi A.",
                                     "score": 80, "feedbackVi": "Lập luận phù hợp.",
                                     "recordingAtUtc": "recorded",
                                     "assessmentCompletedAtUtc": "assessed"}},
            }, participant=participant)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["data"]["part1"]["selectedShoeId"], "shoe-a")
            self.assertEqual(completed["data"]["part1"]["transcript"], "Tôi đề xuất đôi A.")

    async def test_late_partial_sales_fragment_does_not_erase_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            participant, _ = ParticipantManager().assign("Sales participant")
            store = RunResultStore(Path(directory), mongo=FakeMongo())
            run_id = "sales-late-fragment"
            await store.begin(run_id, "sale", participant)
            await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "part1-assessed",
                "data": {"part1": {"attemptId": "attempt-1", "selectedShoeId": "shoe-a",
                                     "bestFitShoeId": "shoe-b", "transcript": "Tôi chọn đôi A.",
                                     "score": 90, "feedbackVi": "Tốt.",
                                     "recordingAtUtc": "recorded",
                                     "assessmentCompletedAtUtc": "assessed"}},
            }, participant=participant)
            draft = await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "part1-unity-ack",
                "data": {"part1": {"attemptId": "attempt-1", "selectedShoeId": "shoe-a",
                                     "bestFitShoeId": "shoe-b"}},
            }, participant=participant)
            self.assertEqual(draft["data"]["part1"]["transcript"], "Tôi chọn đôi A.")
            self.assertEqual(draft["data"]["part1"]["score"], 90)

    async def test_sales_waits_for_every_accepted_part2_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            participant, _ = ParticipantManager().assign("Sales participant")
            store = RunResultStore(Path(directory), mongo=FakeMongo())
            run_id = "sales-pending-turn"
            await store.begin(run_id, "sale", participant)
            await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "part1",
                "data": {"part1": {"attemptId": "attempt-1", "selectedShoeId": "shoe-a",
                                     "bestFitShoeId": "shoe-b", "transcript": "Tôi chọn đôi A.",
                                     "score": 90, "feedbackVi": "Tốt.",
                                     "recordingAtUtc": "recorded",
                                     "assessmentCompletedAtUtc": "assessed"}},
            }, participant=participant)
            pending = await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "part2",
                "requiredFields": ["part1", "part2"], "complete": True,
                "data": {"part2": {"score": 85, "customerRating": "good", "criterionScores": {"apologyAndPolicyRemedy": 45, "adaptabilityAndDeescalation": 40}, "trustState": "restored", "completionReason": "natural",
                                     "emotionalHandling": True, "causeIdentification": True,
                                     "solutionSuitability": True, "trustRebuilding": True,
                                     "acceptedTurnCount": 1}},
            }, participant=participant)
            self.assertEqual(pending["status"], "draft")

            completed = await store.accept_fragment({
                "runId": run_id, "gameId": "sale", "fragmentId": "turn-1",
                "data": {"turns": [{"turnId": "turn-1", "transcript": "Em xin lỗi chị."}]},
            }, participant=participant)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["data"]["turns"][0]["transcript"], "Em xin lỗi chị.")

    def test_sales_store_projection_uses_aggregate_field_names(self):
        from app.run_results import project_sales_part1, project_sales_part2

        part1 = project_sales_part1({
            "attemptId": "attempt-1", "salesSessionId": "session-1",
            "scenarioId": "scenario-1", "customerId": "customer-1",
            "selectedShoeId": "shoe-a", "bestFitShoeId": "shoe-b",
            "transcript": "Tôi chọn đôi A.", "score": 75, "feedbackVi": "Ổn.",
            "createdAtUtc": "recorded", "updatedAtUtc": "assessed",
        })
        self.assertEqual(part1["recordingAtUtc"], "recorded")
        self.assertEqual(part1["assessmentCompletedAtUtc"], "assessed")
        self.assertNotIn("createdAtUtc", part1)

        part2 = project_sales_part2({
            "turns": [{"turnId": "turn-1", "transcript": "Em xin lỗi chị.",
                       "customerText": "Chị muốn nghe giải pháp cụ thể.",
                       "playerResponseRating": "bad",
                       "policyViolations": ["unauthorized_refund"],
                       "activeObjective": 2, "objectiveActiveDuringTurn": 1,
                       "createdAtUtc": "turn-time"}],
            "rawScore": 70,
            "policyViolationPenalty": 10,
            "policyViolations": [{"turnId": "turn-1", "code": "unauthorized_refund"}],
            "score": 60,
            "goodResponseCount": 1,
            "badResponseCount": 2,
            "finalCustomerText": "Chị không muốn nói chuyện với em, kêu quản lý ra đây cho chị.",
        })
        self.assertEqual(part2["turns"][0]["customerReply"], "Chị muốn nghe giải pháp cụ thể.")
        self.assertEqual(part2["turns"][0]["turnTimestampUtc"], "turn-time")
        self.assertEqual(part2["rawScore"], 70)
        self.assertEqual(part2["policyViolationPenalty"], 10)
        self.assertEqual(part2["policyViolations"][0]["code"], "unauthorized_refund")
        self.assertEqual(part2["goodResponseCount"], 1)
        self.assertEqual(part2["badResponseCount"], 2)

    async def test_current_unity_envelopes_post_for_all_games_and_exclude_extra_fields(self):
        from httpx import ASGITransport, AsyncClient
        from app import main

        with tempfile.TemporaryDirectory() as directory:
            original_store = main.run_result_store
            original_participant = main.participant_manager.active
            original_activity = main.participant_manager.activity
            original_active_run = main.active_run_id
            try:
                participant, _ = main.participant_manager.assign("Envelope Tester")
                main.run_result_store = RunResultStore(Path(directory), mongo=FakeMongo(failures=9))
                async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
                    async def post(run_id, game_id, kind, payload):
                        envelope = {"kind": kind, "fragmentId": kind + run_id, "runId": run_id,
                                    "participantName": participant.name, "participantSessionId": participant.session_id,
                                    "gameId": game_id, "occurredAtUtc": "2026-09-14T00:00:00Z",
                                    "startedAtUtc": "2026-09-14T00:00:00Z", "complete": False,
                                    "requiredFields": ["ignored-by-backend"], "data": payload}
                        response = await client.post("/api/results/fragments", json=envelope)
                        self.assertEqual(response.status_code, 200, response.text)
                        return response

                    patient = {"patientId": "p1", "sicknessName": "flu", "severity": 1, "bedIndex": 0,
                               "resolution": "cured", "actionableAtUtc": "2026-09-14T00:00:00Z",
                               "resolvedAtUtc": "2026-09-14T00:00:10Z", "resolutionDurationSeconds": 10,
                               "scoreDelta": 5, "scoreTotal": 5, "audio": "drop", "diagnostics": "drop"}
                    await post("clinic-run", "clinic", "clinic.patient_resolved", patient)
                    await post("clinic-run", "clinic", "clinic.round_finished", {"roundId": "r1", "scoreTotal": 5, "model": "drop"})

                    case = {"roundId": "dr", "caseId": "case1", "patientName": "P", "selectedQuestionsJson": "[]",
                            "notesJson": "[]", "scoreDelta": 1, "scoreTotal": 1, "remainingTime": 1,
                            "caseSubmittedAtUtc": "x", "audio": "drop"}
                    await post("doctor-run", "doctor", "doctor.case_submitted", case)
                    await post("doctor-run", "doctor", "doctor.round_finished", {"roundId": "dr", "scoreTotal": 1})

                    lawyer = {"roundId": "lr", "caseId": "case", "interviewRestartCount": 0, "transcript": "bào chữa",
                              "criterionScores": {"evidenceUse": 1}, "rawScore": 1, "restartPenaltyPercent": 0,
                              "finalScore": 1, "feedbackVi": "tốt", "completionStatus": "completed",
                              "assessmentContextJson": "must not persist", "provider": "must not persist"}
                    await post("lawyer-run", "lawyer", "lawyer.defense_completed", lawyer)

                    await post("sales-run", "sale", "sales.part1_recording_uploaded", {
                        "roundId": "a", "cardId": "shoe", "bestFitShoeId": "best-shoe",
                        "transcript": "Tôi đề xuất đôi này.", "score": 70,
                        "feedback": "Lập luận phù hợp.", "recordingAtUtc": "recorded",
                        "assessmentCompletedAtUtc": "assessed", "resolution": "{}",
                        "audio": "drop",
                    })
                    await post("sales-run", "sale", "sales.part2.completed", {"score": 0, "customerRating": "bad", "criterionScores": {"apologyAndPolicyRemedy": 0, "adaptabilityAndDeescalation": 0}, "trustState": "lost", "completionReason": "silence_limit",
                        "emotionalHandling": False, "causeIdentification": False, "solutionSuitability": False,
                        "trustRebuilding": False, "acceptedTurnCount": 0, "diagnostic": "drop"})
                    bad = await client.post("/api/results/fragments", json={"kind": "unknown.result", "runId": "bad", "gameId": "clinic", "data": {}})
                    self.assertEqual(bad.status_code, 422)

                for run_id in ("clinic-run", "doctor-run", "lawyer-run", "sales-run"):
                    aggregate = json.loads((Path(directory) / "simulation-results" / f"run-{run_id}.json").read_text(encoding="utf-8"))
                    serialized = json.dumps(aggregate, ensure_ascii=False)
                    self.assertNotIn("drop", serialized)
                    self.assertNotIn("assessmentContextJson", serialized)
                    self.assertNotIn("provider", serialized)
            finally:
                main.run_result_store = original_store
                main.participant_manager.active = original_participant
                main.participant_manager.activity = original_activity
                main.active_run_id = original_active_run


if __name__ == "__main__":
    unittest.main()
