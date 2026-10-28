"""Replay context reconstruction and benchmark bookkeeping (offline)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.sales_adaptive import build_messages, model_context, turn_policy
from app.sales_pipeline import process_turn
from app.sales_returning_customer import ReturningSessionStore
from app.tests.sales_fixtures import write_part1
from app.tests.test_sales_pipeline_api import classification
from app.tests.test_sales_returning_customer import FakeTranscriber, request
from scripts.benchmark_sales_adaptive import (ADAPTIVE_ARMS, DEFAULT_MANIFEST, LEGACY_ARM, ROOT, build_cases,
                                              quantile, schedule, stats, summarize)
from scripts.sales_adaptive_replay import labels_for, reconstruct_case, sha256_file

SETTINGS = SimpleNamespace(pipeline_mode="openrouter", sales_max_turns=8, sales_luna_arbitration_enabled=False,
                           sales_jev_timeout_seconds=2, sales_writer_timeout_seconds=2)


class Writer:
    async def write(self, plan, transcript, history):
        return {"customerText": plan["fallbackText"], "metadata": {}, "fallbackUsed": False}


class ReplayContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        recordings = self.root / "recordings"
        store = ReturningSessionStore(recordings)
        attempt = write_part1(recordings, "src", "run-src")
        with patch("app.sales_returning_customer.get_settings", return_value=SETTINGS):
            await store.create_or_resume("src", attempt, run_id="run-src")
        script = [("TURN ONE APOLOGY UTTERANCE", ("acknowledgment",)),
                  ("TURN TWO OPEN QUESTION UTTERANCE", ("openQuestion",)),
                  ("TURN THREE WHERE DOES IT HURT", ("painLocationQuestion",)),
                  ("TURN FOUR DO YOU HAVE THE RECEIPT", ("receiptRequest",))]
        for index, (text, labels) in enumerate(script, 1):
            class Classifier:
                async def classify(self, transcript, context, labels=labels):
                    return classification(*labels)
            result = await process_turn("src", request(f"turn-{index}"), store=store, transcriber=FakeTranscriber(text),
                                        classifier=Classifier(), writer=Writer())
            self.assertEqual(result["status"], "accepted", result)
        path = recordings / "sales-session-src.json"
        session = json.loads(path.read_text(encoding="utf-8"))
        # Unique markers make any leak of the selected or later replies visible.
        for index, turn in enumerate(session["turns"], 1):
            turn["customerText"] = f"MARKER LAN REPLY {index} chị"
        path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        self.session = session
        self.case = {"caseId": "T-01", "sessionId": "src", "sourceRelativePath": "recordings/sales-session-src.json",
                     "savedTurnIndex1Based": 3, "turnId": "turn-3",
                     "historicalReference": {"customerText": "MARKER LAN REPLY 3 chị"}}
        self.path = path

    async def asyncTearDown(self):
        self.temp.cleanup()

    def context(self):
        rebuilt = reconstruct_case(self.case, self.root)
        policy = turn_policy(rebuilt["before"], rebuilt["candidate"], rebuilt["decision"], rebuilt["plan"], rebuilt["labels"])
        context = model_context(rebuilt["before"], rebuilt["candidate"], rebuilt["decision"], rebuilt["plan"],
                                rebuilt["labels"], rebuilt["transcript"], policy)
        return rebuilt, context, "\n".join(message["content"] for message in build_messages(context))

    def test_context_excludes_selected_reply_future_turns_and_private_ids(self):
        digest = sha256_file(self.path)
        rebuilt, context, text = self.context()
        self.assertEqual(sha256_file(self.path), digest)
        for present in ("TURN ONE APOLOGY UTTERANCE", "TURN TWO OPEN QUESTION UTTERANCE", "MARKER LAN REPLY 1",
                        "MARKER LAN REPLY 2", "TURN THREE WHERE DOES IT HURT"):
            self.assertIn(present, text)
        for absent in ("MARKER LAN REPLY 3", "MARKER LAN REPLY 4", "TURN FOUR", "run-src", "part1-src",
                       '"score"', '"turnQuality"', '"noul"'):
            self.assertNotIn(absent, text)
        self.assertEqual(context["currentSalespersonUtterance"], "TURN THREE WHERE DOES IT HURT")
        self.assertEqual(rebuilt["before"]["dialogueTurnCount"], 2)
        self.assertEqual(len(rebuilt["before"]["evidenceLedger"]), 2)

    def test_final_memory_is_reset_to_prefix_state(self):
        final = self.session["customerMemory"]["facts"]
        self.assertTrue(final["pain_location"]["shared"] and final["receipt_status"]["shared"])
        rebuilt, context, _ = self.context()
        shared = context["facts"]["sharedFacts"]
        self.assertNotIn("pain_location", shared)  # disclosed by the selected turn's reply
        self.assertNotIn("receipt_status", shared)  # disclosed later
        self.assertIn("pain_location", context["facts"]["mayDiscloseNow"])  # asked now
        self.assertEqual(context["facts"]["mustAnswerFactIds"], ["pain_location"])
        self.assertEqual(rebuilt["before"]["customerMemory"]["requests"], {})
        self.assertIn("walking_routine", shared)  # immutable Part 1 memory

    def test_legacy_boolean_labels_map_with_provenance(self):
        turn = {"policyViolations": ["unauthorized_compensation"],
                "turnAssessment": {"apology": True, "condescending": True, "unauthorizedPromise": True,
                                   "openQuestion": False, "verificationStep": True}}
        labels, provenance = labels_for(turn, None)
        self.assertEqual(labels["acknowledgment"]["status"], "true")
        self.assertEqual(labels["disrespect"]["status"], "true")
        self.assertEqual(labels["unauthorizedCompensation"]["status"], "true")
        self.assertEqual(labels["receiptRequest"]["status"], "false")
        self.assertIn("receiptRequest", provenance["absentLabelsSetFalse"])
        self.assertEqual(provenance["unmappedLegacyKeys"], ["verificationStep"])
        self.assertEqual(provenance["origin"], "turnAssessment:legacy-boolean")

    def test_prefix_disclosures_arrive_at_their_original_turn(self):
        case = dict(self.case, savedTurnIndex1Based=4, turnId="turn-4")
        rebuilt = reconstruct_case(case, self.root)
        ledger = rebuilt["before"]["evidenceLedger"]
        self.assertTrue(rebuilt["before"]["customerMemory"]["facts"]["pain_location"]["shared"])
        self.assertIn("pain_location", rebuilt["before"]["investigationEvidence"])
        for entry in ledger:
            # Turn three answers the pain question, so its own before-state
            # and every earlier entry must still lack that disclosure.
            self.assertNotIn("pain_location", entry["factsKnownBefore"])
        self.assertIn("pain_location", ledger[2]["disclosedFactIds"])

    def test_terminal_prefix_conflict_drops_only_credit_labels(self):
        generous = {name: True for name in ("emotionalAcknowledgment", "openQuestion", "fitConditionOrPreferenceQuestion",
                                             "causeStatement", "policyExchange", "lightweightForWalking", "fitOrWalkTrial")}
        attempt = write_part1(self.root / "recordings", "legacy", "run-legacy")
        source = {"sessionId": "legacy", "runId": "run-legacy", "part1AttemptId": attempt,
                  "openingComplaint": "Em ơi, chị bị đau chân.",
                  "turns": [{"turnId": "a", "transcript": "A", "customerText": "Chị chưa hài lòng.", "turnAssessment": generous},
                            {"turnId": "b", "transcript": "B", "customerText": "Chị về.", "turnAssessment": {"abuse": False}}]}
        (self.root / "legacy.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        rebuilt = reconstruct_case({"caseId": "L", "sourceRelativePath": "legacy.json", "savedTurnIndex1Based": 2,
                                    "turnId": "b"}, self.root)
        prior = rebuilt["provenance"]["priorTurns"][0]["labels"]
        self.assertEqual(prior["terminalConflictEnding"], "objectives_completed")
        self.assertIn("causeStatement", prior["terminalConflictCreditLabelsDropped"])
        self.assertFalse(rebuilt["decision"]["conversationComplete"])


@unittest.skipUnless(DEFAULT_MANIFEST.exists() and (ROOT / "recordings").exists(), "local benchmark corpus not present")
class RealManifestTests(unittest.TestCase):
    def test_all_cases_rebuild_without_leakage(self):
        effective, cases, checks = build_cases(DEFAULT_MANIFEST, ROOT)
        self.assertEqual(checks["errors"], [])
        self.assertEqual((checks["caseCount"], checks["distinctSessions"]), (10, 10))
        self.assertTrue(all(checks["sourceHashBefore"].values()) and all(checks["sourceHashAfter"].values()))
        sa01 = next(item for item in cases if item["case"]["caseId"] == "SA-01")
        self.assertEqual((sa01["case"]["savedTurnIndex1Based"], sa01["case"]["turnId"]),
                         (6, "8fa9657a2b17451b9fbe26a2b7cdcff0"))
        self.assertTrue(sa01["policy"]["closeAllowed"])
        self.assertGreater(sa01["policy"]["turnsRemaining"], 0)
        self.assertIsNone(sa01["policy"]["deterministicEnding"])
        self.assertEqual(effective["amendments"][0]["caseId"], "SA-01")
        self.assertTrue(all("replayInput" not in case for case in effective["cases"]))


class BenchmarkBookkeepingTests(unittest.TestCase):
    def test_quantiles(self):
        self.assertEqual(quantile([4, 1, 3, 2], .5), 2.5)
        self.assertAlmostEqual(quantile([1, 2, 3, 4], .9), 3.7)
        self.assertIsNone(quantile([], .5))
        self.assertEqual(stats([None, 2.0])["n"], 1)

    def test_schedule_is_seeded_interleaved_and_balanced(self):
        cases = [f"SA-{index:02d}" for index in range(1, 11)]
        slots = schedule(cases, 3, 7, False)
        self.assertEqual(len(slots), 60)
        self.assertEqual(slots, schedule(cases, 3, 7, False))
        for case in cases:
            orders = [slot["blockOrder"] for slot in slots if slot["caseId"] == case and slot["positionInBlock"] == 0]
            self.assertEqual(len(orders), 3)
            self.assertTrue({"AB", "BA"} <= set(orders))
        for index in range(0, 60, 2):
            self.assertEqual({slots[index]["arm"], slots[index + 1]["arm"]}, set(ADAPTIVE_ARMS))
            self.assertEqual(slots[index]["caseId"], slots[index + 1]["caseId"])
        with_legacy = schedule(cases, 3, 7, True)
        self.assertEqual(sum(1 for slot in with_legacy if slot["arm"] == LEGACY_ARM), 30)
        self.assertEqual(len(with_legacy), 90)

    def test_summary_keeps_unknown_costs_null_and_counts_providers(self):
        def sample(arm, cost, wall, provider, outcome="model", reasoning=0):
            usage = {"promptTokens": 1000, "completionTokens": 50 + reasoning, "reasoningTokens": reasoning,
                     "cachedTokens": 0, "totalTokens": 1050 + reasoning, "costUsd": cost}
            return {"seq": 1, "caseId": "SA-01", "arm": arm, "outcome": outcome, "wallClockReadySeconds": wall,
                    "firstAttemptLatencySeconds": wall, "httpAttempts": [{"latencySeconds": wall, "usage": usage}],
                    "retryCount": 0, "failedAttempts": 0, "costUsd": cost, "costMissingAttempts": int(cost is None),
                    "servedProviders": [provider], "servedModels": ["qwen/qwen3.7-flash"], "finishReasons": ["stop"],
                    "tokens": {"reasoningTokens": reasoning}, "output": {"action": "raise_concern", "decision": "continue"},
                    "fallbackReason": None if outcome == "model" else "adaptive_timeout"}
        samples = [sample("adaptive_off", 0.0001, 1.0, "Alibaba"), sample("adaptive_off", None, 3.0, "Alibaba", "fallback"),
                   sample("adaptive_on", 0.0003, 2.0, "Alibaba", reasoning=300),
                   sample("adaptive_on", 0.0005, 4.0, "Alibaba", reasoning=500)]
        summary = summarize(samples, {"mode": "test"})
        off, on = summary["arms"]["adaptive_off"], summary["arms"]["adaptive_on"]
        self.assertIsNone(off["costUsdTotal"])
        self.assertEqual(off["costUsdKnownTotal"], 0.0001)
        self.assertEqual(off["samplesMissingCost"], 1)
        self.assertEqual(off["fallbackRate"], .5)
        self.assertAlmostEqual(on["costUsdTotal"], 0.0008)
        self.assertEqual(on["servedProviders"], {"Alibaba": 2})
        self.assertEqual(on["samplesWithReasoningTokens"], 2)
        self.assertEqual(on["wallClockReadySeconds"]["median"], 3.0)
        self.assertAlmostEqual(summary["pairedByCase"]["SA-01"]["wallMedianDeltaOnMinusOff"], 1.0)
        self.assertIsNone(summary["pairedByCase"]["SA-01"]["costRatioOnOverOff"])
