"""Sales v3 behavior through the turn-pipeline seam.

Tests call process_turn() and complete() directly on a temporary session store.
STT, Jev and Qwen are scripted fakes passed through the existing injection
parameters; Luna is disabled. Assertions read only what the pipeline returns
or what complete() stores, never the concern module or planner internals,
except where the spec asks for plan-level facts such as the raised concern.
"""
import copy
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.sales_concerns import FACT_QUESTIONS, GOOD_LABELS, score_ledger, score_session
from app.sales_customer_lines import (ACTION_LINES, CLARIFY_LINES, CONCERN_LINES, ENDING_LINES, GENERIC_LINES,
                                      REPEATED_LINES, fact_sentence)
from app.sales_openrouter import FACTS, customer_text_rejections
from app.sales_pipeline import complete, process_turn
from app.sales_rescore import rescore_directory, write_report
from app.sales_returning_customer import CompletionRequest, ReturningSessionStore
from app.sales_rubric import BAD_TURN_LABELS, VIOLATIONS
from app.tests.test_sales_pipeline_api import classification
from app.tests.test_sales_returning_customer import FakeTranscriber, request

FIXTURES = Path(__file__).parent / "fixtures" / "sales_v3_regression_labels.json"
SETTINGS = SimpleNamespace(sales_pipeline_mode="openrouter", sales_max_turns=8, sales_luna_arbitration_enabled=False,
                           sales_jev_timeout_seconds=2, sales_writer_timeout_seconds=2)


class ScriptedClassifier:
    def __init__(self):
        self.next = classification()

    async def classify(self, transcript, context):
        return self.next


class PlanWriter:
    """Returns the backend line, optionally misbehaving to exercise the guards."""

    def __init__(self, mode="fallback"):
        self.mode, self.plans, self.previous = mode, [], None

    async def write(self, plan, transcript, history):
        self.plans.append(copy.deepcopy(plan))
        if self.mode == "fail":
            raise RuntimeError("writer offline")
        text = self.previous if self.mode == "repeat" and self.previous else plan["fallbackText"]
        return {"customerText": text, "metadata": {}, "fallbackUsed": False}


def repeats_recent(text, recent):
    return "repeated_reply" in customer_text_rejections(text, {"avoidReplyTexts": recent[-3:], "knownFacts": list(FACTS)})


class TurnPipelineHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = ReturningSessionStore(self.directory)
        self.classifier, self.writer = ScriptedClassifier(), PlanWriter()
        self.stt = FakeTranscriber("Dạ, em muốn hỏi chị thêm một chút.")
        self.index = 0
        await self.new_session("s3")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def new_session(self, session_id):
        self.session_id = session_id
        with patch("app.sales_returning_customer.get_settings", return_value=SETTINGS):
            await self.store.create_or_resume(session_id, run_id="run-" + session_id)

    async def turn(self, *true, uncertain=()):
        self.index += 1
        self.classifier.next = classification(*true, **{name: .5 for name in uncertain})
        result = await process_turn(self.session_id, request(f"t{self.index}"), store=self.store, transcriber=self.stt,
                                    classifier=self.classifier, writer=self.writer)
        self.assertEqual(result["status"], "accepted", result)
        return result

    async def complete(self, reason="natural"):
        return await complete(self.session_id, CompletionRequest(completionId="done", reason=reason), store=self.store)

    async def state(self):
        return await self.store.get(self.session_id)


class CustomerStateTests(TurnPipelineHarness):
    async def test_fact_question_is_answered_immediately_then_lan_raises_her_concern(self):
        turn = await self.turn("painLocationQuestion")
        self.assertEqual(turn["disclosedFactIds"], ["pain_location"])
        self.assertIn("gót chân", turn["customerText"])
        self.assertEqual(turn["activeObjective"], 1)
        plan = self.writer.plans[-1]
        self.assertEqual((plan["intent"], plan["concern"], plan["missingPart"]), ("raise_concern", 1, "acknowledgment"))
        self.assertEqual(plan["customerDisposition"], "receptive")

    async def test_unheard_concern_escalates_then_ends_in_stalemate_without_manager(self):
        first, second = await self.turn(), await self.turn()
        self.assertEqual([plan["hintLevel"] for plan in self.writer.plans], [1, 2])
        self.assertEqual(self.writer.plans[-1]["customerDisposition"], "irritated")
        self.assertFalse(second["conversationComplete"])
        last = await self.turn()
        self.assertTrue(last["conversationComplete"])
        self.assertEqual(last["endingReason"], "stalemate")
        self.assertEqual(last["remainingTurns"], 5)
        self.assertNotIn("quản lý", last["customerText"])
        final = await self.complete()
        self.assertEqual((final["endingReason"], final["completionReason"], final["trustState"]), ("stalemate", "stalemate", "lost"))
        ledger = (await self.state())["evidenceLedger"]
        self.assertEqual([entry["noProgress"] for entry in ledger], [True, True, True])
        self.assertEqual([entry["hintLevel"] for entry in ledger], [1, 2, 2])
        self.assertNotEqual(first["customerText"], second["customerText"])

    async def test_skills_count_in_any_order_and_the_displayed_objective_catches_up(self):
        turns = [await self.turn("exchangeOffer", "exchangeConditions"),
                 await self.turn("walkingQuestion"), await self.turn("fitQuestion"),
                 await self.turn("acknowledgment", "openQuestion"),
                 await self.turn("causeStatement"),
                 await self.turn("lightweightForWalking", "fitOrWalkTrial")]
        self.assertEqual([turn["activeObjective"] for turn in turns], [1, 1, 1, 2, 3, 4])
        self.assertEqual([turn["objectiveCompleted"] for turn in turns], [False, False, False, True, True, True])
        self.assertEqual(self.writer.plans[-1]["intent"], "trust_challenge")
        self.assertFalse(turns[-1]["conversationComplete"])
        last = await self.turn("originalSaleResponsibility", "routineMatchExplanation", "fitOrWalkTrial")
        self.assertEqual(last["endingReason"], "objectives_completed")
        final = await self.complete()
        self.assertEqual((final["score"], final["trustState"]), (100, "restored"))
        self.assertTrue(all(final[flag] for flag in ("emotionalHandling", "causeIdentification", "solutionSuitability", "trustRebuilding")))

    async def test_cause_and_solution_only_count_after_the_needed_facts_are_known(self):
        await self.turn("acknowledgment", "openQuestion")
        early = await self.turn("causeStatement", "lightweightForWalking", "fitOrWalkTrial")
        self.assertEqual(early["playerResponseRating"], "neutral")
        self.assertEqual(set((await self.state())["rubricComponents"]), {"acknowledgment"})
        await self.turn("walkingQuestion", "preferenceQuestion")
        cause = await self.turn("causeStatement")
        self.assertEqual(cause["playerResponseRating"], "good")
        self.assertEqual(cause["activeObjective"], 3)
        self.assertEqual(set((await self.state())["rubricComponents"]),
                         {"acknowledgment", "use_question", "fit_question", "cause"})

    async def test_lan_says_she_already_answered_only_when_every_asked_fact_is_known(self):
        await self.turn("painLocationQuestion")
        repeated = await self.turn("painLocationQuestion")
        self.assertEqual((self.writer.plans[-1]["intent"], repeated["disclosedFactIds"]), ("repeated_question", []))
        mixed = await self.turn("painLocationQuestion", "painTimingQuestion")
        self.assertEqual((self.writer.plans[-1]["intent"], mixed["disclosedFactIds"]), ("raise_concern", ["late_discomfort"]))

    async def test_exchange_offer_after_the_cause_is_found_moves_to_the_solution(self):
        await self.turn("acknowledgment", "openQuestion")
        await self.turn("walkingQuestion", "fitQuestion")
        await self.turn("causeStatement")
        offer = await self.turn("exchangeOffer", "exchangeConditions")
        self.assertFalse(offer["conversationComplete"])
        self.assertEqual((self.writer.plans[-1]["concern"], self.writer.plans[-1]["missingPart"]), (3, "lightweight"))

    async def test_bad_turn_reveals_nothing_and_keeps_the_hint_level(self):
        await self.turn()
        rude = await self.turn("disrespect", "painLocationQuestion")
        self.assertEqual((rude["playerResponseRating"], rude["disclosedFactIds"]), ("bad", []))
        self.assertEqual(self.writer.plans[-1]["intent"], "warning")
        self.assertEqual((await self.state())["evidenceLedger"][-1]["hintLevel"], 1)

    async def test_failing_writer_never_repeats_and_never_exhausts_the_fallback_pool(self):
        self.writer.mode = "fail"
        spoken = []
        for labels in ((), ("painLocationQuestion",), (), ("acknowledgment",), (), ("openQuestion",), (), ()):
            turn = await self.turn(*labels)
            self.assertTrue(turn["fallbackUsed"])
            self.assertFalse(repeats_recent(turn["customerText"], spoken), turn["customerText"])
            self.assertFalse(turn["writerMetadata"]["fallbackSelection"]["exhausted"])
            spoken.append(turn["customerText"])
            if turn["conversationComplete"]:
                break


class RegressionFixtureTests(TurnPipelineHarness):
    async def replay(self, fixture):
        await self.new_session(fixture["sessionId"])
        results = []
        for item in fixture["turns"]:
            if results and results[-1]["conversationComplete"]:
                break
            results.append(await self.turn(*item["true"], uncertain=item["uncertain"]))
        return results

    async def test_stored_labels_from_recent_sessions_replay_under_v3(self):
        fixtures = {item["sessionId"]: item for item in json.loads(FIXTURES.read_text(encoding="utf-8"))["sessions"]}
        for session_id, fixture in fixtures.items():
            with self.subTest(session=session_id):
                self.writer = PlanWriter()
                results = await self.replay(fixture)
                spoken = []
                for item, result in zip(fixture["turns"], results):
                    self.assertFalse(repeats_recent(result["customerText"], spoken), result["customerText"])
                    spoken.append(result["customerText"])
                asked = [index for index, item in enumerate(fixture["turns"][:len(results)]) if "painLocationQuestion" in item["true"]]
                if asked and results[asked[0]]["playerResponseRating"] != "bad":
                    self.assertIn("pain_location", results[asked[0]]["disclosedFactIds"])
                if session_id.startswith("1c6d"):
                    raised = [(plan["concern"], plan["missingPart"], plan["hintLevel"]) for plan in self.writer.plans]
                    self.assertIn((1, "acknowledgment", 2), raised)
                    self.assertEqual(results[-1]["endingReason"], "stalemate")
                    self.assertGreater(results[-1]["remainingTurns"], 0)
                if session_id.startswith("6de0"):
                    self.assertEqual(results[0]["disclosedFactIds"], ["pain_location"])
                if session_id.startswith("25bf"):
                    self.assertEqual((len(results), results[-1]["endingReason"], results[-1]["remainingTurns"]),
                                     (4, "exchange_accepted", 4))
                    final = await self.complete()
                    self.assertEqual((final["score"], final["trustState"], final["resolutionAccepted"]),
                                     (20, "partially_restored", True))
                    self.assertFalse(final["causeIdentification"])


class InvariantTests(TurnPipelineHarness):
    """Deterministic label sequences from fixed seeds; no new dependency."""

    def random_labels(self, rng):
        true = [name for name in GOOD_LABELS if name not in ("retractsUnauthorizedRefund", "retractsUnauthorizedDiscount",
                "retractsUnauthorizedCompensation", "retractsAbsoluteGuarantee") and rng.random() < .13]
        true += [name for name in BAD_TURN_LABELS if rng.random() < .04]
        true += [name for name in VIOLATIONS if name != "managerEscalation" and rng.random() < .02]
        if "absoluteGuarantee" in true or rng.random() < .1:
            true.append(rng.choice(("retractsAbsoluteGuarantee", "maintainsUnauthorizedPromise")))
        uncertain = [rng.choice(("acknowledgment", "openQuestion", "exchangeConditions", "disrespect"))] if rng.random() < .06 else []
        return [name for name in true if name not in uncertain], uncertain

    async def test_invariants_hold_across_seeded_dialogues(self):
        for seed in range(30):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                self.writer = PlanWriter(rng.choice(("fallback", "fallback", "repeat", "fail")))
                await self.new_session(f"p{seed}")
                spoken, known, previous_objective = [], set(), 1
                for _ in range(8):
                    true, uncertain = self.random_labels(rng)
                    result = await self.turn(*true, uncertain=uncertain)
                    self.writer.previous = result["customerText"]
                    # 1. No committed line repeats one of the last three.
                    self.assertFalse(repeats_recent(result["customerText"], spoken), result["customerText"])
                    spoken.append(result["customerText"])
                    # 2. An unknown fact asked in a turn that is not bad is answered now.
                    asked = [fact for label, fact in FACT_QUESTIONS if label in true and fact not in known]
                    if asked and result["playerResponseRating"] != "bad":
                        self.assertIn(asked[0], result["disclosedFactIds"])
                    known |= set(result["disclosedFactIds"])
                    # 3. Unity's progression rule always holds.
                    objective = result["activeObjective"]
                    self.assertIn(objective - previous_objective, (0, 1))
                    self.assertEqual(objective > previous_objective, result["objectiveCompleted"])
                    previous_objective = objective
                    self.assertFalse((result["writerMetadata"].get("fallbackSelection") or {}).get("exhausted", False))
                    if result["conversationComplete"]:
                        break
                state = await self.state()
                ledger = state["evidenceLedger"]
                # 4. Three consecutive turns without progress on one concern end the dialogue.
                concern, run = 1, 0
                for index, entry in enumerate(ledger):
                    run = run + 1 if entry["noProgress"] else 0
                    if run == 3:
                        self.assertEqual(index, len(ledger) - 1)
                        self.assertEqual(state["endingReason"], "stalemate")
                    if entry["statedConcern"] != concern:
                        concern, run = entry["statedConcern"], 0
                # 5. Adding a good act never lowers the final score of a resolved ledger.
                resolved = copy.deepcopy(ledger)
                for entry in resolved:
                    for label in entry["labels"].values():
                        if label.get("status") == "uncertain":
                            label["status"] = "false"
                base = score_ledger(resolved, ending=state.get("endingReason"))["outcome"]["score"]
                for _ in range(6):
                    changed = copy.deepcopy(resolved)
                    entry = rng.choice(changed)
                    entry["labels"][rng.choice(GOOD_LABELS)] = {"status": "true", "noul": 1.0}
                    self.assertGreaterEqual(score_ledger(changed, ending=state.get("endingReason"))["outcome"]["score"], base)
                # 6. The completion outcome equals rescoring the same ledger.
                final = await self.complete()
                if final.get("completionStatus") == "completed":
                    rescored = score_session(final)["outcome"]
                    self.assertEqual({key: final[key] for key in rescored}, rescored)


class OfflineRescoreTests(TurnPipelineHarness):
    async def test_rescore_matches_completion_marks_old_versions_and_writes_nothing(self):
        await self.turn("acknowledgment", "openQuestion")
        await self.turn("walkingQuestion", "fitQuestion")
        await self.turn("causeStatement", "exchangeOffer", "exchangeConditions")
        completed = await self.complete()
        old = copy.deepcopy(completed)
        old.update(sessionId="oldversion", rubricVersion="sales-rubric-v2.5", score=0, trustState="lost")
        (self.directory / "sales-session-oldversion.json").write_text(json.dumps(old), encoding="utf-8")
        legacy = {"sessionId": "legacy", "pipelineMode": "legacy", "completedTurns": {}, "turns": []}
        (self.directory / "sales-session-legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
        before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.directory.iterdir() if path.is_file()}
        report = rescore_directory(self.directory, "sales-rubric-v3")
        after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.directory.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        records = {item["sessionId"]: item for item in report["sessions"]}
        current = records["s3"]
        self.assertFalse(current["hypothetical"])
        self.assertEqual(current["rescoredOutcome"]["score"], completed["score"])
        self.assertEqual(current["rescoredOutcome"]["trustState"], completed["trustState"])
        self.assertEqual(current["delta"], {"score": 0, "trustStateChanged": False, "customerRatingChanged": False})
        self.assertTrue(records["oldversion"]["hypothetical"])
        self.assertEqual(records["oldversion"]["delta"]["score"], completed["score"])
        self.assertEqual(report["skipped"], [{"sessionId": "legacy", "rubricVersion": None, "reason": "not_openrouter_pipeline"}])
        path = write_report(report, self.directory)
        self.assertEqual(path.parent.parent, self.directory / "evaluations")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["summary"], report["summary"])
        with self.assertRaises(ValueError):
            rescore_directory(self.directory, "sales-rubric-v2.5")


class CustomerLineTests(unittest.TestCase):
    STAGE_FACTS = {(1, "acknowledgment"): [], (1, "problem_question"): [], (2, "walking"): ["pain_location"],
                   (2, "fit"): ["walking_routine"], (2, "cause"): ["walking_routine", "fit_condition"]}

    def plan(self, **changes):
        return {"intent": "raise_concern", "allowedNewFacts": [], "knownFacts": [], "requiredFactIds": [],
                "requiredContent": [], "avoidReplyTexts": [], "ending": None, **changes}

    def test_every_backend_line_passes_the_writer_guard_alone_and_after_fact_answers(self):
        pairs = [["pain_location", "late_discomfort"], ["walking_routine", "lighter_preference"], ["fit_condition"]]
        for concern, parts in CONCERN_LINES.items():
            for part, levels in parts.items():
                known = self.STAGE_FACTS.get((concern, part), ["walking_routine", "fit_condition"])
                for line in (line for level in levels for line in level):
                    self.assertEqual(customer_text_rejections(line, self.plan(knownFacts=known)), [], line)
                    for facts in pairs:
                        combined = fact_sentence(facts) + " " + line
                        plan = self.plan(knownFacts=known, allowedNewFacts=facts, requiredFactIds=facts)
                        self.assertEqual(customer_text_rejections(combined, plan), [], combined)
        for action, lines in ACTION_LINES.items():
            content = {"warning": "respect_warning"}.get(action, action)
            for line in lines:
                self.assertEqual(customer_text_rejections(line, self.plan(requiredContent=[content])), [], line)
        for topic, lines in CLARIFY_LINES.items():
            content = ["clarify_refusal"] if topic == "refusal" else []
            for line in lines:
                self.assertEqual(customer_text_rejections(line, self.plan(requiredContent=content)), [], line)
        for key, lines in ENDING_LINES.items():
            content = "ending_lost" if key == "ending_lost_hostile" else key
            for line in lines:
                self.assertEqual(customer_text_rejections(line, self.plan(ending="turn_limit", requiredContent=[content])), [], line)
        for line in GENERIC_LINES:
            self.assertEqual(customer_text_rejections(line, self.plan()), [], line)
        for line in REPEATED_LINES:
            self.assertEqual(customer_text_rejections(line, self.plan(intent="repeated_question")), [], line)

    def test_pools_are_larger_than_the_repetition_window(self):
        for pool in (*ACTION_LINES.values(), CLARIFY_LINES["refusal"], *ENDING_LINES.values()):
            self.assertGreaterEqual(len(pool), 4)
        self.assertGreaterEqual(len(GENERIC_LINES), 4)
        for parts in CONCERN_LINES.values():
            for levels in parts.values():
                self.assertEqual(len(levels), 3)
                self.assertTrue(all(len(level) >= 3 for level in levels))


if __name__ == "__main__":
    unittest.main()
