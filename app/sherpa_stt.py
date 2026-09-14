"""Offline Vietnamese speech recognition through sherpa-onnx."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import importlib
from pathlib import Path
import wave
from typing import Any

from app.config import get_settings
from app.sales_persuasion import SalesProcessingError
from app.sherpa_setup import MODEL_NAME, model_bundle_error, resolve_model_dir


@dataclass(frozen=True)
class SherpaTranscription:
    text: str
    language: str
    provider: str
    model: str
    version: str


class SherpaOnnxTranscriber:
    """Own one CPU recognizer and serialize native inference on one worker."""

    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = self._new_executor()
        self._recognizer: Any | None = None
        self._numpy: Any | None = None
        self._version = "unavailable"
        self._last_error: str | None = "Speech recognition has not been initialized."
        self._initialize_lock = asyncio.Lock()

    @staticmethod
    def _new_executor() -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=1, thread_name_prefix="sherpa-stt")

    @property
    def ready(self) -> bool:
        return self._recognizer is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def initialize(self) -> bool:
        async with self._initialize_lock:
            if self._executor is None:
                self._executor = self._new_executor()
            settings = get_settings()
            model_dir = resolve_model_dir(settings.sherpa_model_dir)
            error = model_bundle_error(model_dir)
            if error is not None:
                self._recognizer = None
                self._last_error = f"Model bundle is not configured: {error}. Run 'ai setup'."
                return False
            try:
                loop = asyncio.get_running_loop()
                recognizer, numpy_module, version = await loop.run_in_executor(
                    self._executor,
                    self._load_sync,
                    model_dir,
                    settings.sherpa_num_threads,
                )
            except Exception as exc:
                self._recognizer = None
                self._last_error = f"Speech recognition initialization failed: {exc}"
                return False
            self._recognizer = recognizer
            self._numpy = numpy_module
            self._version = version
            self._last_error = None
            return True

    @staticmethod
    def _load_sync(model_dir: Path, num_threads: int) -> tuple[Any, Any, str]:
        sherpa_onnx = importlib.import_module("sherpa_onnx")
        numpy_module = importlib.import_module("numpy")
        recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.onnx"),
            joiner=str(model_dir / "joiner.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=num_threads,
            sample_rate=16_000,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
        )
        version = str(
            getattr(sherpa_onnx, "__version__", getattr(sherpa_onnx, "version", "unavailable"))
        )
        return recognizer, numpy_module, version

    async def transcribe(self, wav_path: Path) -> str:
        return (await self.transcribe_with_metadata(wav_path)).text

    async def transcribe_with_metadata(self, wav_path: Path) -> SherpaTranscription:
        if not self.ready:
            raise SalesProcessingError("transcription_unavailable", self.last_error or "Speech recognition is unavailable.")
        executor = self._executor
        if executor is None:
            raise SalesProcessingError("transcription_unavailable", "Speech recognition is unavailable.")
        loop = asyncio.get_running_loop()
        work = loop.run_in_executor(executor, self._transcribe_sync, wav_path)
        try:
            text = await asyncio.wait_for(
                asyncio.shield(work), timeout=get_settings().sherpa_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise SalesProcessingError("transcription_failed", "Speech recognition timed out.") from exc
        except SalesProcessingError:
            raise
        except Exception as exc:
            raise SalesProcessingError("transcription_failed", "Speech recognition failed.") from exc
        return SherpaTranscription(
            text=text,
            language="vi",
            provider="sherpa-onnx",
            model=MODEL_NAME,
            version=self._version,
        )

    def _transcribe_sync(self, wav_path: Path) -> str:
        if self._recognizer is None or self._numpy is None:
            raise SalesProcessingError("transcription_unavailable", "Speech recognition is unavailable.")
        try:
            with wave.open(str(wav_path), "rb") as recording:
                if recording.getnchannels() != 1 or recording.getsampwidth() != 2:
                    raise SalesProcessingError("audio_read_failed", "Speech recognition requires mono 16-bit PCM WAV audio.")
                sample_rate = recording.getframerate()
                frames = recording.readframes(recording.getnframes())
        except (OSError, EOFError, wave.Error) as exc:
            raise SalesProcessingError("audio_read_failed", "The stored WAV could not be read.") from exc
        samples = self._numpy.frombuffer(frames, dtype=self._numpy.int16).astype(self._numpy.float32)
        samples *= 1.0 / 32768.0
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        text = str(stream.result.text).strip()
        if not text:
            raise SalesProcessingError("transcription_empty", "Speech recognition returned no text.")
        return text[: get_settings().max_transcript_chars]

    async def close(self) -> None:
        self._recognizer = None
        self._numpy = None
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
