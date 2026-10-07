"""Part 1 regression: a mark must be earned by what the player actually said.

The test drives the real Part 1 processing seam (SalesAttemptStore.accept ->
process_sales_attempt -> LLMSalesAssessor) with no network access. Only the
speech transcriber and the language-model service are replaced, so the saved
result shows whether credit is grounded in the transcript or copied from an
inflated model opinion.
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.run_results import project_sales_part1
from app.sales_persuasion import (
    PART1_CRITERIA,
    PART1_QUESTIONS,
    LLMSalesAssessor,
    SalesAttemptStore,
    SalesPersuasionSubmission,
    process_sales_attempt,
)
from app.tests.test_sales_persuasion import FakeTranscriber, submission_data, wav_bytes


# Everyday talk about weather, cooking, and a film. It does not mention the
# customer, the shoes, the price objection, or any product fact.
UNRELATED_TRANSCRIPT = (
    "Hôm nay trời mưa to quá. Tối nay tôi định nấu canh chua cá lóc "
    "rồi ngồi xem phim với cả nhà."
)
PART1_UNRELATED_SCORE_CEILING = 20


class InflatingLegacyLLM:
    """Returns the legacy {score, feedback_vi} shape with an unearned high mark."""

    configured = True

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        return (
            '{"score": 92, "feedback_vi": "Bạn đã nắm rõ nhu cầu của khách, '
            'xử lý phản đối về giá rất thuyết phục và giới thiệu sản phẩm chính xác."}'
        )


class RubricLLM:
    """Returns fixed per-criterion rubric decisions."""

    configured = True
    model = "fake-rubric"

    def __init__(self, decisions: dict) -> None:
        self.decisions = decisions
        self.calls = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        return json.dumps(self.decisions, ensure_ascii=False)


class SalesPart1UnrelatedAnswerScoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SalesAttemptStore(Path(self.directory.name))

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def _score(self, attempt_id: str, transcript: str, service) -> dict:
        request = SalesPersuasionSubmission.model_validate(
            submission_data(wav_bytes(amplitude=1000), attempt_id=attempt_id)
        )
        await self.store.accept(request)
        await process_sales_attempt(
            request.attempt_id,
            store=self.store,
            transcriber=FakeTranscriber(transcript),
            assessor=LLMSalesAssessor(service),
        )
        saved = await self.store.get(request.attempt_id)
        self.assertEqual(saved["assessmentStatus"], "completed", saved.get("error"))
        return saved

    async def test_unrelated_speech_cannot_earn_high_part1_score(self):
        request = SalesPersuasionSubmission.model_validate(
            submission_data(wav_bytes(amplitude=1000), attempt_id="part1-unrelated-001")
        )
        await self.store.accept(request)
        transcriber = FakeTranscriber(UNRELATED_TRANSCRIPT)

        await process_sales_attempt(
            request.attempt_id,
            store=self.store,
            transcriber=transcriber,
            assessor=LLMSalesAssessor(InflatingLegacyLLM()),
        )

        saved = await self.store.get(request.attempt_id)
        # The answer was spoken and transcribed, so this must be an assessed
        # result. A failure here means the scenario did not reach scoring.
        self.assertEqual(saved["assessmentStatus"], "completed", saved.get("error"))
        self.assertEqual(transcriber.calls, 1)
        self.assertEqual(saved["transcript"], UNRELATED_TRANSCRIPT)
        part1 = project_sales_part1(saved)
        self.assertIsInstance(part1["score"], int)
        self.assertLessEqual(
            part1["score"],
            PART1_UNRELATED_SCORE_CEILING,
            "Part 1 saved a high mark for speech unrelated to the customer, "
            "the shoes, or the price objection.",
        )

    async def test_rubric_credit_requires_quotes_from_the_transcript(self):
        # A short on-topic answer that the model over-credits with invented quotes.
        transcript = "Dạ giày này cũng được ạ."
        fabricated = {name: {"status": "true", "evidence": "Giày B nhẹ, hợp ngân sách và chị nên thử ngay"}
                      for name in PART1_CRITERIA}
        fabricated.update(falseProductClaim={"status": "false", "evidence": ""},
                          disrespectOrPressure={"status": "false", "evidence": ""})
        saved = await self._score("part1-fabricated-001", transcript, RubricLLM(fabricated))
        self.assertEqual(saved["score"], 0)
        self.assertEqual(set(saved["assessmentRubric"]["rejected"]), set(PART1_CRITERIA))

    async def test_grounded_complete_answer_earns_full_score(self):
        transcript = (
            "Em đề xuất chị lấy Giày B vì nhẹ và bền, đi bộ cả ngày không mỏi. "
            "Giày B chỉ một triệu hai nên vừa ngân sách của chị. Chị mang thử nhé."
        )
        quotes = {
            "recommendsShoe": "Em đề xuất chị lấy Giày B",
            "needsMatch": "nhẹ và bền, đi bộ cả ngày không mỏi",
            "objectionResponse": "vừa ngân sách của chị",
            "productFacts": "Giày B chỉ một triệu hai",
            "nextStep": "Chị mang thử nhé",
        }
        decisions = {name: {"status": "false", "evidence": ""} for name in PART1_QUESTIONS}
        decisions.update({name: {"status": "true", "evidence": quote} for name, quote in quotes.items()})
        saved = await self._score("part1-grounded-001", transcript, RubricLLM(decisions))
        self.assertEqual(saved["score"], 100)
        self.assertEqual(saved["assessmentRubric"]["rejected"], {})


if __name__ == "__main__":
    unittest.main()
