import base64
import io
from pathlib import Path
import tempfile
import unittest
import wave

from app.lawyer_assessment import (
    LawyerAttemptConflictError,
    LawyerAttemptStore,
    LawyerCriterionAssessment,
    LawyerDefenseSubmission,
    process_lawyer_attempt,
)


def wav_bytes(amplitude: int = 1200) -> bytes:
    output = io.BytesIO()
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(sample * 320)
    return output.getvalue()


def submission(restarts: int = 0, amplitude: int = 1200) -> LawyerDefenseSubmission:
    return LawyerDefenseSubmission.model_validate(
        {
            "roundId": "a" * 32,
            "caseId": "club-room-fire",
            "interviewRestartCount": restarts,
            "assessmentContext": {
                "caseSummary": "Một vụ cháy xảy ra tại phòng CLB.",
                "investigationObjective": "Xây dựng lời bào chữa dựa trên chứng cứ.",
                "defenseConclusion": "Chưa đủ chứng cứ để kết luận Minh gây cháy.",
                "orderedEvidence": [
                    {
                        "evidenceId": f"clue-{index}",
                        "title": f"Manh mối {index}",
                        "description": f"Nội dung manh mối {index}.",
                        "strength": "strong" if index < 4 else "weak",
                    }
                    for index in range(1, 5)
                ],
                "reasoningCards": [
                    {
                        "reasoningId": "reasoning-timeline",
                        "title": "Mốc thời gian",
                        "description": "Minh rời phòng trước khi có dấu hiệu cháy.",
                    }
                ],
                "sampleAnswers": ["Minh rời phòng trước khi đám cháy phát triển."],
            },
            "audio": {
                "mimeType": "audio/wav",
                "encoding": "pcm_s16le",
                "sampleRateHz": 16000,
                "channels": 1,
                "dataBase64": base64.b64encode(wav_bytes(amplitude)).decode("ascii"),
            },
        }
    )


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, _path: Path) -> str:
        self.calls += 1
        return "Minh đã rời phòng và bằng chứng không xác định bộ sạc của Minh gây cháy."


class FakeAssessor:
    def __init__(self, raw_score: int = 75) -> None:
        self.calls = 0
        self.raw_score = raw_score

    async def assess(self, _attempt, _transcript: str) -> LawyerCriterionAssessment:
        self.calls += 1
        if self.raw_score == 75:
            scores = (30, 25, 12, 8)
        else:
            scores = (40, 35, 15, 10)
        return LawyerCriterionAssessment(
            evidence_use=scores[0],
            logical_connections=scores[1],
            conclusion_fidelity=scores[2],
            clarity_and_persuasiveness=scores[3],
            feedbackVi="Lập luận dựa trên chứng cứ đã chọn.",
        )


class LawyerAssessmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = LawyerAttemptStore(Path(self.temporary_directory.name))

    async def asyncTearDown(self):
        self.temporary_directory.cleanup()

    async def test_fourth_restart_halves_final_score_but_preserves_criteria(self):
        request = submission(restarts=4)
        await self.store.accept(request)

        result = await process_lawyer_attempt(
            request.round_id,
            store=self.store,
            transcriber=FakeTranscriber(),
            assessor=FakeAssessor(),
        )

        self.assertEqual(result["rawScore"], 75)
        self.assertEqual(result["restartPenaltyPercent"], 50)
        self.assertEqual(result["finalScore"], 37.5)
        self.assertEqual(result["criteria"]["evidenceUse"], 30)

    async def test_three_restarts_do_not_reduce_score(self):
        request = submission(restarts=3)
        await self.store.accept(request)

        result = await process_lawyer_attempt(
            request.round_id,
            store=self.store,
            transcriber=FakeTranscriber(),
            assessor=FakeAssessor(),
        )

        self.assertEqual(result["restartPenaltyPercent"], 0)
        self.assertEqual(result["finalScore"], 75.0)

    async def test_silent_recording_completes_without_model_calls(self):
        request = submission(restarts=7, amplitude=0)
        await self.store.accept(request)
        transcriber = FakeTranscriber()
        assessor = FakeAssessor()

        result = await process_lawyer_attempt(
            request.round_id,
            store=self.store,
            transcriber=transcriber,
            assessor=assessor,
        )

        self.assertEqual(result["assessmentStatus"], "completed")
        self.assertEqual(result["rawScore"], 0)
        self.assertEqual(result["finalScore"], 0.0)
        self.assertEqual(result["restartPenaltyPercent"], 50)
        self.assertEqual(transcriber.calls, 0)
        self.assertEqual(assessor.calls, 0)

    async def test_identical_retry_is_idempotent(self):
        request = submission(restarts=2)
        first, first_should_process = await self.store.accept(request)
        second, second_should_process = await self.store.accept(request)

        self.assertTrue(first_should_process)
        self.assertFalse(second_should_process)
        self.assertEqual(first["requestHash"], second["requestHash"])
        self.assertEqual(len(list(Path(self.temporary_directory.name).glob("*.wav"))), 1)

    async def test_changed_restart_count_conflicts_under_same_round_id(self):
        await self.store.accept(submission(restarts=3))

        with self.assertRaises(LawyerAttemptConflictError):
            await self.store.accept(submission(restarts=4))


if __name__ == "__main__":
    unittest.main()
