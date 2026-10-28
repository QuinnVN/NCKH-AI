"""Adaptive Lan dialogue: opt-in, model-chosen actions inside code-owned limits.

The real AdaptiveLanResponder runs against httpx.MockTransport, so request
payloads, bounded attempts, guards, usage and cost capture are exercised
exactly as in production. STT and Jev are the existing scripted fakes.
"""
import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.config import get_settings
from app.sales_adaptive import (ACTIONS, ADAPTIVE_VERSION, SOFT_CLOSE_ENDING, AdaptiveLanResponder,
                                AdaptiveRequestConfig, build_payload, next_memory, output_rejections, parse_output,
                                sum_costs)
from app.sales_openrouter import FACTS, QwenSalesWriter
from app.sales_pipeline import complete, process_turn
from app.sales_returning_customer import CompletionRequest, ReturningSessionStore, public_session
from app.tests.sales_fixtures import write_part1
from app.tests.test_sales_pipeline_api import classification
from app.tests.test_sales_returning_customer import FakeTranscriber, request

LEGACY = SimpleNamespace(pipeline_mode="openrouter", sales_max_turns=8, sales_luna_arbitration_enabled=False,
                         sales_jev_timeout_seconds=2, sales_writer_timeout_seconds=2)
ADAPTIVE = SimpleNamespace(**vars(LEGACY), sales_adaptive_dialogue_enabled=True, sales_adaptive_reasoning="off",
                           sales_adaptive_max_tokens=4096, sales_adaptive_reasoning_max_tokens=1024,
                           sales_adaptive_timeout_seconds=45.0)
NEXT_STEPS = ("Vậy em có cách nào khác để giải quyết chuyện chân chị bị đau không?",
              "Em nói thử xem bên mình kiểm tra độ vừa cho chị theo cách nào được không?",
              "Chị muốn biết em định xử lý tiếp chuyện đôi giày này ra sao?")
CLOSE = "Thôi, chị dừng ở đây, chị về trước vì chuyện này em chưa giải quyết được cho chị."


def body(content, *, cost=0.0002, reasoning=0, provider="Alibaba"):
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return {"id": "gen-test", "model": "qwen/qwen3.7-flash", "provider": provider,
            "choices": [{"finish_reason": "stop", "message": {"content": text}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 80 + reasoning, "total_tokens": 980 + reasoning,
                      "completion_tokens_details": {"reasoning_tokens": reasoning},
                      "prompt_tokens_details": {"cached_tokens": 0}, "cost": cost}}


class ScriptedModel:
    """Plays the model: a strategy maps (context, attempt) to a response."""

    def __init__(self, strategy):
        self.strategy, self.payloads, self.contexts = strategy, [], []

    def handler(self, request_: httpx.Request) -> httpx.Response:
        payload = json.loads(request_.content)
        self.payloads.append(payload)
        context = json.loads(next(m["content"] for m in payload["messages"] if m["role"] == "user"))
        self.contexts.append(context)
        result = self.strategy(context, len(payload["messages"]) - 2, len(self.payloads))
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=body(result))

    def responder(self, config=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return AdaptiveLanResponder(replace(get_settings(), openrouter_api_key="test-key"), client=client,
                                    config=config or AdaptiveRequestConfig(timeout_seconds=5))


def next_step(context, repairs, calls):
    action = "request_next_step" if "request_next_step" in context["allowedActions"] else context["allowedActions"][0]
    return {"action": action, "decision": "continue", "closeReason": None,
            "customerText": NEXT_STEPS[(calls - 1) % len(NEXT_STEPS)], "factIds": context["facts"]["mustAnswerFactIds"]}


def close_when_allowed(context, repairs, calls):
    if context["limits"]["closeAllowed"]:
        return {"action": "close_conversation", "decision": "close", "closeReason": "unresolved_frustration",
                "customerText": CLOSE, "factIds": []}
    return next_step(context, repairs, calls)


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = ReturningSessionStore(self.directory)
        self.stt = FakeTranscriber("Dạ, em muốn hỏi chị thêm một chút.")
        self.index = 0

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def session(self, session_id, settings):
        attempt = write_part1(self.directory, session_id, "run-" + session_id)
        with patch("app.sales_returning_customer.get_settings", return_value=settings):
            await self.store.create_or_resume(session_id, attempt, run_id="run-" + session_id)
        return session_id

    async def turn(self, session_id, *true, responder=None, writer=None, turn_id=None):
        self.index += 1

        class Classifier:
            async def classify(self, transcript, context):
                return classification(*true)

        return await process_turn(session_id, request(turn_id or f"t{self.index}"), store=self.store,
                                  transcriber=self.stt, classifier=Classifier(), writer=writer, responder=responder)


class ExplodingResponder:
    async def respond(self, messages, validate):
        raise AssertionError("adaptive responder must not run for a legacy session")


class LegacyWriter:
    def __init__(self):
        self.calls = 0

    async def write(self, plan, transcript, history):
        self.calls += 1
        return {"customerText": plan["fallbackText"], "metadata": {}, "fallbackUsed": False}


class DefaultLegacyTests(Harness):
    def test_settings_default_to_legacy(self):
        settings = get_settings()
        self.assertFalse(settings.sales_adaptive_dialogue_enabled)
        self.assertEqual((settings.sales_adaptive_reasoning, settings.sales_adaptive_max_tokens,
                          settings.sales_adaptive_reasoning_max_tokens, settings.sales_adaptive_timeout_seconds),
                         ("off", 4096, 1024, 45.0))
        self.assertEqual(settings.sales_writer_timeout_seconds, 4.0)

    async def test_legacy_session_never_calls_adaptive_responder(self):
        sid = await self.session("legacy", LEGACY)
        writer = LegacyWriter()
        result = await self.turn(sid, "refusesRemedy", responder=ExplodingResponder(), writer=writer)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(writer.calls, 1)
        self.assertNotIn("adaptiveAction", result)
        state = await self.store.get(sid)
        self.assertNotIn("adaptiveDialogue", state)
        self.assertNotIn("adaptiveMemory", state)

    async def test_legacy_writer_request_is_unchanged(self):
        seen = []

        def handler(request_):
            seen.append(json.loads(request_.content))
            return httpx.Response(200, json=body("Chị vẫn chưa hiểu sao đôi giày này lại làm chị khổ như vậy."))

        writer = QwenSalesWriter(replace(get_settings(), openrouter_api_key="k"),
                                 client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        plan = {"intent": "raise_concern", "concern": 2, "missingPart": "walking", "hintLevel": 0, "knownFacts": [],
                "allowedNewFacts": [], "requiredFactIds": [], "requiredContent": [], "factTexts": {},
                "avoidReplyTexts": [], "fallbackText": "Chị muốn biết vì sao mang đôi này lại khó chịu đến thế."}
        await writer.write(plan, "Dạ em chào chị.", [])
        self.assertEqual((seen[0]["max_tokens"], seen[0]["reasoning"]), (220, {"enabled": False}))
        self.assertNotIn("response_format", seen[0])

    async def test_frozen_flag_survives_setting_changes(self):
        adaptive = await self.session("frozen-on", ADAPTIVE)
        legacy = await self.session("frozen-off", LEGACY)
        state = await self.store.get(adaptive)
        self.assertEqual(state["adaptiveDialogue"]["version"], ADAPTIVE_VERSION)
        self.assertEqual((state["adaptiveDialogue"]["maxTokens"], state["adaptiveDialogue"]["timeoutSeconds"]), (4096, 45.0))
        model = ScriptedModel(next_step)
        with patch("app.config.get_settings", return_value=replace(get_settings(), sales_adaptive_dialogue_enabled=False)):
            result = await self.turn(adaptive, "refusesRemedy", responder=model.responder())
        self.assertEqual(result["adaptiveAction"]["source"], "model")
        result = await self.turn(legacy, "refusesRemedy", responder=ExplodingResponder(), writer=LegacyWriter())
        self.assertNotIn("adaptiveAction", result)
        self.assertNotIn("adaptiveMemory", public_session(await self.store.get(adaptive)))


class AdaptiveChoiceTests(Harness):
    async def test_model_choice_replaces_fixed_refusal_intent(self):
        sid = await self.session("choice", ADAPTIVE)
        model = ScriptedModel(next_step)
        result = await self.turn(sid, "refusesRemedy", responder=model.responder())
        self.assertEqual(result["adaptiveAction"]["legacyIntent"], "refusal_challenge")
        self.assertEqual(result["adaptiveAction"]["action"], "request_next_step")
        self.assertEqual(result["customerText"], NEXT_STEPS[0])
        self.assertFalse(result["fallbackUsed"])
        # Same model, same prompt: action and reply come from one request.
        self.assertEqual(len(model.payloads), 1)
        self.assertEqual(model.payloads[0]["response_format"], {"type": "json_object"})
        self.assertEqual(model.payloads[0]["reasoning"], {"enabled": False})
        context = model.contexts[0]
        for private in ("score", "turnQuality", "rubricComponents", "noul", "sessionId"):
            self.assertNotIn(private, json.dumps(context))

    async def test_repeated_pushback_is_removed_from_allowed_actions(self):
        sid = await self.session("loop", ADAPTIVE)

        def challenge(context, repairs, calls):
            if "challenge_refusal" in context["allowedActions"]:
                text = ("Em từ chối đổi thì chuyện chân chị đau ai lo?", "Sao em cứ nói không đổi mà không đưa cách gì?")[calls - 1]
                return {"action": "challenge_refusal", "decision": "continue", "closeReason": None,
                        "customerText": text, "factIds": []}
            return close_when_allowed(context, repairs, calls)

        model = ScriptedModel(challenge)
        first = await self.turn(sid, "refusesRemedy", responder=model.responder())
        second = await self.turn(sid, "refusesRemedy", responder=model.responder())
        self.assertEqual([first["adaptiveAction"]["action"], second["adaptiveAction"]["action"]],
                         ["challenge_refusal", "challenge_refusal"])
        third = await self.turn(sid, "refusesRemedy", responder=model.responder())
        self.assertNotIn("challenge_refusal", model.contexts[-1]["allowedActions"])
        self.assertEqual(third["adaptiveAction"]["action"], "close_conversation")
        self.assertTrue(third["conversationComplete"])
        self.assertEqual(third["endingReason"], SOFT_CLOSE_ENDING)

    async def test_early_close_changes_ending_but_not_deterministic_score(self):
        legacy, adaptive = await self.session("score-legacy", LEGACY), await self.session("score-adaptive", ADAPTIVE)
        model = ScriptedModel(close_when_allowed)
        script = [("acknowledgment", "openQuestion"), ("refusesRemedy",), ("refusesRemedy",)]
        for number, labels in enumerate(script, 1):
            await self.turn(legacy, *labels, writer=LegacyWriter(), turn_id=f"s{number}")
            result = await self.turn(adaptive, *labels, responder=model.responder(), turn_id=f"s{number}")
        self.assertEqual(result["endingReason"], SOFT_CLOSE_ENDING)
        self.assertEqual(result["adaptiveAction"]["closeReason"], "unresolved_frustration")
        before = [copy.deepcopy(state["evidenceLedger"]) for state in (await self.store.get(legacy), await self.store.get(adaptive))]
        strip = lambda ledger: [{key: entry[key] for key in ("labels", "turnQuality", "disclosedFactIds", "statedConcern",
                                                             "hintLevel", "noProgress", "factsKnownBefore")} for entry in ledger]
        self.assertEqual(strip(before[0]), strip(before[1]))
        with self.assertRaises(RuntimeError):
            await self.turn(adaptive, "acknowledgment", responder=model.responder())
        done = [await complete(sid, CompletionRequest(completionId="c", reason="natural"), store=self.store)
                for sid in (legacy, adaptive)]
        for key in ("score", "rawScore", "criterionScores", "rubricComponents", "policyViolations", "trustState",
                    "customerRating", "completedObjectives"):
            self.assertEqual(done[0][key], done[1][key], key)
        self.assertEqual((done[0]["endingReason"], done[1]["endingReason"]), ("stopped_early", SOFT_CLOSE_ENDING))
        self.assertEqual(done[1]["finalCustomerText"], CLOSE)

    async def test_close_is_rejected_when_code_does_not_allow_it(self):
        sid = await self.session("no-close", ADAPTIVE)

        def always_close(context, repairs, calls):
            return {"action": "close_conversation", "decision": "close", "closeReason": "unresolved_frustration",
                    "customerText": CLOSE, "factIds": []}

        model = ScriptedModel(always_close)
        result = await self.turn(sid, "acknowledgment", responder=model.responder())
        self.assertFalse(result["conversationComplete"])
        self.assertTrue(result["fallbackUsed"])
        self.assertEqual(result["adaptiveAction"]["source"], "fallback")
        self.assertEqual(len(model.payloads), 2)  # one bounded repair, then fallback
        attempts = result["writerMetadata"]["attempts"]
        self.assertIn("action_not_allowed", attempts[0]["rejectionCodes"])
        self.assertEqual(attempts[1]["kind"], "repair")
        self.assertEqual(result["writerMetadata"]["costUsd"], 0.0004)


class HardTerminalTests(Harness):
    async def test_deterministic_ending_is_authoritative(self):
        sid = await self.session("manager", ADAPTIVE)
        model = ScriptedModel(next_step)  # tries to keep talking
        result = await self.turn(sid, "managerEscalation", responder=model.responder())
        self.assertTrue(result["conversationComplete"])
        self.assertEqual(result["endingReason"], "manager_escalation")
        self.assertEqual(result["adaptiveAction"]["source"], "fallback")
        self.assertEqual(model.contexts[0]["allowedActions"], ["close_conversation"])
        self.assertIn("must_close", result["writerMetadata"]["attempts"][0]["rejectionCodes"])

    async def test_model_can_word_a_deterministic_ending(self):
        sid = await self.session("manager-text", ADAPTIVE)

        def closing(context, repairs, calls):
            return {"action": "close_conversation", "decision": "close", "closeReason": "deterministic_ending",
                    "customerText": "Thôi, chị dừng ở đây, chuyện này vẫn chưa giải quyết được.", "factIds": []}

        result = await self.turn(sid, "managerEscalation", responder=ScriptedModel(closing).responder())
        self.assertEqual((result["endingReason"], result["adaptiveAction"]["source"]), ("manager_escalation", "model"))


class GuardTests(unittest.TestCase):
    PLAN = {"intent": "raise_concern", "knownFacts": ["complaint", "purchase_history", "prior_advice"],
            "allowedNewFacts": ["pain_location"], "requiredFactIds": ["pain_location"],
            "factTexts": {fact: FACTS[fact] for fact in ("complaint", "purchase_history", "prior_advice", "pain_location")},
            "avoidReplyTexts": [], "requiredContent": [], "fallbackText": "x"}
    POLICY = {"allowedActions": ["answer_question", "raise_concern", "request_next_step"], "deterministicEnding": None,
              "endingTone": None, "closeAllowed": False, "mustVoiceTrustDoubt": False,
              "requiredFactIds": ["pain_location"],
              "citableFactIds": ["complaint", "purchase_history", "prior_advice", "pain_location"]}

    def check(self, **change):
        output = {"action": "answer_question", "decision": "continue", "closeReason": None, "factIds": ["pain_location"],
                  "customerText": "Chị đau ở gót chân khi đi được một lúc. Em tính giúp chị thế nào?"}
        output.update(change)
        return output_rejections(output, self.POLICY, self.PLAN, "Chị đau ở đâu ạ?")

    def test_valid_output_passes(self):
        self.assertEqual(self.check(), [])

    def test_schema_rejects(self):
        self.assertEqual(parse_output("not json")[1], ["invalid_json"])
        self.assertIn("invalid_action", parse_output(json.dumps({"action": "refund", "decision": "continue",
                                                                 "customerText": "Chị", "factIds": []}))[1])
        self.assertIn("invalid_fact_ids", parse_output(json.dumps({"action": "raise_concern", "decision": "continue",
                                                                   "customerText": "Chị", "factIds": ["shoe_size"]}))[1])
        parsed, codes = parse_output("```json\n" + json.dumps({"action": "raise_concern", "decision": "continue",
                                                                "closeReason": None, "customerText": " Chị ",
                                                                "factIds": []}) + "\n```")
        self.assertEqual((codes, parsed["customerText"]), ([], "Chị"))
        self.assertEqual(set(ACTIONS) >= {"close_conversation", "request_next_step"}, True)

    def test_fact_and_policy_rejections(self):
        self.assertIn("missing_cited_fact", self.check(factIds=[]))
        self.assertIn("uncitable_fact", self.check(factIds=["pain_location", "socks"]))
        self.assertIn("unallowed_fact", self.check(customerText="Chị đau ở gót chân khi đi được một lúc, chị thường mang tất mỏng."))
        self.assertIn("policy_or_instruction", self.check(customerText="Chị đau ở gót chân khi đi được một lúc. Em hoàn tiền cho chị đi."))
        self.assertIn("action_not_allowed", self.check(action="reject_promise"))
        self.assertIn("decision_action_mismatch", self.check(decision="close"))

    def test_no_fabricated_store_actions(self):
        for text in ("Chị đau ở gót chân khi đi được một lúc. Quản lý đã đồng ý cho chị đổi rồi.",
                     "Chị đau ở gót chân khi đi được một lúc. Em đã đổi cho chị rồi mà.",
                     "Chị đau ở gót chân khi đi được một lúc. Chị đã thử đôi mới rồi, thấy vừa lắm."):
            codes = self.check(customerText=text)
            self.assertTrue({"fabricated_store_action", "unsupported_completion", "physical_action"} & set(codes), (text, codes))
        # Asking for an exchange is allowed.
        self.assertEqual(self.check(customerText="Chị đau ở gót chân khi đi được một lúc. Em đổi giúp chị đôi khác được không?"), [])


class ResponderUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_attempt_makes_total_cost_unknown(self):
        valid = {"action": "raise_concern", "decision": "continue", "closeReason": None,
                 "customerText": "Chị vẫn chưa hiểu sao đôi này làm chị đau.", "factIds": []}
        responses = iter([httpx.Response(503, json={}), httpx.Response(200, json=body(valid, cost=0.0003, reasoning=150))])
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request_: next(responses)))
        responder = AdaptiveLanResponder(replace(get_settings(), openrouter_api_key="k"), client=client,
                                         config=AdaptiveRequestConfig(reasoning="on", timeout_seconds=5))
        result = await responder.respond([{"role": "user", "content": "{}"}], lambda output: [])
        self.assertIsNotNone(result["output"])
        self.assertEqual([attempt["kind"] for attempt in result["attempts"]], ["initial", "retry"])
        self.assertEqual(result["attempts"][0]["errorCode"], "openrouter_http_503")
        self.assertIsNone(result["costUsd"])
        self.assertEqual(result["costMissingAttempts"], 1)
        self.assertEqual(result["attempts"][1]["usage"]["reasoningTokens"], 150)

    async def test_timeout_is_bounded_and_cost_unknown(self):
        async def slow(request_):
            await asyncio.sleep(5)

        class SlowTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request_):
                await slow(request_)

        responder = AdaptiveLanResponder(replace(get_settings(), openrouter_api_key="k"),
                                         client=httpx.AsyncClient(transport=SlowTransport()),
                                         config=AdaptiveRequestConfig(timeout_seconds=0.2))
        result = await responder.respond([{"role": "user", "content": "{}"}], lambda output: [])
        self.assertIsNone(result["output"])
        self.assertIn(result["errorCode"], ("adaptive_timeout", "openrouter_timeout"))
        self.assertLess(result["durationSeconds"], 1.5)
        self.assertIsNone(result["costUsd"])

    def test_cost_sum_and_null(self):
        attempt = lambda cost: {"usage": {"costUsd": cost}}
        self.assertAlmostEqual(sum_costs([attempt(0.1), attempt(0.2)]), 0.3)
        self.assertIsNone(sum_costs([attempt(0.1), attempt(None)]))
        self.assertIsNone(sum_costs([]))

    def test_payloads_differ_only_in_reasoning(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "{}"}]
        provider = {"order": ["alibaba"], "allow_fallbacks": False, "data_collection": "deny"}
        off = build_payload(messages, AdaptiveRequestConfig(reasoning="off"), provider)
        on = build_payload(messages, AdaptiveRequestConfig(reasoning="on"), provider)
        self.assertEqual({key: value for key, value in off.items() if key != "reasoning"},
                         {key: value for key, value in on.items() if key != "reasoning"})
        self.assertEqual((off["reasoning"], on["reasoning"]), ({"enabled": False}, {"enabled": True, "max_tokens": 1024}))
        self.assertEqual(off["max_tokens"], 4096)


class MemoryCheckpointTests(Harness):
    async def test_paid_stage_and_memory_are_idempotent(self):
        sid = await self.session("ckpt", ADAPTIVE)
        model = ScriptedModel(next_step)
        original = self.store.finalize_turn
        crashed = []

        async def flaky(*args, **kwargs):
            if not crashed:
                crashed.append(True)
                raise RuntimeError("disk full")
            return await original(*args, **kwargs)

        with patch.object(self.store, "finalize_turn", flaky):
            failed = await self.turn(sid, "refusesRemedy", responder=model.responder(), turn_id="same")
        self.assertEqual(failed["status"], "failed")
        state = await self.store.get(sid)
        self.assertNotIn("adaptiveMemory", state)
        self.assertIn("adaptive", state["turnCheckpoints"]["same"])
        result = await self.turn(sid, "refusesRemedy", responder=model.responder(), turn_id="same")
        again = await self.turn(sid, "refusesRemedy", responder=model.responder(), turn_id="same")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(again["customerText"], result["customerText"])
        self.assertEqual(len(model.payloads), 1)
        memory = (await self.store.get(sid))["adaptiveMemory"]
        self.assertEqual([item["action"] for item in memory["history"]], ["request_next_step"])

    def test_memory_is_bounded(self):
        memory = None
        for index in range(12):
            memory = next_memory(memory, {"source": "model", "output": {"action": "raise_concern", "decision": "continue",
                                                                         "factIds": []}}, index)
        self.assertEqual(len(memory["history"]), 8)
        self.assertEqual(memory["history"][-1]["turn"], 11)
