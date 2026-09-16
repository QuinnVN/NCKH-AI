import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from app import main
from app.sales_returning_customer import ReturningSessionStore, OPENING_COMPLAINT, sales_speech_id
from app.tts_service import SynthesizedWav, TTSBusyError, TTSInvalidAudioError, TTSUnavailableError


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as source:
        source.setnchannels(1)
        source.setsampwidth(2)
        source.setframerate(16000)
        source.writeframes(b"\x00\x00" * 1600)
    return output.getvalue()


class FakeTTS:
    configured = True
    ready = True
    last_error = None

    def __init__(self, result=None):
        self.result = result or SynthesizedWav(wav_bytes(), 0.1, 16000, "1.3.1")
        self.texts = []

    async def synthesize(self, text):
        self.texts.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TTSApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReturningSessionStore(Path(self.temp.name))
        self.tts = FakeTTS()
        self.patches = [
            patch.object(main, "sales_returning_store", self.store),
            patch.object(main, "tts_service", self.tts),
            patch.dict(
                os.environ,
                {
                    "BACKEND_API_TOKEN": "tts-test",
                    "DISABLE_AI_SALE_PT2": "false",
                },
            ),
        ]
        for item in self.patches:
            item.start()
        self.client = AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")
        self.auth = {"Authorization": "Bearer tts-test"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def test_general_route_returns_complete_wav_and_safe_headers(self):
        response = await self.client.post("/api/tts", headers=self.auth, json={"text": "Xin chào"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.headers["x-sample-rate"], "16000")
        self.assertEqual(response.headers["x-supertonic-version"], "1.3.1")
        self.assertEqual(response.content, self.tts.result.data)
        self.assertEqual(self.tts.texts, ["Xin chào"])

    async def test_general_route_enforces_auth_and_text_bound(self):
        denied = await self.client.post("/api/tts", json={"text": "Xin chào"})
        too_long = await self.client.post("/api/tts", headers=self.auth, json={"text": "x" * 601})

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(too_long.status_code, 422)

    async def test_sales_route_resolves_opening_text_without_caller_text(self):
        session = await self.store.create_or_resume("speech-session")
        speech_id = sales_speech_id("speech-session", "opening", OPENING_COMPLAINT)

        response = await self.client.get(
            f"/api/sales/sessions/speech-session/speech/{speech_id}", headers=self.auth
        )
        unknown = await self.client.get(
            "/api/sales/sessions/speech-session/speech/not-a-real-speech-id", headers=self.auth
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tts.texts, [OPENING_COMPLAINT])
        self.assertEqual(response.headers["x-speech-id"], speech_id)
        self.assertEqual(unknown.status_code, 404)
        self.assertNotIn("text", str(unknown.request.url))

    async def test_tts_failures_have_stable_categories(self):
        for error, status, code in (
            (TTSUnavailableError(), 503, "tts_unavailable"),
            (TTSBusyError(), 429, "tts_busy"),
            (TTSInvalidAudioError(), 502, "tts_invalid_audio"),
        ):
            with self.subTest(code=code):
                self.tts.result = error
                response = await self.client.post("/api/tts", headers=self.auth, json={"text": "Xin chào"})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"]["code"], code)

    async def test_disable_flag_returns_text_only_and_skips_tts(self):
        with patch.dict(os.environ, {"DISABLE_AI_SALE_PT2": "true"}):
            session_response = await self.client.post(
                "/api/sales/sessions",
                headers=self.auth,
                json={"sessionId": "text-only-session"},
            )
            tts_response = await self.client.post(
                "/api/tts", headers=self.auth, json={"text": "Xin chào"}
            )

        self.assertEqual(session_response.status_code, 200)
        self.assertIn("lastCustomerText", session_response.json())
        self.assertNotIn("speech", session_response.json())
        self.assertEqual(tts_response.status_code, 503)
        self.assertEqual(tts_response.json()["detail"]["code"], "tts_disabled")
        self.assertEqual(self.tts.texts, [])
