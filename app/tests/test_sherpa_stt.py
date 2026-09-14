from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy

from app.sales_persuasion import SalesProcessingError
from app.sherpa_setup import MODEL_NAME
from app.sherpa_stt import SherpaOnnxTranscriber


class FakeResult:
    text = "Em xin lỗi chị."


class FakeStream:
    result = FakeResult()

    def accept_waveform(self, sample_rate, samples):
        self.sample_rate = sample_rate
        self.samples = samples


class FakeRecognizer:
    def create_stream(self):
        self.stream = FakeStream()
        return self.stream

    def decode_stream(self, stream):
        self.decoded = stream


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes((1000).to_bytes(2, "little", signed=True) * 160)


class SherpaSttTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_recognizer_reports_technical_failure(self):
        transcriber = SherpaOnnxTranscriber()
        try:
            with self.assertRaises(SalesProcessingError) as raised:
                await transcriber.transcribe(Path("missing.wav"))
            self.assertEqual(raised.exception.code, "transcription_unavailable")
        finally:
            await transcriber.close()

    async def test_metadata_identifies_fixed_vietnamese_model(self):
        transcriber = SherpaOnnxTranscriber()
        transcriber._recognizer = FakeRecognizer()
        transcriber._numpy = numpy
        transcriber._version = "1.13.8"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "turn.wav"
                write_wav(path)
                result = await transcriber.transcribe_with_metadata(path)
            self.assertEqual(result.text, "Em xin lỗi chị.")
            self.assertEqual(result.language, "vi")
            self.assertEqual(result.provider, "sherpa-onnx")
            self.assertEqual(result.model, MODEL_NAME)
            self.assertEqual(result.version, "1.13.8")
        finally:
            await transcriber.close()

    async def test_initialize_keeps_process_alive_when_model_is_missing(self):
        transcriber = SherpaOnnxTranscriber()
        try:
            with patch("app.sherpa_stt.model_bundle_error", return_value="missing tokens.txt"):
                ready = await transcriber.initialize()
            self.assertFalse(ready)
            self.assertFalse(transcriber.ready)
            self.assertIn("Run 'ai setup'", transcriber.last_error)
        finally:
            await transcriber.close()


if __name__ == "__main__":
    unittest.main()
