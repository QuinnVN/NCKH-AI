"""Small llama.cpp/OpenAI-compatible client used by the response endpoint."""

from collections.abc import Mapping
from typing import Any

import httpx

from app.config import Settings, get_settings


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

    async def generate(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            raise LLMServiceError("No messages were supplied to the language model.", status_code=422)
        if len(messages) > self.settings.max_conversation_messages + 2 or any(
            not self._valid_message(message)
            or len(message["content"]) > self.settings.max_message_chars
            for message in messages
        ):
            raise LLMServiceError("The language-model message contract is invalid.", status_code=422)

        self.request_count += 1
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.3,
                    "top_p": 0.9,
                    "max_tokens": self.settings.llm_max_tokens,
                    "stream": False,
                },
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
