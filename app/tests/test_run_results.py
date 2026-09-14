import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.run_results import ParticipantManager, RunResultStore, normalize_participant_name


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


if __name__ == "__main__":
    unittest.main()
