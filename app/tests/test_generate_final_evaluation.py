import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from app.final_evaluation import DIMENSION_IDS, FinalAssessment
from app.final_evaluation_calculation import (
    behavioural_alignment,
    calculate_behaviours,
    career_candidates,
    combine_game_results,
    dimension_level,
    extract_dimensions,
    group_scores,
    weighted_behaviour_data,
)
from scripts.generate_final_evaluation import (
    _write_icon,
    choose_participant,
    ensure_qwen_server,
    generate_assessment,
    parse_args,
    resolve_game_participant_name,
    run,
    save_assessment,
)
from app.tests.test_final_evaluation import COMPLETED_AT, EMAIL, NAME, valid_answer


def questionnaire() -> dict:
    return {"careerInterests": ["technology-engineering"],
            "dimensions": [{"id": code, "name": code, "description": f"Mô tả {code}",
                            "score": 50 + (index % 5) * 10}
                           for index, code in enumerate(DIMENSION_IDS)]}


def lawyer_game() -> dict:
    return {"gameId": "lawyer", "status": "completed", "data": {"lawyer": {
        "criterionScores": {"evidenceUse": 32, "logicalConnections": 28,
                            "conclusionFidelity": 12, "clarityAndPersuasiveness": 8}}}}


class CalculationTests(unittest.TestCase):
    def test_saving_omits_missing_optional_icon(self):
        answer = valid_answer()
        collection = Mock()
        save_assessment(collection, FinalAssessment.model_validate(answer))
        stored = collection.replace_one.call_args.args[1]
        self.assertNotIn("icon", stored["behaviourComparison"]["findings"][0])
        self.assertNotIn("remedy", stored["behaviourComparison"]["findings"][0])

    def test_choose_participant_retries_invalid_input(self):
        answers = iter(["x", "4", "2"])
        output = []
        selected = choose_participant(["An", "Bình"], input_fn=lambda _: next(answers), output_fn=output.append)
        self.assertEqual(selected, "Bình")
        self.assertEqual(choose_participant(["An"], input_fn=lambda _: "all", output_fn=lambda _: None), "all")

    def test_merges_all_games_and_repeat_runs_into_one_vr_summary(self):
        doctor = {"gameId": "doctor", "data": {"cases": [{
            "categorizationAccuracyPercent": 80,
            "essentialCategorizationAccuracyPercent": 60,
        }]}}
        clinic = {"gameId": "clinic", "data": {"patientResults": [
            {"scoreDelta": 1}, {"scoreDelta": -1},
        ]}}
        summary = combine_game_results(
            [lawyer_game(), lawyer_game(), doctor, clinic],
            extract_dimensions(questionnaire()),
        )
        self.assertEqual(summary.experience_name, "Luật sư, Bác sĩ")
        self.assertEqual(len(summary.weighted_results), 3)
        self.assertEqual(summary.weighted_results[0]["runCount"], 2)
        self.assertEqual(summary.behaviours["decision-making"].score, 63)
        self.assertGreaterEqual(len(summary.findings), 2)
        self.assertTrue({"confirmed", "emerging"}.issubset({item.kind for item in summary.findings}))
        self.assertEqual(len({item.behaviour_code for item in summary.findings}), len(summary.findings))
        self.assertTrue(all("(" not in item.title for item in summary.findings))
        self.assertTrue(any(item.kind in {"confirmed", "emerging"} for item in summary.findings))

    def test_single_good_game_does_not_force_a_development_item(self):
        summary = combine_game_results([lawyer_game()], extract_dimensions(questionnaire()))
        kinds = {item.kind for item in summary.findings}
        self.assertEqual(kinds, {"confirmed", "emerging"})

    def test_all_zero_vr_uses_relative_cards_without_claiming_absolute_strength(self):
        game = lawyer_game()
        game["data"]["lawyer"]["criterionScores"] = {
            "evidenceUse": 0, "logicalConnections": 0,
            "conclusionFidelity": 0, "clarityAndPersuasiveness": 0,
        }
        summary = combine_game_results([game], extract_dimensions(questionnaire()))
        self.assertEqual({item.kind for item in summary.findings},
                         {"confirmed", "emerging", "development"})
        self.assertIn("0/100", next(item for item in summary.findings if item.kind == "confirmed").vr_fact)
        self.assertIn("VR hiện chưa xác nhận", next(item for item in summary.findings if item.kind == "emerging").questionnaire_fact)

    def test_two_observed_clinic_behaviours_produce_two_distinct_cards(self):
        clinic = {"gameId": "clinic", "data": {"patientResults": [
            {"scoreDelta": 1}, {"scoreDelta": -1},
        ]}}
        summary = combine_game_results([clinic], extract_dimensions(questionnaire()))
        self.assertEqual(len(summary.findings), 2)
        self.assertEqual(len({item.id for item in summary.findings}), 2)
        self.assertEqual({item.kind for item in summary.findings},
                         {"confirmed", "emerging"})
        self.assertTrue(all("(" not in item.title for item in summary.findings))
        self.assertTrue(all("scoreDelta" not in item.vr_fact for item in summary.findings))

    def test_code_calculates_dimensions_and_weighted_behaviours(self):
        dimensions = extract_dimensions(questionnaire())
        groups = group_scores(dimensions)
        behaviours = calculate_behaviours(lawyer_game(), dimensions)
        alignment = behavioural_alignment("lawyer", behaviours)
        weighted = weighted_behaviour_data("lawyer", behaviours)

        self.assertEqual(len(dimensions), 28)
        self.assertEqual(dimension_level(81), "well-compatible")
        self.assertEqual(behaviours["problem-solving"].score, 80)
        self.assertIsInstance(alignment, int)
        self.assertEqual(weighted["problem-solving"]["weight"], 0.35)
        self.assertEqual(weighted["problem-solving"]["weightedContribution"], 28)

    def test_career_candidates_come_from_selected_interest_groups(self):
        candidates = career_candidates(["technology-engineering"])
        self.assertEqual(len(candidates), 7)
        self.assertTrue(all(item.interest_group == "technology-engineering" for item in candidates))

        broad_candidates = career_candidates(["exploring", "technology-engineering"])
        self.assertGreater(len(broad_candidates), 7)
        self.assertGreater(len({item.interest_group for item in broad_candidates}), 1)
        self.assertIn("ky-su-phan-mem", {item.id for item in candidates})

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
    async def test_invalid_icon_falls_back_to_kind(self):
        service = AsyncMock()
        service.generate.return_value = "unknown-icon"
        self.assertIsNone(await _write_icon(
            service, field="behaviourComparison.findings.test.icon",
            facts={"findingTitle": "Xử lý thông tin"}, max_prompt_chars=16_000,
        ))

    async def test_existing_evaluation_is_skipped_in_single_and_all_modes(self):
        settings = SimpleNamespace(mongodb_uri="mongodb://example.invalid", mongodb_database="test")
        client = MagicMock()
        database = MagicMock()
        client.__getitem__.return_value = database
        evaluation_collection = MagicMock()
        evaluation_collection.distinct.return_value = [NAME]
        database.__getitem__.side_effect = lambda name: evaluation_collection if name == "final_evaluations" else MagicMock()
        service = MagicMock()
        service.close = AsyncMock()
        with patch("scripts.generate_final_evaluation.get_settings", return_value=settings), patch(
            "scripts.generate_final_evaluation.replace", return_value=settings
        ), patch("scripts.generate_final_evaluation.MongoClient", return_value=client), patch(
            "scripts.generate_final_evaluation.LLMService", return_value=service
        ), patch("scripts.generate_final_evaluation.list_participant_names", return_value=[NAME]), patch(
            "scripts.generate_final_evaluation.load_participant_data"
        ) as load, patch("scripts.generate_final_evaluation.ensure_qwen_server", new_callable=AsyncMock) as start, patch(
            "builtins.print"
        ):
            self.assertEqual(await run(parse_args(["--participant", NAME])), 0)
            self.assertEqual(await run(parse_args(["--all"])), 0)
        load.assert_not_called()
        start.assert_not_awaited()

    async def test_all_mode_evaluates_remaining_participants_once(self):
        other_name = "Nguyễn Văn Bình"
        other_email = "binh@example.com"
        answer = valid_answer()
        answer["participantName"] = other_name
        answer["participantEmail"] = other_email
        assessment = FinalAssessment.model_validate(answer)
        settings = SimpleNamespace(mongodb_uri="mongodb://example.invalid", mongodb_database="test")
        client = MagicMock()
        database = MagicMock()
        client.__getitem__.return_value = database
        evaluation_collection = MagicMock()
        evaluation_collection.distinct.return_value = [NAME]
        evaluation_collection.count_documents.return_value = 0
        database.__getitem__.side_effect = lambda name: evaluation_collection if name == "final_evaluations" else MagicMock()
        service = MagicMock()
        service.close = AsyncMock()
        with patch("scripts.generate_final_evaluation.get_settings", return_value=settings), patch(
            "scripts.generate_final_evaluation.replace", return_value=settings
        ), patch("scripts.generate_final_evaluation.MongoClient", return_value=client), patch(
            "scripts.generate_final_evaluation.LLMService", return_value=service
        ), patch("scripts.generate_final_evaluation.list_participant_names", return_value=[NAME, other_name]), patch(
            "scripts.generate_final_evaluation.load_participant_data",
            return_value=(questionnaire(), other_email, [lawyer_game()], other_name),
        ) as load, patch("scripts.generate_final_evaluation.ensure_qwen_server", new_callable=AsyncMock) as start, patch(
            "scripts.generate_final_evaluation.generate_assessment", new_callable=AsyncMock, return_value=assessment
        ) as generate, patch("scripts.generate_final_evaluation.save_assessment") as save, patch("builtins.print"):
            self.assertEqual(await run(parse_args(["--all"])), 0)
        load.assert_called_once()
        self.assertEqual(load.call_args.args[-1], other_name)
        start.assert_awaited_once()
        generate.assert_awaited_once()
        save.assert_called_once_with(evaluation_collection, assessment)

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

    async def test_llm_writes_only_isolated_values_and_code_assembles_json(self):
        service = AsyncMock()
        def answer(messages, **_kwargs):
            payload = json.loads(messages[1]["content"].split("\n/no_think")[0])
            if payload["field"].endswith(".compatibilityPercent"):
                return "82"
            if payload["field"].endswith(".icon"):
                return "analysis"
            return "Nhận xét được viết từ facts đã cung cấp."
        service.generate.side_effect = answer

        doctor = {"gameId": "doctor", "data": {"cases": [{
            "categorizationAccuracyPercent": 80,
            "essentialCategorizationAccuracyPercent": 60,
        }]}}
        clinic = {"gameId": "clinic", "data": {"patientResults": [
            {"scoreDelta": 1}, {"scoreDelta": -1},
        ]}}
        result = await generate_assessment(service, participant_name=NAME,
            participant_email=EMAIL, questionnaire=questionnaire(),
            game_results=[lawyer_game(), doctor, clinic],
            completed_at=COMPLETED_AT, max_prompt_chars=16_000, max_tokens=512)

        self.assertEqual(result.participant_name, NAME)
        self.assertEqual(len(result.dimension_levels), 28)
        self.assertEqual(len(result.career_suggestions), 7)
        self.assertTrue(all(item.compatibility_percent == 82 for item in result.career_suggestions))
        self.assertEqual(result.behaviour_comparison.experience_name, "Luật sư, Bác sĩ")
        self.assertGreaterEqual(len(result.behaviour_comparison.findings), 2)
        self.assertTrue({"confirmed", "emerging"}.issubset(
            {item.kind for item in result.behaviour_comparison.findings}
        ))
        self.assertTrue(all("(" not in item.title for item in result.behaviour_comparison.findings))
        self.assertTrue(all(item.icon == "analysis" for item in result.behaviour_comparison.findings))
        self.assertTrue(all(
            item.remedy is None if item.kind == "confirmed" else bool(item.remedy)
            for item in result.behaviour_comparison.findings
        ))
        self.assertGreater(service.generate.await_count, 10)
        remedy_prompts = [
            json.loads(call.args[0][1]["content"].split("\n/no_think")[0])
            for call in service.generate.await_args_list
            if json.loads(call.args[0][1]["content"].split("\n/no_think")[0])["field"].endswith(".remedy")
        ]
        self.assertEqual(len(remedy_prompts), sum(
            item.kind in {"emerging", "development"}
            for item in result.behaviour_comparison.findings
        ))
        self.assertTrue(all(
            prompt["facts"]["kindCalculatedByBackend"] in {"emerging", "development"}
            for prompt in remedy_prompts
        ))
        self.assertIn("summary", remedy_prompts[0]["facts"])
        self.assertEqual(remedy_prompts[0]["facts"]["previousRemedies"], [])
        self.assertTrue(all(
            prompt["facts"]["learningActivities"]
            for prompt in remedy_prompts
        ))
        self.assertTrue(all("vrFact" not in prompt["facts"] for prompt in remedy_prompts))
        self.assertTrue(all("game" not in prompt["instruction"].casefold() for prompt in remedy_prompts))
        if len(remedy_prompts) > 1:
            first_remedy = next(
                item.remedy for item in result.behaviour_comparison.findings
                if item.remedy is not None
            )
            self.assertEqual(
                remedy_prompts[1]["facts"]["previousRemedies"],
                [first_remedy],
            )
        challenge_prompt = next(
            json.loads(call.args[0][1]["content"].split("\n/no_think")[0])
            for call in service.generate.await_args_list
            if json.loads(call.args[0][1]["content"].split("\n/no_think")[0])["field"]
            == "finalEvaluation.challenge"
        )
        has_development = any(
            item.kind == "development" for item in result.behaviour_comparison.findings
        )
        self.assertEqual(challenge_prompt["facts"]["developmentFinding"] is not None,
                         has_development)
        self.assertIn("không gọi điểm thấp nhất là điểm yếu", challenge_prompt["instruction"])
        score_prompts = [
            json.loads(call.args[0][1]["content"].split("\n/no_think")[0])
            for call in service.generate.await_args_list
            if json.loads(call.args[0][1]["content"].split("\n/no_think")[0])["field"].endswith(".compatibilityPercent")
        ]
        self.assertEqual(len(score_prompts), 7)
        self.assertEqual(
            {item["gameId"] for item in score_prompts[0]["facts"]["weightedVrResults"]},
            {"lawyer", "doctor", "clinic"},
        )
        for call in service.generate.await_args_list:
            messages = call.args[0]
            self.assertIn('"field":', messages[1]["content"])
            self.assertNotIn("response_format", call.kwargs["options"])


if __name__ == "__main__":
    unittest.main()
