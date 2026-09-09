# services/llm_service.py

from collections.abc import AsyncIterator

import httpx


class LLMService:
    def __init__(self) -> None:
        self.base_url = "http://127.0.0.1:8081/v1"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=120.0,
                write=10.0,
                pool=5.0,
            )
        )

    async def generate(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        response = await self.client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": "qwen3-4b",
                "messages": messages,
                "temperature": 0.3,
                "top_p": 0.9,
                "max_tokens": 100,
                "stream": False,
            },
        )
        response.raise_for_status()

        data = response.json()
        return data["choices"][0]["message"]["content"]

    async def close(self) -> None:
        await self.client.aclose()