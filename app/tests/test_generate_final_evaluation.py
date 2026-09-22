import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.final_evaluation import DIMENSION_IDS
from app.final_evaluation_calculation import (
    behavioural_alignment,
    build_careers,
    calculate_behaviours,
    dimension_level,
    extract_dimensions,
    group_scores,
)
from scripts.generate_final_evaluation import (
    choose_participant,
    ensure_qwen_server,
    generate_assessment,
    resolve_game_participant_name,
)
from app.tests.test_final_evaluation import COMPLETED_AT, EMAIL, NAME


def questionnaire() -> dict:
    return {"dimensions": [{"id": code, "name": code, "description": f"Mô tả {code}",
                             "score": 50 + (index % 5) * 10}
                            for index, code in enumerate(DIMENSION_IDS)]}


def lawyer_game() -> dict:
    return {"gameId": "lawyer", "status": "completed", "data": {"lawyer": {
        "criterionScores": {"evidenceUse": 32, "logicalConnections": 28,
                            "conclusionFidelity": 12, "clarityAndPersuasiveness": 8}}}}


class CalculationTests(unittest.TestCase):
    def test_choose_participant_retries_invalid_input(self):
        answers = iter(["x", "4", "2"])
        output = []
        selected = choose_participant(["An", "Bình"], input_fn=lambda _: next(answers), output_fn=output.append)
        self.assertEqual(selected, "Bình")

    def test_code_calculates_dimensions_behaviours_and_careers(self):
        dimensions = extract_dimensions(questionnaire())
        groups = group_scores(dimensions)
        behaviours = calculate_behaviours(lawyer_game(), dimensions)
        alignment = behavioural_alignment("lawyer", behaviours)
        careers = build_careers(groups, "lawyer", alignment)

        self.assertEqual(len(dimensions), 28)
        self.assertEqual(dimension_level(81), "well-compatible")
        self.assertEqual(behaviours["problem-solving"].score, 80)
        self.assertIsInstance(alignment, int)
        self.assertEqual(len(careers), 3)
        self.assertTrue(all(isinstance(item.compatibility_percent, int) for item in careers))

    def test_extracts_real_mongo_questionnaire_score_shape(self):
        document = {"participant": {"name": NAME, "email": EMAIL}, "scores": {
            "groups": {code: {"raw": 3, "max": 4, "percent": "75"}
                       for code in DIMENSION_IDS}}}

        dimensions = extract_dimensions(document)

        self.assertEqual(set(dimensions), set(DIMENSION_IDS))
        self.assertTrue(all(item.score == 75 for item in dimensions.values()))

    def test_resolves_unique_reordered_name_without_fuzzy_typo_matching(self):
        class Names:
            @staticmethod
            def count_documents(query):
                return int(query["participantName"] == "Exact Name")

            @staticmethod
            def distinct(*_args, **_kwargs):
                return ["Nguyễn Mỹ Ngọc Anh", "Hoàng Hảo Nguyên Nguyên"]

        collection = Names()
        self.assertEqual(
            resolve_game_participant_name(collection, "Ngọc Anh Nguyễn Mỹ"),
            "Nguyễn Mỹ Ngọc Anh",
        )
        self.assertEqual(
            resolve_game_participant_name(collection, "Hoàng Thảo Nguyên Nguyên"),
            "Hoàng Thảo Nguyên Nguyên",
        )


class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_start_another_window_when_qwen_is_ready(self):
        service = Mock()
        with patch(
            "scripts.generate_final_evaluation.available_model_ids",
            AsyncMock(return_value={"qwen3-8b"}),
        ), patch("scripts.generate_final_evaluation.start_qwen_server_window") as start:
            started = await ensure_qwen_server(
                service, "qwen3-8b", auto_start=True, timeout_seconds=30,
                output_fn=lambda _message: None,
            )
        self.assertFalse(started)
        start.assert_not_called()

    async def test_starts_new_window_and_waits_for_requested_alias(self):
        service = Mock()
        process = Mock()
        process.poll.return_value = None
        probe = AsyncMock(side_effect=[None, {"qwen3-8b"}])
        with patch("scripts.generate_final_evaluation.available_model_ids", probe), patch(
            "scripts.generate_final_evaluation.start_qwen_server_window",
            return_value=process,
        ) as start, patch("scripts.generate_final_evaluation.asyncio.sleep", AsyncMock()):
            started = await ensure_qwen_server(
                service, "qwen3-8b", auto_start=True, timeout_seconds=30,
                output_fn=lambda _message: None,
            )
        self.assertTrue(started)
        start.assert_called_once_with(service.base_url, "qwen3-8b")

    async def test_rejects_a_different_model_on_the_configured_port(self):
        service = Mock()
        with patch(
            "scripts.generate_final_evaluation.available_model_ids",
            AsyncMock(return_value={"qwen3-4b"}),
        ):
            with self.assertRaisesRegex(Exception, "qwen3-4b"):
                await ensure_qwen_server(
                    service, "qwen3-8b", auto_start=True, timeout_seconds=30
                )

    async def test_llm_writes_only_text_values_and_code_assembles_json(self):
        service = AsyncMock()
        service.generate.return_value = "Nhận xét được viết từ facts đã cung cấp."

        result = await generate_assessment(service, participant_name=NAME,
            participant_email=EMAIL, questionnaire=questionnaire(), game_results=[lawyer_game()],
            completed_at=COMPLETED_AT, max_prompt_chars=16_000, max_tokens=512)

        self.assertEqual(result.participant_name, NAME)
        self.assertEqual(len(result.dimension_levels), 28)
        self.assertGreater(service.generate.await_count, 10)
        for call in service.generate.await_args_list:
            messages = call.args[0]
            self.assertIn('"field":', messages[1]["content"])
            self.assertNotIn("response_format", call.kwargs["options"])


if __name__ == "__main__":
    unittest.main()
