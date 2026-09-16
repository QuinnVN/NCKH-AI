"""Bounded client for the local Supertonic sidecar.

The service deliberately keeps synthesized audio only in the request buffer. It
does not cache audio or persist TTS input text.
"""

from __future__ import annotations

import asyncio
import io
import time
import wave
from dataclasses import dataclass

import httpx

from app.config import get_settings


MAX_TTS_CHARS = 600
MAX_WAV_BYTES = 10 * 1024 * 1024


class TTSError(RuntimeError):
    code = "tts_failed"


class TTSUnavailableError(TTSError):
    code = "tts_unavailable"


class TTSTimeoutError(TTSError):
    code = "tts_timeout"


class TTSInvalidAudioError(TTSError):
    code = "tts_invalid_audio"


class TTSBusyError(TTSError):
    code = "tts_busy"


@dataclass(frozen=True)
class SynthesizedWav:
    data: bytes
    duration_seconds: float
    sample_rate: int
    model_version: str | None


class TTSCoordinator:
    """Allow one active synthesis and one request waiting for it."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._available = asyncio.Event()
        self._available.set()
        self._active = False
        self._waiting = False

    async def acquire(self, deadline: float) -> None:
        queued = False
        while True:
            async with self._lock:
                if not self._active:
                    self._active = True
                    self._available.clear()
                    return
                if not queued:
                    if self._waiting:
                        raise TTSBusyError()
                    self._waiting = True
                    queued = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                async with self._lock:
                    if queued:
                        self._waiting = False
                raise TTSTimeoutError()
            try:
                await asyncio.wait_for(self._available.wait(), timeout=remaining)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exception:
                async with self._lock:
                    if queued:
                        self._waiting = False
                if isinstance(exception, asyncio.CancelledError):
                    raise
                raise TTSTimeoutError() from exception

    async def release(self) -> None:
        async with self._lock:
            self._active = False
            self._waiting = False
            self._available.set()


class SupertonicClient:
    """Speak to Supertonic's native endpoint with fixed research settings."""

    async def synthesize(self, text: str, deadline: float) -> SynthesizedWav:
        settings = get_settings()
        if not settings.supertonic_base_url:
            raise TTSUnavailableError()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TTSTimeoutError()
        timeout = httpx.Timeout(remaining, connect=min(remaining, 2.0))
        payload = {
            "text": text,
            "voice": "F4",
            "lang": "vi",
            "steps": 5,
            "speed": 1.15,
            "response_format": "wav",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", settings.supertonic_base_url + "/v1/tts", json=payload
                ) as response:
                    if response.status_code >= 400:
                        raise TTSUnavailableError()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_WAV_BYTES:
                            raise TTSInvalidAudioError()
                    version = response.headers.get("X-Supertonic-Version")
        except TTSInvalidAudioError:
            raise
        except httpx.TimeoutException as exception:
            raise TTSTimeoutError() from exception
        except httpx.HTTPError as exception:
            raise TTSUnavailableError() from exception
        return _validate_wav(bytes(body), version)


def _validate_wav(data: bytes, model_version: str | None) -> SynthesizedWav:
    if not data or len(data) > MAX_WAV_BYTES:
        raise TTSInvalidAudioError()
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            sample_rate = source.getframerate()
            channels = source.getnchannels()
            frames = source.getnframes()
            sample_width = source.getsampwidth()
            if sample_rate <= 0 or channels <= 0 or frames <= 0 or sample_width <= 0:
                raise wave.Error("empty WAV")
            duration = frames / sample_rate
    except (wave.Error, EOFError) as exception:
        raise TTSInvalidAudioError() from exception
    return SynthesizedWav(data=data, duration_seconds=duration, sample_rate=sample_rate, model_version=model_version)


class TTSService:
    def __init__(self, client: SupertonicClient | None = None) -> None:
        self._client = client or SupertonicClient()
        self._coordinator = TTSCoordinator()
        self.ready = False
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        settings = get_settings()
        return not settings.disable_ai_sale_pt2 and settings.supertonic_base_url is not None

    async def synthesize(self, text: str) -> SynthesizedWav:
        settings = get_settings()
        if settings.disable_ai_sale_pt2:
            raise TTSUnavailableError()
        deadline = time.monotonic() + settings.tts_timeout_seconds
        try:
            await self._coordinator.acquire(deadline)
            try:
                result = await self._client.synthesize(text, deadline)
            finally:
                await self._coordinator.release()
        except asyncio.CancelledError:
            raise
        except TTSError as exception:
            self.ready = False
            self.last_error = exception.code
            raise
        self.ready = True
        self.last_error = None
        return result
