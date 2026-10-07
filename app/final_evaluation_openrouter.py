"""OpenRouter text writer for final assessments, independent of local LLMs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
from typing import Any, Mapping

import httpx

FINAL_EVALUATION_MODEL = "deepseek/deepseek-v4.1-flash"
CHAT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_DELAY_SECONDS = 3.0


class FinalEvaluationProviderError(RuntimeError):
    """Only safe diagnostic messages, never provider bodies or credentials."""


def _retry_delay(response: httpx.Response) -> float:
    value = response.headers.get("retry-after")
    if not value:
        return 1.0 if response.status_code == 429 else .5
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else math.inf
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 1.0


class FinalEvaluationOpenRouter:
    def __init__(self, api_key: str | None, *, timeout_seconds: float = 45,
                 client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds)),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def generate(self, messages: list[dict[str, str]], *, options: Mapping[str, Any] | None = None,
                       max_message_chars: int = 16_000) -> str:
        if not self._api_key:
            raise FinalEvaluationProviderError("OPENROUTER_API_KEY chưa được cấu hình cho final eval.")
        if sum(len(item["content"]) for item in messages) > max_message_chars:
            raise FinalEvaluationProviderError("Nội dung final eval vượt giới hạn prompt đã cấu hình.")
        controls = options or {}
        payload = {
            "model": FINAL_EVALUATION_MODEL,
            "messages": messages,
            "reasoning": {"enabled": False},
            "provider": {"data_collection": "deny"},
            "temperature": controls.get("temperature", .2),
            "top_p": controls.get("top_p", .9),
            "max_tokens": controls.get("max_tokens", 1024),
            "stream": False,
        }
        for attempt in range(2):
            try:
                response = await self.client.post(
                    CHAT_ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            except httpx.HTTPError:
                if attempt == 0:
                    await asyncio.sleep(.5)
                    continue
                raise FinalEvaluationProviderError("Không thể kết nối tới OpenRouter để tạo final eval.") from None
            if attempt == 0 and response.status_code in RETRYABLE_STATUSES:
                delay = _retry_delay(response)
                if delay <= MAX_RETRY_DELAY_SECONDS:
                    await asyncio.sleep(delay)
                    continue
            if response.status_code in {401, 403}:
                raise FinalEvaluationProviderError("Khóa OpenRouter không hợp lệ hoặc không được phép dùng model.")
            if response.status_code == 402:
                raise FinalEvaluationProviderError("OpenRouter không đủ tín dụng để tạo final eval.")
            if response.status_code == 429:
                raise FinalEvaluationProviderError("OpenRouter đang giới hạn lượt phân tích. Hãy thử lại sau.")
            if not response.is_success:
                raise FinalEvaluationProviderError(f"OpenRouter chưa thể tạo final eval, mã HTTP {response.status_code}.")
            try:
                body = response.json()
                choice = body["choices"][0]
                content = choice["message"]["content"]
                if not isinstance(content, str) or choice.get("finish_reason") == "length":
                    raise ValueError("invalid completion")
            except (ValueError, KeyError, IndexError, TypeError):
                raise FinalEvaluationProviderError("OpenRouter trả về nội dung final eval thiếu hoặc chưa hoàn tất.") from None
            # Provider annotations must not become participant-facing feedback.
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE)
            return re.sub(r"\bUser\s+Safety\s*:\s*(?:safe|unsafe)\b", "", content, flags=re.IGNORECASE).strip()
        raise FinalEvaluationProviderError("OpenRouter chưa thể tạo final eval.")
