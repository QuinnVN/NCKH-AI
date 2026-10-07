"""Offline HTTP-contract and failure tests. No secrets or paid requests."""
import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from app.config import get_settings
from app.sales_customer_lines import choose_customer_line
from app.sales_openrouter import (
    CHAT_ENDPOINT, DECISIONS_ENDPOINT, FACTS, JEV_MODEL, PENALTY_LABELS,
    QUESTIONS, QWEN_MODEL, JevSalesClassifier, QwenSalesWriter,
    SalesClassifierError, customer_text_rejections, validate_customer_text,
)


def settings(**changes):
    with patch.dict("os.environ", {}, clear=True):
        return replace(get_settings(), openrouter_api_key="test-only-secret", **changes)


def decisions(**overrides):
    answers = {name: {"type": "noul", "noul": 0.05} for name in QUESTIONS}
    answers.update(overrides)
    return {"answers": answers, "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
            "id": "decision-1", "usage": {"input_tokens": 12, "output_tokens": 4, "cost": 0.002}}


def completion(text, **extra):
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "model": QWEN_MODEL, "provider": "test", "id": "writer-1",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": 0.003}, **extra}


PLAN = {"fallbackText": "Chị vẫn chưa hiểu rõ, em giải thích thêm giúp chị nhé.",
        "allowedNewFacts": [], "knownFacts": [], "requiredPhrases": [],
        "intent": "clarification", "emotion": "cautious", "question": "", "ending": None}


class ConfigurationTests(unittest.TestCase):
    def test_natural_exchange_fit_and_lost_wording_passes_content_guard(self):
        for text, content, ending in (
            ("Chị đồng ý đổi. Em đã hỏi chị thường đi bộ bao lâu chưa?", [], None),
            ("Chị chấp nhận đổi. Em đã xem đôi này có bị chật hoặc hỏng không?", [], None),
            ("Chị không chấp nhận cách tư vấn này. Chị muốn dừng trao đổi ở đây.", ["ending_lost"], "turn_limit")):
            self.assertEqual(customer_text_rejections(text, {**PLAN, "requiredContent": content, "ending": ending}), [])

    def test_same_exchange_prefix_is_rejected_across_recent_reply_history(self):
        history = ["Đổi cho chị thì được, nhưng chị chưa yên tâm. Em đã hỏi chị về nhu cầu đi bộ chưa?",
                   "Chị đi bộ từ bến xe đến trường và giữa các lớp mỗi ngày."]
        candidate = "Đổi cho chị thì được, nhưng chị chưa yên tâm. Em sẽ kiểm tra độ vừa thế nào?"
        plan = {**PLAN, "avoidReplyTexts": history, "knownFacts": ["walking_routine"]}
        self.assertIn("repeated_reply", customer_text_rejections(candidate, plan))

    def test_changing_prefix_does_not_allow_repeated_customer_question(self):
        previous = "Chị vẫn lo. Em định kiểm tra đôi giày này thế nào?"
        candidate = "Đổi cho chị thì được. Em định kiểm tra đôi giày này thế nào?"
        self.assertIn("repeated_reply", customer_text_rejections(candidate, {**PLAN, "avoidReplyText": previous}))

    def test_administrative_customer_prose_is_rejected_but_natural_exchange_is_valid(self):
        self.assertIn("administrative_tone", customer_text_rejections("Chị ghi nhận lời em, em nói tiếp nhé.", PLAN))
        self.assertEqual(customer_text_rejections("Đổi cho chị thì được, nhưng em định kiểm tra đôi giày này thế nào?", PLAN), [])

    def test_soft_actions_no_longer_require_keywords(self):
        self.assertIn("false_already_answered", customer_text_rejections(
            "Chị đã nói rồi, nhưng em vẫn chưa hỏi chị dùng giày để làm gì.", PLAN))
        self.assertEqual(customer_text_rejections("Chị đã nói rồi mà em.", {**PLAN, "intent": "repeated_question"}), [])
        self.assertEqual(customer_text_rejections("Chị kể rồi, vậy em thấy vấn đề là gì?",
                                                  {**PLAN, "knownFacts": ["walking_routine"]}), [])
        for requirement in ("acknowledge_exchange", "investigate_walking", "investigate_fit", "investigate_complaint",
                            "explain_cause", "answer_opening_complaint"):
            self.assertIn("missing_required_content", customer_text_rejections(
                "Chị vẫn còn lo về đôi giày này lắm.", {**PLAN, "requiredContent": [requirement]}))
        self.assertEqual(customer_text_rejections("Chị vẫn còn lo về đôi giày này lắm.", {**PLAN, "requiredContent": []}), [])

    def test_rollout_remains_legacy_and_sales_does_not_change_general_llm(self):
        with patch.dict("os.environ", {"SALES_PIPELINE_MODE": "unexpected", "SALES_MAX_TURNS": "99",
                                      "SALES_JEV_TIMEOUT_SECONDS": "0", "OPENROUTER_API_KEY": "secret"}, clear=True):
            config = get_settings()
        self.assertEqual(config.sales_pipeline_mode, "legacy")
        self.assertEqual(config.sales_max_turns, 32)
        self.assertEqual(config.sales_jev_timeout_seconds, 0.1)
        self.assertEqual(config.llm_base_url, "http://127.0.0.1:8080/v1")
        self.assertNotIn("secret", repr(config))

    def test_schema_has_independent_promises_and_never_invents_derived_labels(self):
        self.assertEqual(len(QUESTIONS), 30)
        self.assertTrue({"unauthorizedRefund", "unauthorizedDiscount", "unauthorizedCompensation",
                         "absoluteGuarantee", "maintainsUnauthorizedPromise"}.issubset(QUESTIONS))
        self.assertNotIn("unauthorizedPromise", QUESTIONS)
        self.assertNotIn("apologyOnly", QUESTIONS)
        self.assertNotIn("policyExchange", QUESTIONS)


class ClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_quality_labels_use_ordinary_thresholds_without_lowering_penalty_thresholds(self):
        body = decisions(disrespect={"type": "noul", "noul": .82},
                         abuse={"type": "noul", "noul": .82},
                         refusesRemedy={"type": "noul", "noul": .9})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            result = await JevSalesClassifier(settings(), client=client).classify("test", {})
        self.assertEqual(result["labels"]["disrespect"]["status"], "true")
        self.assertEqual(result["labels"]["refusesRemedy"]["status"], "true")
        self.assertEqual(result["labels"]["abuse"]["status"], "uncertain")
    async def test_decisions_contract_privacy_scope_and_metadata(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, json=decisions(acknowledgment={"type": "noul", "noul": 0.83},
                                                     unauthorizedRefund={"type": "noul", "noul": 0.85}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await JevSalesClassifier(settings(sales_openrouter_zdr=True), client=client).classify(
                "Em xin lỗi chị.", {"phase": 1, "realName": "PRIVATE", "runId": "PRIVATE-ID", "audio": "PRIVATE-AUDIO",
                                      "history": [{"player": f"past-{i}", "lan": "context"} for i in range(5)]})
        request = captured[0]
        self.assertEqual(str(request.url), DECISIONS_ENDPOINT)
        self.assertEqual(request.headers["authorization"], "Bearer test-only-secret")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], JEV_MODEL)
        self.assertEqual(payload["provider"], {"data_collection": "deny", "zdr": True})
        self.assertEqual(len(payload["state"]["prior_dialogue"]), 3)
        self.assertNotIn("PRIVATE", json.dumps(payload))
        self.assertEqual(result["labels"]["acknowledgment"]["status"], "true")
        self.assertEqual(result["labels"]["acknowledgment"]["noul"], 0.83)
        self.assertEqual(result["labels"]["unauthorizedRefund"]["status"], "uncertain")
        self.assertEqual(result["metadata"]["servedModel"], "typesafe/jev-1.13-20260917")
        self.assertEqual(result["metadata"]["costUsd"], 0.002)

    async def test_missing_malformed_and_out_of_domain_answers_remain_uncertain(self):
        body = decisions(acknowledgment={"type": "noul", "noul": True},
                         abuse={"type": "noul", "noul": 1.2},
                         unauthorizedRefund={"type": "noul", "noul": "0.99"},
                         exchangeOffer={"type": "choice", "noul": 0.99})
        body["answers"].pop("openQuestion")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            result = await JevSalesClassifier(settings(), client=client).classify("đổi quản lý đau", {})
        for name in ("acknowledgment", "abuse", "unauthorizedRefund", "exchangeOffer", "openQuestion"):
            self.assertEqual(result["labels"][name]["status"], "uncertain")
            self.assertIsNone(result["labels"][name]["noul"])

    async def test_exact_normal_and_penalty_thresholds(self):
        body = decisions(acknowledgment={"type": "noul", "noul": 0.8},
                         openQuestion={"type": "noul", "noul": 0.2},
                         unauthorizedRefund={"type": "noul", "noul": 0.9},
                         unauthorizedDiscount={"type": "noul", "noul": 0.1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            result = await JevSalesClassifier(settings(), client=client).classify("test", {})
        self.assertEqual([result["labels"][name]["status"] for name in (
            "acknowledgment", "openQuestion", "unauthorizedRefund", "unauthorizedDiscount")],
            ["true", "false", "true", "false"])

    async def test_huge_json_numbers_and_nonfinite_usage_stay_inside_adapter_boundary(self):
        body = decisions(acknowledgment={"type": "noul", "noul": 10 ** 400})
        body["usage"] = {"cost": 10 ** 400, "input_tokens": 10 ** 400, "output_tokens": 3}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            result = await JevSalesClassifier(settings(), client=client).classify("Em xin lỗi chị.", {})
        self.assertEqual(result["labels"]["acknowledgment"]["status"], "uncertain")
        self.assertIsNone(result["labels"]["acknowledgment"]["noul"])
        self.assertIsNone(result["metadata"]["costUsd"])
        self.assertEqual(result["metadata"]["usage"], {"output_tokens": 3})

    async def test_typed_retractions_are_independent_ordinary_observations(self):
        names = ("retractsUnauthorizedRefund", "retractsUnauthorizedDiscount", "retractsUnauthorizedCompensation", "retractsAbsoluteGuarantee")
        body = decisions(**{name: {"type": "noul", "noul": 0.8 if name == names[0] else 0.2} for name in names})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            result = await JevSalesClassifier(settings(), client=client).classify("Em rút lại lời hứa hoàn tiền.", {})
        self.assertEqual([result["labels"][name]["status"] for name in names], ["true", "false", "false", "false"])
        self.assertFalse(set(names) & PENALTY_LABELS)

    async def test_errors_are_safe_and_do_not_retry(self):
        for status in (401, 402, 403, 404, 429, 500, 503):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"error": {"message": "test-only-secret sensitive transcript"}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(SalesClassifierError) as caught:
                    await JevSalesClassifier(settings(), client=client).classify("test", {})
            self.assertEqual(str(caught.exception), f"openrouter_http_{status}")
            self.assertEqual(len(calls), 1)

    async def test_total_deadline_applies_even_to_dribbling_transport(self):
        async def handler(request):
            await asyncio.sleep(0.2)
            return httpx.Response(200, json=decisions())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(SalesClassifierError) as caught:
                await JevSalesClassifier(settings(sales_jev_timeout_seconds=0.02), client=client).classify("test", {})
        self.assertEqual(caught.exception.code, "openrouter_timeout")

    async def test_missing_key_and_old_schema_do_not_become_negative_evidence(self):
        with self.assertRaises(SalesClassifierError):
            await JevSalesClassifier(replace(settings(), openrouter_api_key=None)).classify("test", {})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"acknowledgment": True}))) as client:
            with self.assertRaises(SalesClassifierError):
                await JevSalesClassifier(settings(), client=client).classify("test", {})


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_followup_is_rewritten_and_records_attempt_rejections(self):
        previous = "Đổi cho chị thì được, nhưng em định kiểm tra đôi giày này thế nào?"
        revised = "Chị đã nghe em đề nghị đổi giày. Trước khi quyết định, em còn muốn hỏi chị điều gì?"
        replies = iter([previous, revised])
        plan = {**PLAN, "intent": "raise_concern", "avoidReplyText": previous,
                "fallbackText": revised, "requiredContent": []}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=completion(next(replies))))) as client:
            result = await QwenSalesWriter(settings(), client=client).write(plan, "Dạ", [{"lan": previous}])
        self.assertFalse(result["fallbackUsed"])
        self.assertEqual(result["customerText"], revised)
        self.assertEqual(result["metadata"]["attempts"][0]["rejectionCodes"], ["repeated_reply"])
        self.assertEqual(result["metadata"]["attempts"][1]["rejectionCodes"], [])

    async def test_short_writer_disables_reasoning_to_leave_tokens_for_speech(self):
        def handler(request):
            payload = json.loads(request.content)
            if payload.get("reasoning") != {"enabled": False}:
                return httpx.Response(200, json=completion("", usage={"completion_tokens": 220,
                    "completion_tokens_details": {"reasoning_tokens": 220}}))
            return httpx.Response(200, json=completion("Chị hiểu ý em, em nói tiếp nhé."))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await QwenSalesWriter(settings(), client=client).write(PLAN, "Dạ", [])
        self.assertFalse(result["fallbackUsed"])

    async def test_reasoning_only_response_has_diagnostics_without_reasoning_text(self):
        body = completion("", usage={"completion_tokens": 220,
            "completion_tokens_details": {"reasoning_tokens": 220}})
        body["choices"][0].update(finish_reason="length")
        body["choices"][0]["message"]["reasoning"] = "private reasoning"
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            result = await QwenSalesWriter(settings(), client=client).write(PLAN, "Dạ", [])
        attempt = result["metadata"]["attempts"][0]
        self.assertEqual(attempt["finishReason"], "length")
        self.assertEqual(attempt["reasoningTokens"], 220)
        self.assertEqual(attempt["contentChars"], 0)
        self.assertNotIn("private reasoning", json.dumps(result))

    async def test_writer_plan_privacy_and_history_have_no_assessment_authority(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, json=completion("Chị hiểu ý em rồi, em giải thích bước tiếp theo nhé."))
        plan = {**PLAN, "score": 99, "runId": "PRIVATE", "actualCause": "PRIVATE", "labels": {"abuse": True}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await QwenSalesWriter(settings(), client=client).write(plan, "Dạ", [{"player": "dạ", "lan": "ừ"}] * 6)
        payload = json.loads(captured[0].content)
        self.assertEqual(str(captured[0].url), CHAT_ENDPOINT)
        self.assertEqual(payload["model"], QWEN_MODEL)
        self.assertEqual(payload["provider"]["data_collection"], "deny")
        self.assertFalse(payload["stream"])
        self.assertNotIn("PRIVATE", payload["messages"][1]["content"])
        self.assertNotIn("score", payload["messages"][1]["content"])
        self.assertEqual(len(json.loads(payload["messages"][1]["content"])["recentDialogue"]), 3)
        self.assertFalse(result["fallbackUsed"])

    async def test_one_rewrite_then_safe_fallback_and_cost_sum(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, json=completion("Chị yêu cầu còn tem và hoàn tiền cho chị."))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await QwenSalesWriter(settings(), client=client).write(PLAN, "Dạ", [])
        self.assertEqual(len(captured), 2)
        self.assertEqual(result["customerText"], choose_customer_line(PLAN, "Dạ"))
        self.assertTrue(result["fallbackUsed"])
        self.assertEqual(result["metadata"]["costUsd"], 0.006)
        self.assertNotIn("còn tem", captured[1].content.decode())

    async def test_successful_rewrite_keeps_its_cost(self):
        responses = iter([completion("rubric"), completion("Chị hiểu ý em, chị muốn nghe cách xử lý tiếp theo.")])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(responses)))) as client:
            result = await QwenSalesWriter(settings(), client=client).write(PLAN, "Dạ", [])
        self.assertFalse(result["fallbackUsed"])
        self.assertEqual(len(result["metadata"]["attempts"]), 2)

    async def test_writer_total_deadline_does_not_reset_on_rewrite(self):
        calls = []
        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                await asyncio.sleep(0.2)
                return httpx.Response(200, json=completion("rubric"))
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await QwenSalesWriter(settings(sales_writer_timeout_seconds=0.8), client=client).write(PLAN, "Dạ", [])
        self.assertTrue(result["fallbackUsed"])
        self.assertEqual(result["metadata"]["errorCode"], "openrouter_timeout")
        self.assertEqual(len(calls), 2)
        # The retry receives the unspent part of the same budget. This invariant
        # is independent of exact Windows timer resolution or elapsed milliseconds.
        first_budget = calls[0].extensions["timeout"]["read"]
        second_budget = calls[1].extensions["timeout"]["read"]
        self.assertLess(second_budget, first_budget - 0.1)

    async def test_huge_usage_numbers_never_crash_the_writer(self):
        body = completion("Chị hiểu ý em, chị muốn nghe bước tiếp theo.", usage={"cost": 10 ** 400, "prompt_tokens": 10 ** 400})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
            result = await QwenSalesWriter(settings(), client=client).write(PLAN, "Dạ", [])
        self.assertFalse(result["fallbackUsed"])
        self.assertIsNone(result["metadata"]["costUsd"])
        self.assertEqual(result["metadata"]["attempts"][0]["usage"], {})

    async def test_unsafe_period_and_negated_required_fact_reach_only_fallback(self):
        for candidate, plan in (
            ("Chị được đổi giày trong mười ngày nếu đôi giày còn nguyên.", PLAN),
            ("Chị không đi bộ hằng ngày.", {**PLAN, "allowedNewFacts": ["walking_routine"], "requiredFactIds": ["walking_routine"]}),
        ):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json=completion(candidate))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await QwenSalesWriter(settings(), client=client).write(plan, "Dạ", [])
            self.assertTrue(result["fallbackUsed"])
            self.assertNotEqual(result["customerText"], candidate)
            self.assertEqual(len(calls), 2)

    async def test_natural_factual_and_challenge_and_ending_prose_survive_guards(self):
        cases = (
            ({**PLAN, "allowedNewFacts": ["walking_routine"], "requiredFactIds": ["walking_routine"]},
             "Chị phải đi bộ khá nhiều mỗi ngày, em xem giúp chị đôi nào hợp hơn nhé."),
            ({**PLAN, "requiredContent": ["trust_challenge"]},
             "Lần trước em bảo đôi này hợp với chị, làm sao chị biết đôi mới sẽ tránh được vấn đề tương tự?"),
            ({**PLAN, "requiredContent": ["promise_challenge"]},
             "Chị không thể dựa vào lời hứa đó, em sẽ kiểm tra độ phù hợp thế nào?"),
            ({**PLAN, "ending": "objectives_completed", "requiredContent": ["ending_restored"]},
             "Chị thấy yên tâm với cách em xử lý rồi, cảm ơn em, chị về nhé."),
            ({**PLAN, "ending": "turn_limit", "requiredContent": ["ending_partially_restored"]},
             "Chị thấy em đã cố gắng nhưng vẫn lo, chị sẽ cân nhắc và về trước."),
            ({**PLAN, "ending": "manager_escalation", "requiredContent": ["ending_lost"]},
             "Chị không nói chuyện tiếp với em nữa, chị muốn gặp quản lý."),
        )
        for plan, candidate in cases:
            with self.subTest(candidate=candidate):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(candidate)))) as client:
                    result = await QwenSalesWriter(settings(), client=client).write(plan, "Dạ", [])
                self.assertFalse(result["fallbackUsed"], result["metadata"])
                self.assertEqual(result["customerText"], candidate)

    def test_arbitrary_exchange_purchase_periods_and_extra_conditions_are_rejected(self):
        for days in ("1", "2", "4", "10", "14", "30", "7.5", "7,5", "mười", "mười lăm", "hai mươi mốt", "bảy mươi", "vài"):
            with self.subTest(days=days):
                self.assertIn("invented_policy", customer_text_rejections(f"Chị được đổi giày trong {days} ngày.", PLAN))
        for days in ("1", "7", "10", "mười", "hai mươi", "vài"):
            self.assertIn("wrong_purchase_period", customer_text_rejections(f"Chị mua đôi giày này {days} ngày trước.", PLAN))
        for clause in ("có hóa đơn", "còn tem", "còn nguyên tem", "chưa sử dụng", "chưa dùng", "chưa mang"):
            self.assertIn("policy_or_instruction", customer_text_rejections(f"Chị yên tâm vì em sẽ đổi nếu giày {clause}.", PLAN))
        self.assertTrue(validate_customer_text("Chị mua đôi giày này ba ngày trước.", PLAN))
        self.assertTrue(validate_customer_text("Chị hiểu em nói được đổi trong bảy ngày.", PLAN))

    def test_negated_forbidden_and_contradictory_facts_are_rejected(self):
        for fact, candidate in (
            ("walking_routine", "Chị không đi bộ hằng ngày."),
            ("fit_condition", "Giày chị không đúng cỡ và không bị hỏng."),
            ("late_discomfort", "Chị đau ngay lúc thử đôi này."),
            ("lighter_preference", "Chị không thích giày nhẹ như đôi cũ."),
            ("appearance", "Chị không thích màu của đôi này."),
            ("original_missed_question", "Lần trước em đã hỏi kỹ nhu cầu của chị."),
            ("pain_location", "Chị không đau ở gót chân."),
        ):
            with self.subTest(fact=fact):
                plan = {**PLAN, "allowedNewFacts": [fact], "requiredFactIds": [fact]}
                self.assertIn("contradicted_fact", customer_text_rejections(candidate, plan))
                self.assertIn("missing_required_fact", customer_text_rejections(candidate, plan))
                self.assertIn("unallowed_fact", customer_text_rejections(candidate, PLAN))
        for fact, candidate in FACTS.items():
            known = ["walking_routine"] if fact == "original_missed_question" else []
            self.assertTrue(validate_customer_text(candidate, {**PLAN, "allowedNewFacts": [fact], "knownFacts": known, "requiredFactIds": [fact]}), fact)

    def test_required_content_never_accepts_arbitrary_or_opposite_response(self):
        for requirement in ("trust_challenge", "promise_challenge", "ending_restored", "ending_partially_restored", "ending_lost"):
            self.assertIn("missing_required_content", customer_text_rejections("Chị hiểu ý em.", {**PLAN, "requiredContent": [requirement]}))
        self.assertIn("missing_required_content", customer_text_rejections("Chị chưa yên tâm với em nên chị về nhé.",
                      {**PLAN, "ending": "objectives_completed", "requiredContent": ["ending_restored"]}))

    async def test_provider_failure_and_exhaustion_always_have_safe_text(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            writer = QwenSalesWriter(settings(), client=client)
            for _ in range(20):
                result = await writer.write(PLAN, "Dạ", [])
                self.assertEqual(result["customerText"], choose_customer_line(PLAN, "Dạ"))
                self.assertTrue(result["fallbackUsed"])
                self.assertIsNone(result["metadata"]["costUsd"])

    def test_guard_rejections_and_known_fact_confirmation(self):
        for text, code in (
            ("Chị muốn em nói " + "thêm " * 60, "too_long"),
            ("Chị sẽ nói. Em hãy nghe. Chị vẫn lo.", "too_many_sentences"),
            ("Tôi hiểu ý bạn rồi.", "wrong_address"),
            ("Chị đi bộ từ bến xe đến trường.", "unallowed_fact"),
            ("Chị muốn xem đáp án để chấm điểm em.", "policy_or_instruction"),
            ("Chị nghe em nói đôi giày này chắc chắn không đau.", "policy_or_instruction"),
        ):
            self.assertIn(code, customer_text_rejections(text, PLAN))
        self.assertTrue(validate_customer_text(FACTS["walking_routine"], {**PLAN, "knownFacts": ["walking_routine"]}))
        self.assertIn("transcript_echo", customer_text_rejections("Chị muốn em kiểm tra đôi giày này thật kỹ.", PLAN,
                                                                  "Em kiểm tra đôi giày này thật kỹ"))
        self.assertIn("missing_required_content", customer_text_rejections("Chị hiểu ý em.", {**PLAN, "requiredPhrases": ["Làm sao chị biết"]}))
        self.assertIn("missing_required_fact", customer_text_rejections("Chị hiểu ý em.", {**PLAN, "requiredFactIds": ["walking_routine"]}))


if __name__ == "__main__":
    unittest.main()
