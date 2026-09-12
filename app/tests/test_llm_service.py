import unittest
import os
from unittest.mock import patch
from unittest.mock import AsyncMock

from app.config import DEFAULT_LLM_BASE_URL, DEFAULT_LLM_MODEL, get_settings
from app.llm_service import LLMService, LLMServiceError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class LLMServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_default_service():
        # Tests must not inherit a developer shell's LLM_* configuration.
        with patch.dict(
            "os.environ",
            {"LLM_BASE_URL": DEFAULT_LLM_BASE_URL, "LLM_MODEL": DEFAULT_LLM_MODEL},
        ):
            return LLMService()

    def test_defaults_target_local_llama_server_without_environment_contamination(self):
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("LLM_BASE_URL", None)
            os.environ.pop("LLM_MODEL", None)
            settings = get_settings()
        self.assertEqual(settings.llm_base_url, DEFAULT_LLM_BASE_URL)
        self.assertEqual(settings.llm_model, DEFAULT_LLM_MODEL)

    def test_environment_can_select_server_and_alias(self):
        with patch.dict(
            "os.environ",
            {
                "LLM_BASE_URL": "http://127.0.0.1:9090/v1/",
                "LLM_MODEL": "my-local-alias",
            },
            clear=True,
        ):
            settings = get_settings()

        self.assertEqual(settings.llm_base_url, "http://127.0.0.1:9090/v1")
        self.assertEqual(settings.llm_model, "my-local-alias")

    async def test_uses_llama_server_chat_completions_contract(self):
        service = self.make_default_service()
        response = FakeResponse({"choices": [{"message": {"content": "answer"}}]})
        service.client.post = AsyncMock(return_value=response)
        try:
            result = await service.generate([{"role": "user", "content": "hello"}])
            self.assertEqual(result, "answer")
            service.client.post.assert_awaited_once_with(
                "http://127.0.0.1:8080/v1/chat/completions",
                json={
                    "model": DEFAULT_LLM_MODEL,
                    "messages": [{"role": "user", "content": "hello"}],
                    "temperature": 0.3,
                    "top_p": 0.9,
                    "max_tokens": 100,
                    "stream": False,
                },
            )
        finally:
            await service.close()

    async def test_invalid_upstream_shape_is_reported_as_bad_gateway(self):
        service = self.make_default_service()
        service.client.post = AsyncMock(return_value=FakeResponse({"choices": []}))
        try:
            with self.assertRaises(LLMServiceError) as raised:
                await service.generate([{"role": "user", "content": "hello"}])
            self.assertEqual(raised.exception.status_code, 502)
            self.assertEqual(service.last_error, "invalid upstream response")
        finally:
            await service.close()

    async def test_generation_options_are_bounded_and_forwarded(self):
        service = self.make_default_service()
        response = FakeResponse({"choices": [{"message": {"content": "{}"}}]})
        service.client.post = AsyncMock(return_value=response)
        try:
            await service.generate(
                [{"role": "user", "content": "career data"}],
                options={"temperature": 0.6, "top_k": 20, "max_tokens": 512},
            )
            payload = service.client.post.await_args.kwargs["json"]
            self.assertEqual(payload["temperature"], 0.6)
            self.assertEqual(payload["top_k"], 20)
            self.assertEqual(payload["max_tokens"], 512)

            with self.assertRaises(LLMServiceError):
                await service.generate(
                    [{"role": "user", "content": "career data"}],
                    options={"messages": []},
                )
        finally:
            await service.close()

    async def test_message_contract_is_checked_before_upstream_call(self):
        service = self.make_default_service()
        service.client.post = AsyncMock()
        try:
            with self.assertRaises(LLMServiceError) as raised:
                await service.generate([{"role": "user", "content": ""}])
            self.assertEqual(raised.exception.status_code, 422)
            service.client.post.assert_not_awaited()
        finally:
            await service.close()


if __name__ == "__main__":
    unittest.main()
