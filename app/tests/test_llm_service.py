import unittest
import os
from unittest.mock import patch
from unittest.mock import AsyncMock

from app.config import (
    AMD_HYBRID_LLM_BASE_URL,
    AMD_HYBRID_LLM_MODEL,
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    get_settings,
)
from app.final_evaluation_openrouter import FINAL_EVALUATION_MODEL
from app.llm_service import (
    LLMService,
    LLMServiceError,
    OPENROUTER_BASE_URL,
    OpenRouterLLMService,
    create_llm_service,
)


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

    async def test_hybrid_flag_selects_amd_model_without_changing_llama_configuration(self):
        with patch.dict("os.environ", {
            "LLM_USE_AMD_HYBRID": "true",
            "LLM_BASE_URL": "http://127.0.0.1:9090/v1",
            "LLM_MODEL": "legacy-alias",
        }, clear=True):
            settings = get_settings()
            service = LLMService(settings)
        self.assertTrue(settings.llm_use_amd_hybrid)
        self.assertEqual(settings.llm_base_url, AMD_HYBRID_LLM_BASE_URL)
        self.assertEqual(settings.llm_model, AMD_HYBRID_LLM_MODEL)
        self.assertEqual(service.provider, "ryzenai-llm")
        await service.close()
        with patch.dict("os.environ", {
            "LLM_USE_AMD_HYBRID": "false",
            "LLM_BASE_URL": "http://127.0.0.1:9090/v1",
            "LLM_MODEL": "legacy-alias",
        }, clear=True):
            disabled = get_settings()
        self.assertEqual(disabled.llm_base_url, "http://127.0.0.1:9090/v1")
        self.assertEqual(disabled.llm_model, "legacy-alias")

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
                options={
                    "temperature": 0.6,
                    "top_k": 20,
                    "max_tokens": 512,
                    "reasoning_effort": "none",
                },
            )
            payload = service.client.post.await_args.kwargs["json"]
            self.assertEqual(payload["temperature"], 0.6)
            self.assertEqual(payload["top_k"], 20)
            self.assertEqual(payload["max_tokens"], 512)
            self.assertEqual(payload["reasoning_effort"], "none")

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


class PipelineModeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def settings(mode, key="test-key"):
        environment = {"PIPELINE_MODE": mode, "OPENROUTER_API_KEY": key}
        with patch.dict("os.environ", environment):
            return get_settings()

    async def test_openrouter_mode_selects_deepseek(self):
        service = create_llm_service(self.settings("openrouter"))
        try:
            self.assertIsInstance(service, OpenRouterLLMService)
            self.assertEqual(service.model, FINAL_EVALUATION_MODEL)
            self.assertEqual(service.provider, "openrouter")
            self.assertTrue(service.configured)
            self.assertEqual(service.client.headers["Authorization"], "Bearer test-key")
        finally:
            await service.close()

    async def test_legacy_and_shadow_modes_select_local_llm(self):
        for mode in ("legacy", "shadow", "unexpected"):
            service = create_llm_service(self.settings(mode))
            try:
                self.assertIs(type(service), LLMService)
            finally:
                await service.close()

    async def test_openrouter_without_key_is_not_configured(self):
        service = create_llm_service(self.settings("openrouter", key=""))
        try:
            self.assertFalse(service.configured)
        finally:
            await service.close()

    async def test_openrouter_payload_disables_reasoning_and_requires_schema_support(self):
        service = create_llm_service(self.settings("openrouter"))
        response = FakeResponse({"choices": [{"message": {"content": "{}"}}]})
        service.client.post = AsyncMock(return_value=response)
        schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
        try:
            await service.generate(
                [
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "facts\n/no_think"},
                ],
                options={"max_tokens": 64, "response_format": schema, "reasoning_effort": "none"},
            )
            url = service.client.post.await_args.args[0]
            payload = service.client.post.await_args.kwargs["json"]
            self.assertEqual(url, f"{OPENROUTER_BASE_URL}/chat/completions")
            self.assertEqual(payload["model"], FINAL_EVALUATION_MODEL)
            self.assertEqual(payload["messages"][1]["content"], "facts")
            self.assertEqual(payload["reasoning"], {"enabled": False})
            self.assertNotIn("reasoning_effort", payload)
            self.assertEqual(payload["response_format"], schema)
            self.assertEqual(
                payload["provider"], {"data_collection": "allow", "require_parameters": True}
            )
        finally:
            await service.close()

    async def test_openrouter_without_schema_does_not_require_parameters(self):
        service = create_llm_service(self.settings("openrouter"))
        response = FakeResponse({"choices": [{"message": {"content": "ok"}}]})
        service.client.post = AsyncMock(return_value=response)
        try:
            await service.generate([{"role": "user", "content": "hello"}])
            payload = service.client.post.await_args.kwargs["json"]
            self.assertEqual(payload["provider"], {"data_collection": "allow"})
        finally:
            await service.close()


if __name__ == "__main__":
    unittest.main()
