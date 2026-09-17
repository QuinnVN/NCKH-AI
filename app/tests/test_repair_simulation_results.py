from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.repair_simulation_results import main, maintain_results


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


class RepairSimulationResultsTests(unittest.TestCase):
    def test_dry_run_reports_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "run-run-1.draft.json"
            original = draft("run-1")
            write_json(path, original)

            summary = maintain_results(directory, apply=False)

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

            summary = maintain_results(directory, apply=True, mongo=mongo)

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

            summary = maintain_results(directory, apply=True, mongo=mongo)

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

            summary = maintain_results(directory, apply=True, mongo=FakeMongo())

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)
            self.assertFalse((directory / "run-run-4.json").exists())
            self.assertEqual(summary.finalized_runs, 0)

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

            summary = maintain_results(directory, apply=True, mongo=mongo)

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

            summary = maintain_results(directory, apply=True, mongo=mongo)

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

            summary = maintain_results(directory, apply=True, mongo=mongo)

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

            summary = maintain_results(directory, apply=True, mongo=mongo)

            rebuilt = json.loads((directory / "run-run-7.json").read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["status"], "completed")
            self.assertIn("run-7", mongo.documents)
            self.assertEqual(summary.uploaded, 1)

    def test_upload_failure_writes_safe_sidecar_and_rerun_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "run-run-8.json", aggregate("run-8"))

            failed = maintain_results(directory, apply=True, mongo=FakeMongo({"run-8"}))
            sidecar = json.loads((directory / "run-run-8.sync.json").read_text(encoding="utf-8"))
            self.assertEqual(failed.upload_failed, 1)
            self.assertFalse(sidecar["synchronized"])
            self.assertNotIn("secret", sidecar["lastError"])

            mongo = FakeMongo()
            succeeded = maintain_results(directory, apply=True, mongo=mongo)
            sidecar = json.loads((directory / "run-run-8.sync.json").read_text(encoding="utf-8"))
            self.assertEqual(succeeded.uploaded, 1)
            self.assertTrue(sidecar["synchronized"])
            self.assertIn("run-8", mongo.documents)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_json(directory / "run-run-9.json", aggregate("run-9"))

            self.assertEqual(main(["--results-dir", str(directory)]), 0)
            with patch("scripts.repair_simulation_results.configured_mongo", return_value=None):
                self.assertEqual(main(["--apply", "--results-dir", str(directory)]), 2)
            failing = FakeMongo({"run-9"})
            with patch("scripts.repair_simulation_results.configured_mongo", return_value=failing):
                self.assertEqual(main(["--apply", "--results-dir", str(directory)]), 1)
            self.assertTrue(failing.indexes_ensured)


if __name__ == "__main__":
    unittest.main()
