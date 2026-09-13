import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.sales_persuasion import SalesProcessingError
from app.sales_returning_stt import ReturningWhisperTranscriber, parse_transcription


class ReturningSttTests(unittest.IsolatedAsyncioTestCase):
    def test_detected_language_is_preserved_without_confidence(self):
        result = parse_transcription({"result": {"language": "en"}, "transcription": [
            {"text": " I need help.", "tokens": [{"p": 0.99}]}]}, model="model.bin", version="1.9.3")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.text, "I need help.")
        self.assertFalse(hasattr(result, "confidence"))

    def test_missing_language_and_empty_output_are_technical(self):
        for value in ({}, {"result": {"language": "vi"}, "transcription": []}):
            with self.assertRaises(SalesProcessingError):
                parse_transcription(value, model="model.bin", version="1.9.3")

    async def test_cli_uses_auto_language_basic_json_and_records_safe_version(self):
        commands = []

        async def run(command, timeout):
            commands.append(command)
            if "--version" in command:
                return "whisper.cpp version: 1.9.3-dev\n"
            prefix = Path(command[command.index("-of") + 1])
            prefix.with_suffix(".json").write_text(json.dumps({
                "result": {"language": "vi"},
                "transcription": [{"text": "Em xin lỗi chị."}],
            }), encoding="utf-8")
            return ""

        with patch("app.sales_returning_stt._find_whisper_executable", return_value=Path("private/bin/whisper-cli")), \
             patch("app.sales_returning_stt._resolve_local_path", return_value=Path("private/models/phowhisper.bin")), \
             patch.object(ReturningWhisperTranscriber, "_run", side_effect=run):
            result = await ReturningWhisperTranscriber().transcribe_with_metadata(Path("turn.wav"))
        self.assertEqual(result.model, "phowhisper.bin")
        self.assertEqual(result.version, "1.9.3-dev")
        self.assertEqual(commands[1][commands[1].index("-l") + 1], "auto")
        self.assertIn("-oj", commands[1])
        self.assertNotIn("-ojf", commands[1])

    async def test_cancellation_kills_transcription_process(self):
        process = AsyncMock()
        process.returncode = None
        process.kill = Mock()
        process.communicate.side_effect = asyncio.CancelledError
        with patch("asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(asyncio.CancelledError):
                await ReturningWhisperTranscriber._run(["whisper-cli"], 2)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
