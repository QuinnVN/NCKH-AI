import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.final_evaluation_openrouter import (
    CHAT_ENDPOINT, FINAL_EVALUATION_MODEL, FinalEvaluationOpenRouter,
    FinalEvaluationProviderError,
)


def completion(content="Nhận xét có căn cứ.", **extra):
    return {"choices": [{"message": {"content": content}, **extra}]}


class FinalEvaluationOpenRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_only_requested_model_and_disables_thinking(self):
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(200, json=completion())
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            writer = FinalEvaluationOpenRouter("test-secret", client=client)
            text = await writer.generate([{"role": "user", "content": "Dữ kiện tổng hợp."}],
                                         options={"temperature": .1, "max_tokens": 700})
            await writer.close()
            self.assertFalse(client.is_closed)
        self.assertEqual(text, "Nhận xét có căn cứ.")
        self.assertEqual(str(sent[0].url), CHAT_ENDPOINT)
        self.assertEqual(sent[0].headers['authorization'], 'Bearer test-secret')
        body = json.loads(sent[0].content)
        self.assertEqual(body['model'], FINAL_EVALUATION_MODEL)
        self.assertEqual(body['model'], 'deepseek/deepseek-v4.1-flash')
        self.assertEqual(body['reasoning'], {'enabled': False})
        self.assertEqual(body['provider'], {'data_collection': 'deny'})
        self.assertEqual(body['max_tokens'], 700)
        self.assertEqual(body['temperature'], .1)
        self.assertNotIn('models', body)
        self.assertNotIn('response_format', body)

    async def test_retries_temporary_rate_limit_once(self):
        responses = iter([httpx.Response(429, headers={'retry-after': '0'}), httpx.Response(200, json=completion())])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as client:
            with patch('app.final_evaluation_openrouter.asyncio.sleep', AsyncMock()) as sleep:
                text = await FinalEvaluationOpenRouter('key', client=client).generate([])
            sleep.assert_awaited_once_with(0)
        self.assertEqual(text, 'Nhận xét có căn cứ.')

    async def test_does_not_wait_beyond_retry_window_or_leak_provider_error(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(429, headers={'retry-after':'120'}, json={'error':'test-secret confidential'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch('app.final_evaluation_openrouter.asyncio.sleep', AsyncMock()) as sleep:
                with self.assertRaises(FinalEvaluationProviderError) as error:
                    await FinalEvaluationOpenRouter('test-secret', client=client).generate([])
            sleep.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertNotIn('test-secret', str(error.exception))
        self.assertNotIn('confidential', str(error.exception))

    async def test_preserves_persistent_rate_limit_and_exhausted_network_errors(self):
        for status in [429, 503]:
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, headers={'retry-after':'0'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with patch('app.final_evaluation_openrouter.asyncio.sleep', AsyncMock()):
                    with self.assertRaises(FinalEvaluationProviderError):
                        await FinalEvaluationOpenRouter('key', client=client).generate([])
            self.assertEqual(len(calls), 2)
        client = AsyncMock()
        client.post.side_effect = httpx.ReadTimeout('test-secret provider detail')
        with patch('app.final_evaluation_openrouter.asyncio.sleep', AsyncMock()):
            with self.assertRaises(FinalEvaluationProviderError) as error:
                await FinalEvaluationOpenRouter('key', client=client).generate([])
        self.assertEqual(client.post.await_count, 2)
        self.assertNotIn('test-secret', str(error.exception))

    async def test_missing_key_and_oversized_prompt_never_contact_provider(self):
        client = AsyncMock()
        with self.assertRaisesRegex(FinalEvaluationProviderError, 'OPENROUTER_API_KEY'):
            await FinalEvaluationOpenRouter(None, client=client).generate([])
        with self.assertRaises(FinalEvaluationProviderError):
            await FinalEvaluationOpenRouter('key', client=client).generate(
                [{'role':'user','content':'abcd'}], max_message_chars=3)
        client.post.assert_not_called()

    async def test_rejects_invalid_or_truncated_completions(self):
        for body in [{}, completion(None), completion('Chưa viết xong', finish_reason='length'), {'choices':[]}]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=body))) as client:
                with self.assertRaises(FinalEvaluationProviderError):
                    await FinalEvaluationOpenRouter('key', client=client).generate([])

    async def test_sanitizes_reasoning_and_provider_safety_labels(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=completion(
            '<think>Nội dung nội bộ.</think> User Safety: safe\nNhận xét hữu ích.'
        )))) as client:
            text = await FinalEvaluationOpenRouter('key', client=client).generate([])
        self.assertEqual(text, 'Nhận xét hữu ích.')

    async def test_closes_owned_http_client(self):
        writer = FinalEvaluationOpenRouter('key')
        await writer.close()
        self.assertTrue(writer.client.is_closed)


if __name__ == '__main__':
    unittest.main()
