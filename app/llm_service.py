"""Small OpenAI-compatible client for the language model selected by PIPELINE_MODE."""

from collections.abc import Mapping
import re
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.final_evaluation_openrouter import FINAL_EVALUATION_MODEL

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
NO_THINK_PATTERN = re.compile(r"\s*/no_think\s*$")


class LLMServiceError(RuntimeError):
    """A safe, user-facing description of an upstream LLM failure."""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.llm_base_url
        self.model = self.settings.llm_model
        self.provider = "ryzenai-llm" if self.settings.llm_use_amd_hybrid else "llama.cpp"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self.settings.llm_connect_timeout_seconds,
                read=self.settings.llm_read_timeout_seconds,
                write=self.settings.llm_write_timeout_seconds,
                pool=self.settings.llm_pool_timeout_seconds,
            )
        )
        self.last_error: str | None = None
        self.request_count = 0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    async def generate(
        self,
        messages: list[dict[str, str]],
        *,
        options: Mapping[str, Any] | None = None,
        max_message_chars: int | None = None,
    ) -> str:
        if not messages:
            raise LLMServiceError("No messages were supplied to the language model.", status_code=422)
        message_limit = max_message_chars or self.settings.max_message_chars
        if len(messages) > self.settings.max_conversation_messages + 2 or any(
            not self._valid_message(message)
            or len(message["content"]) > message_limit
            for message in messages
        ):
            raise LLMServiceError("The language-model message contract is invalid.", status_code=422)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": self.settings.llm_max_tokens,
            "stream": False,
        }
        if options:
            allowed_options = {
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "presence_penalty",
                "max_tokens",
                "response_format",
                "reasoning_effort",
            }
            unknown_options = set(options) - allowed_options
            if unknown_options:
                raise LLMServiceError(
                    "The language-model generation options are invalid.", status_code=422
                )
            payload.update(options)
        self._prepare_payload(payload)

        self.request_count += 1
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exception:
            self.last_error = "upstream timeout"
            raise LLMServiceError("The language model timed out.") from exception
        except httpx.HTTPStatusError as exception:
            self.last_error = f"upstream HTTP {exception.response.status_code}"
            raise LLMServiceError(
                "The language model rejected the request.", status_code=502
            ) from exception
        except httpx.RequestError as exception:
            self.last_error = "upstream unavailable"
            raise LLMServiceError("The language model is unavailable.") from exception

        try:
            data: Any = response.json()
            choices = data["choices"]
            message = choices[0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exception:
            self.last_error = "invalid upstream response"
            raise LLMServiceError(
                "The language model returned an invalid response.", status_code=502
            ) from exception

        if not isinstance(content, str) or not content.strip():
            self.last_error = "empty upstream response"
            raise LLMServiceError(
                "The language model returned no answer.", status_code=502
            )

        self.last_error = None
        return content.strip()

    def _prepare_payload(self, payload: dict[str, Any]) -> None:
        """Adjust the OpenAI-compatible payload for the concrete provider."""

    @staticmethod
    def _valid_message(message: object) -> bool:
        if not isinstance(message, Mapping):
            return False
        role = message.get("role")
        content = message.get("content")
        return (
            isinstance(role, str)
            and role in {"system", "user", "assistant"}
            and isinstance(content, str)
            and bool(content.strip())
        )

    async def close(self) -> None:
        await self.client.aclose()


class OpenRouterLLMService(LLMService):
    """DeepSeek V4.1 Flash through OpenRouter, behind the local LLMService contract."""

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.base_url = OPENROUTER_BASE_URL
        self.model = FINAL_EVALUATION_MODEL
        self.provider = "openrouter"
        self._api_key = self.settings.openrouter_api_key
        if self._api_key:
            self.client.headers["Authorization"] = f"Bearer {self._api_key}"

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _prepare_payload(self, payload: dict[str, Any]) -> None:
        # Local Qwen prompts disable thinking with /no_think; OpenRouter uses a request flag.
        payload.pop("reasoning_effort", None)
        payload["reasoning"] = {"enabled": False}
        payload["messages"] = [
            {**message, "content": NO_THINK_PATTERN.sub("", message["content"])}
            for message in payload["messages"]
        ]
        # Prefer availability: any provider, but only ones honouring every sent parameter
        # when a JSON schema is requested.
        provider: dict[str, Any] = {"data_collection": "allow"}
        if "response_format" in payload:
            provider["require_parameters"] = True
        payload["provider"] = provider


def create_llm_service(settings: Settings | None = None) -> LLMService:
    """Return the OpenRouter service in openrouter mode, otherwise the local LLM."""

    settings = settings or get_settings()
    if settings.pipeline_mode == "openrouter":
        return OpenRouterLLMService(settings)
    return LLMService(settings)
