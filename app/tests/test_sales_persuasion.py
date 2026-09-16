import base64
import asyncio
import io
import json
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from app import main
from app.sales_persuasion import (
    LLMSalesAssessor,
    SalesAssessment,
    SalesAttemptConflictError,
    SalesAttemptStore,
    SalesPersuasionSubmission,
    SalesProcessingError,
    process_sales_attempt,
)
from app.run_results import ParticipantManager, RunResultStore


def wav_bytes(*, amplitude: int = 0, frames: int = 160) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"".join(struct.pack("<h", amplitude) for _ in range(frames)))
    return stream.getvalue()


def submission_data(audio: bytes, *, attempt_id: str = "attempt-001") -> dict:
    return {
        "attemptId": attempt_id,
        "scenarioId": "shoe-scenario-1",
        "customerId": "customer-1",
        "selectedShoeId": "shoe-a",
        "bestFitShoeId": "shoe-b",
        "customerNeeds": "Cần giày nhẹ để đi bộ cả ngày.",
        "objection": "Giày này hơi đắt so với ngân sách của tôi.",
        "availableShoes": [
            {"shoeId": "shoe-a", "name": "Giày A", "price": 1800000, "details": "Êm chân"},
            {"shoeId": "shoe-b", "name": "Giày B", "price": 1200000, "details": "Nhẹ và bền"},
        ],
        "audio": {
            "mimeType": "audio/wav",
            "encoding": "pcm_s16le",
            "sampleRateHz": 16000,
            "channels": 1,
            "dataBase64": base64.b64encode(audio).decode("ascii"),
        },
    }


def unity_sales_event(audio: bytes, *, attempt_id: str = "attempt-001") -> dict:
    data = submission_data(audio, attempt_id=attempt_id)
    resolution = {
        "selectedShoe": data["selectedShoeId"],
        "bestFitShoe": data["bestFitShoeId"],
        "customerNeeds": data["customerNeeds"],
        "objection": data["objection"],
        "availableShoes": data["availableShoes"],
    }
    audio_data = dict(data["audio"])
    audio_data.update({"fileName": attempt_id + ".wav", "durationSeconds": 1.0, "endedEarly": False})
    return {
        "eventType": "sales.persuasion_recording",
        "payload": {
            "roundId": attempt_id,
            "questionId": data["scenarioId"],
            "caseId": data["customerId"],
            "cardId": data["selectedShoeId"],
            "resolution": json.dumps(resolution, ensure_ascii=False),
            "audio": audio_data,
        },
    }


class FakeTranscriber:
    def __init__(self, text: str = "Tôi chọn giày B vì nhẹ và phù hợp ngân sách."):
        self.text = text
        self.calls = 0

    async def transcribe(self, wav_path: Path) -> str:
        self.calls += 1
        return self.text


class FakeAssessor:
    def __init__(self, result: SalesAssessment | None = None, error: Exception | None = None):
        self.result = result or SalesAssessment(score=84, feedbackVi="Lập luận rõ ràng và đúng thông tin sản phẩm.")
        self.error = error
        self.calls = 0

    async def assess(self, attempt, transcript: str) -> SalesAssessment:
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeLLM:
    configured = True

    def __init__(self):
        self.messages = None

    async def generate(self, messages, **kwargs):
        self.messages = messages
        return '{"score": 80, "feedback_vi": "Phản hồi phù hợp."}'


class SalesPersuasionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SalesAttemptStore(Path(self.directory.name))

    async def asyncTearDown(self):
        self.directory.cleanup()

    def test_default_store_resolves_recordings_directory_at_use_time(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"RECORDINGS_DIR": directory}
        ):
            store = SalesAttemptStore()
            self.assertEqual(store.directory, Path(directory).resolve())

    def test_contract_rejects_unknown_shoe_references(self):
        data = submission_data(wav_bytes())
        data["selectedShoeId"] = "missing"
        with self.assertRaises(ValueError):
            SalesPersuasionSubmission.model_validate(data)

    async def test_accept_is_idempotent_and_conflicting_retry_is_rejected(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes(amplitude=1000)))
        first, should_process = await self.store.accept(request)
        repeated, repeated_should_process = await self.store.accept(request)

        self.assertTrue(should_process)
        self.assertFalse(repeated_should_process)
        self.assertEqual(first["requestHash"], repeated["requestHash"])
        self.assertEqual(len(list(Path(self.directory.name).glob("sales-persuasion-*.wav"))), 1)
        changed = request.model_copy(update={"objection": "Tôi không thích màu này."})
        with self.assertRaises(SalesAttemptConflictError):
            await self.store.accept(changed)

    async def test_unity_sales_telemetry_envelope_is_accepted_idempotently(self):
        previous_store = main.sales_attempt_store
        previous_transcriber = main.sales_transcriber
        previous_service = main.llm_service
        event = unity_sales_event(wav_bytes())
        main.sales_attempt_store = self.store
        main.sales_transcriber = FakeTranscriber()
        main.llm_service = None
        try:
            async with AsyncClient(
                transport=ASGITransport(app=main.app), base_url="http://testserver"
            ) as client:
                first_response = await client.post("/api/telemetry", json=event)
                second_response = await client.post("/api/telemetry", json=event)
            first = first_response.json()
            second = second_response.json()
            await asyncio.sleep(0.05)
            self.assertEqual(first_response.status_code, 200)
            self.assertEqual(second_response.status_code, 200)
            self.assertEqual(first["status"], "accepted")
            self.assertEqual(second["attemptId"], event["payload"]["roundId"])
            self.assertEqual(len(list(Path(self.directory.name).glob("sales-persuasion-*.wav"))), 1)
            record = await self.store.get(event["payload"]["roundId"])
            self.assertEqual(record["selectedShoeId"], event["payload"]["cardId"])
        finally:
            for task in tuple(main.sales_processing_tasks.values()):
                task.cancel()
            if main.sales_processing_tasks:
                await asyncio.gather(*main.sales_processing_tasks.values(), return_exceptions=True)
            main.sales_processing_tasks.clear()
            main.sales_attempt_store = previous_store
            main.sales_transcriber = previous_transcriber
            main.llm_service = previous_service

    async def test_unity_sales_telemetry_accepts_blank_optional_run_id(self):
        previous_store = main.sales_attempt_store
        previous_transcriber = main.sales_transcriber
        previous_service = main.llm_service
        event = unity_sales_event(wav_bytes())
        event["runId"] = ""
        event["payload"]["runId"] = ""
        main.sales_attempt_store = self.store
        main.sales_transcriber = FakeTranscriber()
        main.llm_service = None
        try:
            async with AsyncClient(
                transport=ASGITransport(app=main.app), base_url="http://testserver"
            ) as client:
                response = await client.post("/api/telemetry", json=event)

            self.assertEqual(response.status_code, 200, response.text)
            record = await self.store.get(event["payload"]["roundId"])
            self.assertIsNotNone(record)
            self.assertIsNone(record["runId"])
        finally:
            for task in tuple(main.sales_processing_tasks.values()):
                task.cancel()
            if main.sales_processing_tasks:
                await asyncio.gather(
                    *main.sales_processing_tasks.values(), return_exceptions=True
                )
            main.sales_processing_tasks.clear()
            main.sales_attempt_store = previous_store
            main.sales_transcriber = previous_transcriber
            main.llm_service = previous_service

    async def test_blank_telemetry_run_id_projects_into_active_sales_run(self):
        previous_results = main.run_result_store
        previous_participants = main.participant_manager
        previous_active_run_id = main.active_run_id
        previous_attempts = main.sales_attempt_store
        event = unity_sales_event(wav_bytes())
        event["runId"] = ""
        event["payload"]["runId"] = ""
        run_id = "sale-run-active"
        main.run_result_store = RunResultStore(Path(self.directory.name))
        main.sales_attempt_store = self.store
        main.participant_manager = ParticipantManager()
        participant, _ = main.participant_manager.assign("Nguyen Van A")
        main.active_run_id = run_id
        await main.run_result_store.begin(run_id, "sale", participant)
        try:
            with (
                patch.object(main, "_schedule_sales_processing"),
                patch.object(main.logger, "warning") as warning,
            ):
                result = await main.telemetry(event)

            self.assertEqual(result["status"], "accepted")
            warning.assert_not_called()
            draft = main.run_result_store._read(
                main.run_result_store._path(run_id, ".draft.json")
            )
            self.assertEqual(
                draft["data"]["part1"]["attemptId"], event["payload"]["roundId"]
            )
        finally:
            main.run_result_store = previous_results
            main.participant_manager = previous_participants
            main.active_run_id = previous_active_run_id
            main.sales_attempt_store = previous_attempts

    async def test_processing_attempts_are_discoverable_for_restart_recovery(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes()))
        await self.store.accept(request)
        self.assertEqual(await self.store.processing_attempt_ids(), [request.attempt_id])

    async def test_startup_requeues_processing_attempts(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes()))
        await self.store.accept(request)
        previous_store = main.sales_attempt_store
        main.sales_attempt_store = self.store
        try:
            with patch.object(main, "_queue_sales_processing", new=AsyncMock()) as queue:
                await main._resume_sales_processing()
            queue.assert_awaited_once_with(request.attempt_id)
        finally:
            main.sales_attempt_store = previous_store

    async def test_truncated_declared_wav_frames_are_rejected(self):
        malformed = bytearray(wav_bytes())
        data_size = struct.unpack_from("<I", malformed, 40)[0]
        riff_size = struct.unpack_from("<I", malformed, 4)[0]
        struct.pack_into("<I", malformed, 40, data_size + 2)
        struct.pack_into("<I", malformed, 4, riff_size + 2)
        with self.assertRaises(ValueError):
            await self.store.accept(
                SalesPersuasionSubmission.model_validate(submission_data(bytes(malformed)))
            )

    async def test_silence_is_completed_with_zero_without_transcription(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes()))
        await self.store.accept(request)
        transcriber = FakeTranscriber()
        result = await process_sales_attempt(
            request.attempt_id,
            store=self.store,
            transcriber=transcriber,
            assessor=FakeAssessor(),
        )

        self.assertEqual(result["assessmentStatus"], "completed")
        self.assertEqual(result["score"], 0)
        self.assertEqual(transcriber.calls, 0)

    async def test_non_silent_attempt_persists_transcript_and_assessment(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes(amplitude=1000)))
        await self.store.accept(request)
        result = await process_sales_attempt(
            request.attempt_id,
            store=self.store,
            transcriber=FakeTranscriber(),
            assessor=FakeAssessor(),
        )

        self.assertEqual(result["assessmentStatus"], "completed")
        self.assertEqual(result["transcript"], "Tôi chọn giày B vì nhẹ và phù hợp ngân sách.")
        self.assertEqual(result["score"], 84)
        self.assertEqual((await self.store.get(request.attempt_id))["feedbackVi"], "Lập luận rõ ràng và đúng thông tin sản phẩm.")

    async def test_transcription_failure_stays_failed_and_never_becomes_zero(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes(amplitude=1000)))
        await self.store.accept(request)
        result = await process_sales_attempt(
            request.attempt_id,
            store=self.store,
            transcriber=FailingTranscriber(),
            assessor=FakeAssessor(),
        )

        self.assertEqual(result["assessmentStatus"], "failed")
        self.assertIsNone(result["score"])
        self.assertEqual(result["error"]["code"], "transcription_failed")

    async def test_assessment_failure_stays_failed_and_never_becomes_zero(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes(amplitude=1000)))
        await self.store.accept(request)
        result = await process_sales_attempt(
            request.attempt_id,
            store=self.store,
            transcriber=FakeTranscriber(),
            assessor=FakeAssessor(
                error=SalesProcessingError("assessment_failed", "test model failure")
            ),
        )

        self.assertEqual(result["assessmentStatus"], "failed")
        self.assertIsNone(result["score"])
        self.assertEqual(result["error"]["code"], "assessment_failed")

    async def test_assessment_prompt_is_bounded_for_large_scenario(self):
        request = SalesPersuasionSubmission.model_validate(submission_data(wav_bytes()))
        attempt = request.model_dump(mode="json", by_alias=True)
        attempt["availableShoes"] = [
            {"shoeId": f"shoe-{index}", "name": "Tên giày" * 20, "price": index, "details": "Chi tiết" * 200}
            for index in range(20)
        ]
        attempt["customerNeeds"] = "Nhu cầu " * 1000
        attempt["objection"] = "Phản đối " * 1000
        service = FakeLLM()
        await LLMSalesAssessor(service).assess(attempt, "Câu trả lời " * 1000)
        self.assertLessEqual(
            len(service.messages[-1]["content"]),
            24_000,
        )


class FailingTranscriber:
    async def transcribe(self, wav_path: Path) -> str:
        raise SalesProcessingError("transcription_failed", "test transcription failure")


if __name__ == "__main__":
    unittest.main()
