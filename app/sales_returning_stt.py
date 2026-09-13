"""Language-aware whisper.cpp transcription without confidence collection."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile

from app.config import get_settings
from app.sales_persuasion import (
    SalesProcessingError, _find_whisper_executable, _resolve_local_path,
)


@dataclass(frozen=True)
class ReturningTranscription:
    text: str
    language: str
    provider: str
    model: str
    version: str


def parse_transcription(document: object, *, model: str, version: str) -> ReturningTranscription:
    if not isinstance(document, dict):
        raise SalesProcessingError("transcription_invalid", "Invalid speech recognition result.")
    result = document.get("result")
    segments = document.get("transcription")
    language = result.get("language") if isinstance(result, dict) else None
    if not isinstance(language, str) or re.fullmatch(r"[a-z]{2,3}", language) is None:
        raise SalesProcessingError("transcription_invalid", "Missing speech language.")
    if not isinstance(segments, list) or any(
        not isinstance(segment, dict) or not isinstance(segment.get("text"), str)
        for segment in segments
    ):
        raise SalesProcessingError("transcription_invalid", "Invalid speech segments.")
    text = " ".join(segment["text"].strip() for segment in segments).strip()
    if len(text) > get_settings().max_transcript_chars:
        raise SalesProcessingError("transcription_too_long", "Speech recognition result is too long.")
    if not text:
        raise SalesProcessingError("transcription_empty", "Speech recognition returned no text.")
    return ReturningTranscription(text, language, "whisper.cpp", model, version)


class ReturningWhisperTranscriber:
    """Use basic JSON output to read detected language, never token confidence."""

    async def transcribe(self, wav_path: Path) -> str:
        return (await self.transcribe_with_metadata(wav_path)).text

    async def transcribe_with_metadata(self, wav_path: Path) -> ReturningTranscription:
        settings = get_settings()
        executable = _find_whisper_executable(settings.whisper_cpp_bin)
        model = _resolve_local_path(settings.phowhisper_model)
        if executable is None or model is None:
            raise SalesProcessingError("transcription_unavailable", "Speech recognition is not configured.")
        version_output = await self._run([str(executable), "--version"], min(10, settings.whisper_timeout_seconds))
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?", version_output)
        if match is None:
            raise SalesProcessingError("transcription_version_missing", "Speech recognition version is unavailable.")
        with tempfile.TemporaryDirectory(prefix="sales-turn-stt-") as temporary:
            prefix = Path(temporary) / "transcript"
            await self._run([
                str(executable), "-m", str(model), "-f", str(wav_path),
                "-oj", "-of", str(prefix), "-l", "auto", "-nt", "-np",
            ], settings.whisper_timeout_seconds)
            try:
                document = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SalesProcessingError("transcription_invalid", "Speech recognition output is unreadable.") from exc
            return parse_transcription(document, model=model.name, version=match.group(0))

    @staticmethod
    async def _run(command: list[str], timeout: float) -> str:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode != 0:
                raise SalesProcessingError("transcription_failed", "Speech recognition failed.")
            return (stdout + stderr).decode("utf-8", errors="replace")
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.TimeoutError) as exc:
            raise SalesProcessingError("transcription_failed", "Speech recognition failed.") from exc
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except OSError:
                    pass
