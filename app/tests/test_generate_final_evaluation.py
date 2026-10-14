import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from app.config import get_settings as load_settings
from app.final_evaluation_openrouter import FINAL_EVALUATION_MODEL, FinalEvaluationProviderError
from app.llm_service import LLMServiceError
from app.final_evaluation import DIMENSION_IDS, FinalAssessment, FinalEvaluationOutputError
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
    choose_participant,
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


def structured_answer(messages, **kwargs):
    payload = json.loads(messages[1]["content"].split("\n/no_think")[0])
    schema = kwargs["options"]["response_format"]["json_schema"]["schema"]
    findings = {
        finding_id: {key: "analysis" if key == "icon" else f"Nhận xét {key} cho {finding_id}."
                     for key in spec["required"]}
        for finding_id, spec in schema["properties"]["findings"]["properties"].items()
    }
    return json.dumps({
        "stageAssessments": {group: f"Nhận định có căn cứ cho nhóm {group}." for group in "DESMAP"},
        "findings": findings,
        "careerSuggestions": {
            career_id: "Nhận xét dựa trên dữ liệu. Có dẫn chứng trong hồ sơ. Bạn có thể thử một hoạt động phù hợp."
            for career_id in payload["careerSuggestions"]
        },
        "finalEvaluation": {field: "Nhận xét được viết từ facts đã cung cấp."
                            for field in payload["finalEvaluation"]["fields"]},
    }, ensure_ascii=False)


def mixed_games() -> list[dict]:
    doctor = {"gameId": "doctor", "data": {"cases": [{
        "categorizationAccuracyPercent": 80,
        "essentialCategorizationAccuracyPercent": 60,
    }]}}
    clinic = {"gameId": "clinic", "data": {"patientResults": [
        {"scoreDelta": 1}, {"scoreDelta": -1},
    ]}}
    return [lawyer_game(), doctor, clinic]


class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, service):
        return await generate_assessment(service, participant_name=NAME,
            participant_email=EMAIL, questionnaire=questionnaire(), game_results=mixed_games(),
            completed_at=COMPLETED_AT, max_prompt_chars=60_000, max_tokens=12_000)

    async def test_repairs_invalid_fields_in_one_follow_up_request(self):
        service = AsyncMock()
        def answer(messages, **kwargs):
            if service.generate.await_count == 1:
                document = json.loads(structured_answer(messages, **kwargs))
                document["stageAssessments"]["D"] = "INSUFFICIENT_EVIDENCE"
                document["finalEvaluation"]["headline"] = "Quá dài. " * 50
                return json.dumps(document, ensure_ascii=False)
            return structured_answer(messages, **kwargs)
        service.generate.side_effect = answer

        result = await self.generate(service)

        self.assertEqual(service.generate.await_count, 2)
        repair = service.generate.await_args_list[1]
        self.assertEqual([item["role"] for item in repair.args[0]], ["system", "user", "assistant", "user"])
        self.assertIn("stageAssessments.D", repair.args[0][-1]["content"])
        self.assertIn("finalEvaluation.headline", repair.args[0][-1]["content"])
        self.assertEqual(repair.kwargs["options"]["temperature"], 0.0)
        self.assertIn("response_format", repair.kwargs["options"])
        self.assertEqual(result.stage_assessments["D"], "Nhận định có căn cứ cho nhóm D.")

    async def test_rejects_output_that_stays_invalid_after_repair(self):
        service = AsyncMock()
        service.generate.return_value = "không phải JSON"
        with self.assertRaises(FinalEvaluationOutputError):
            await self.generate(service)
        self.assertEqual(service.generate.await_count, 2)

    async def test_invalid_icon_is_omitted(self):
        service = AsyncMock()
        def answer(messages, **kwargs):
            document = json.loads(structured_answer(messages, **kwargs))
            for finding in document["findings"].values():
                finding["icon"] = "unknown-icon"
            return json.dumps(document, ensure_ascii=False)
        service.generate.side_effect = answer
        result = await self.generate(service)
        self.assertEqual(service.generate.await_count, 1)
        self.assertTrue(all(item.icon is None for item in result.behaviour_comparison.findings))

    async def test_existing_evaluation_is_skipped_in_single_and_all_modes(self):
        settings = SimpleNamespace(mongodb_uri="mongodb://example.invalid", mongodb_database="test",
                                   pipeline_mode="openrouter")
        client = MagicMock()
        database = MagicMock()
        client.__getitem__.return_value = database
        evaluation_collection = MagicMock()
        evaluation_collection.distinct.return_value = [NAME]
        database.__getitem__.side_effect = lambda name: evaluation_collection if name == "final_evaluations" else MagicMock()
        service = MagicMock()
        service.close = AsyncMock()
        with patch("scripts.generate_final_evaluation.get_settings", return_value=settings), patch("scripts.generate_final_evaluation.MongoClient", return_value=client), patch(
            "scripts.generate_final_evaluation.FinalEvaluationOpenRouter", return_value=service
        ), patch("scripts.generate_final_evaluation.list_participant_names", return_value=[NAME]), patch(
            "scripts.generate_final_evaluation.load_participant_data"
        ) as load, patch("subprocess.Popen") as start, patch(
            "builtins.print"
        ):
            self.assertEqual(await run(parse_args(["--participant", NAME])), 0)
            self.assertEqual(await run(parse_args(["--all"])), 0)
        load.assert_not_called()
        start.assert_not_called()

    async def test_all_mode_evaluates_remaining_participants_once(self):
        other_name = "Nguyễn Văn Bình"
        other_email = "binh@example.com"
        answer = valid_answer()
        answer["participantName"] = other_name
        answer["participantEmail"] = other_email
        assessment = FinalAssessment.model_validate(answer)
        with patch.dict("os.environ", {
            "MONGODB_URI": "mongodb://example.invalid",
            "LLM_USE_AMD_HYBRID": "true",
            "LLM_BASE_URL": "http://127.0.0.1:8080/v1",
            "OPENROUTER_API_KEY": "test-key",
            "PIPELINE_MODE": "openrouter",
        }):
            settings = load_settings()
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
            "scripts.generate_final_evaluation.MongoClient", return_value=client
        ), patch("scripts.generate_final_evaluation.FinalEvaluationOpenRouter", return_value=service) as service_factory, patch(
            "scripts.generate_final_evaluation.list_participant_names", return_value=[NAME, other_name]
        ), patch(
            "scripts.generate_final_evaluation.load_participant_data",
            return_value=(questionnaire(), other_email, [lawyer_game()], other_name),
        ) as load, patch("subprocess.Popen") as start, patch(
            "scripts.generate_final_evaluation.generate_assessment", new_callable=AsyncMock, return_value=assessment
        ) as generate, patch("scripts.generate_final_evaluation.save_assessment") as save, patch("builtins.print"):
            self.assertEqual(await run(parse_args(["--all"])), 0)
        load.assert_called_once()
        self.assertEqual(load.call_args.args[-1], other_name)
        self.assertTrue(settings.llm_use_amd_hybrid)
        self.assertEqual(service_factory.call_args.args[0], "test-key")
        self.assertEqual(service_factory.call_args.kwargs, {"timeout_seconds": 180})
        start.assert_not_called()
        generate.assert_awaited_once()
        save.assert_called_once_with(
            evaluation_collection, assessment, provider="openrouter", model=FINAL_EVALUATION_MODEL
        )

    async def test_legacy_mode_writes_with_the_local_llm(self):
        settings = SimpleNamespace(
            mongodb_uri="mongodb://example.invalid", mongodb_database="test",
            openrouter_api_key="test-key", pipeline_mode="legacy",
        )
        client = MagicMock()
        evaluation_collection = MagicMock()
        evaluation_collection.distinct.return_value = []
        evaluation_collection.count_documents.return_value = 0
        client.__getitem__.return_value.__getitem__.return_value = evaluation_collection
        local = MagicMock(provider="llama.cpp", model="qwen3-4b")
        local.generate = AsyncMock(side_effect=LLMServiceError("The language model is unavailable."))
        local.close = AsyncMock()
        with patch("scripts.generate_final_evaluation.get_settings", return_value=settings), patch(
            "scripts.generate_final_evaluation.MongoClient", return_value=client
        ), patch("scripts.generate_final_evaluation.LLMService", return_value=local), patch(
            "scripts.generate_final_evaluation.FinalEvaluationOpenRouter"
        ) as openrouter, patch(
            "scripts.generate_final_evaluation.list_participant_names", return_value=[NAME]
        ), patch(
            "scripts.generate_final_evaluation.load_participant_data",
            return_value=(questionnaire(), EMAIL, [lawyer_game()], NAME),
        ), patch("scripts.generate_final_evaluation.save_assessment") as save, patch("builtins.print"):
            with self.assertRaises(FinalEvaluationProviderError):
                await run(parse_args(["--participant", NAME]))
        openrouter.assert_not_called()
        local.generate.assert_awaited()
        save.assert_not_called()
        local.close.assert_awaited_once()

    def test_legacy_no_start_flag_remains_accepted_without_a_local_runtime(self):
        self.assertTrue(parse_args(["--all", "--no-start-llm"]).no_start_llm)

    async def test_provider_failure_does_not_save_an_incomplete_evaluation(self):
        settings = SimpleNamespace(
            mongodb_uri="mongodb://example.invalid", mongodb_database="test",
            openrouter_api_key="test-key", pipeline_mode="openrouter",
        )
        client = MagicMock()
        evaluation_collection = MagicMock()
        evaluation_collection.distinct.return_value = []
        evaluation_collection.count_documents.return_value = 0
        client.__getitem__.return_value.__getitem__.return_value = evaluation_collection
        service = MagicMock()
        service.close = AsyncMock()
        with patch("scripts.generate_final_evaluation.get_settings", return_value=settings), patch(
            "scripts.generate_final_evaluation.MongoClient", return_value=client
        ), patch("scripts.generate_final_evaluation.FinalEvaluationOpenRouter", return_value=service), patch(
            "scripts.generate_final_evaluation.list_participant_names", return_value=[NAME]
        ), patch(
            "scripts.generate_final_evaluation.load_participant_data",
            return_value=(questionnaire(), EMAIL, [lawyer_game()], NAME),
        ), patch(
            "scripts.generate_final_evaluation.generate_assessment", new_callable=AsyncMock,
            side_effect=FinalEvaluationProviderError("OpenRouter chưa thể tạo final eval."),
        ), patch("scripts.generate_final_evaluation.save_assessment") as save, patch("builtins.print"):
            with self.assertRaises(FinalEvaluationProviderError):
                await run(parse_args(["--participant", NAME]))
        save.assert_not_called()
        service.close.assert_awaited_once()
        client.close.assert_called_once()

    async def test_llm_writes_all_prose_in_one_structured_request_and_code_assembles_json(self):
        service = AsyncMock()
        service.generate.side_effect = structured_answer

        result = await self.generate(service)

        self.assertEqual(service.generate.await_count, 1)
        call = service.generate.await_args_list[0]
        messages = call.args[0]
        payload = json.loads(messages[1]["content"].split("\n/no_think")[0])
        response_format = call.kwargs["options"]["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]),
                         {"stageAssessments", "findings", "careerSuggestions", "finalEvaluation"})

        self.assertEqual(result.participant_name, NAME)
        self.assertEqual(len(result.dimension_levels), 28)
        self.assertEqual(len(result.career_suggestions), 7)
        self.assertGreater(len({item.compatibility_percent for item in result.career_suggestions}), 1)
        self.assertEqual(result.behaviour_comparison.experience_name, "Luật sư, Bác sĩ")
        self.assertTrue({"confirmed", "emerging"}.issubset(
            {item.kind for item in result.behaviour_comparison.findings}
        ))
        self.assertTrue(all("(" not in item.title for item in result.behaviour_comparison.findings))
        self.assertTrue(all(item.icon == "analysis" for item in result.behaviour_comparison.findings))
        self.assertTrue(all(
            item.remedy is None if item.kind == "confirmed" else bool(item.remedy)
            for item in result.behaviour_comparison.findings
        ))

        finding_schemas = schema["properties"]["findings"]["properties"]
        self.assertEqual(list(finding_schemas), [item.id for item in result.behaviour_comparison.findings])
        for finding in result.behaviour_comparison.findings:
            section = payload["findings"][finding.id]
            has_remedy = finding.kind in {"emerging", "development"}
            self.assertEqual("remedy" in finding_schemas[finding.id]["required"], has_remedy)
            self.assertEqual("remedy" in section["fields"], has_remedy)
            self.assertEqual(bool(section["facts"].get("learningActivities")), has_remedy)
            self.assertEqual(section["facts"]["kindCalculatedByBackend"], finding.kind)
        self.assertTrue(all("game" not in section["fields"]["remedy"]["instruction"].casefold()
                            for section in payload["findings"].values() if "remedy" in section["fields"]))

        final_section = payload["finalEvaluation"]
        has_development = any(item.kind == "development" for item in result.behaviour_comparison.findings)
        self.assertEqual(final_section["facts"]["developmentFinding"] is not None, has_development)
        self.assertIn("không gọi điểm thấp nhất là điểm yếu",
                      final_section["fields"]["challenge"]["instruction"])

        career_sections = list(payload["careerSuggestions"].values())
        self.assertEqual(list(payload["careerSuggestions"]), [item.id for item in result.career_suggestions])
        self.assertFalse(any("compatibilityPercent" in str(spec)
                             for spec in schema["properties"]["careerSuggestions"]["properties"].values()),
                         "Career percentages must come from occupational weights, not AI scores")
        self.assertTrue(all(len(item["facts"]["questionnaireEvidence"]) >= 2 for item in career_sections))
        self.assertTrue(all(item["facts"]["vrEvidence"] for item in career_sections))
        self.assertTrue(all(item["facts"]["exploratoryActivity"] for item in career_sections))
        self.assertIn("3-4 câu ngắn", career_sections[0]["description"]["instruction"])
        self.assertTrue(all("2-3 câu ngắn" in item["description"]["instruction"] for item in career_sections[1:]))
        # The generic answer cites no evidence, so every description falls back to the template.
        self.assertTrue(all("/100" in item.description for item in result.career_suggestions))
        import re
        for index, career in enumerate(result.career_suggestions):
            count = len(re.split(r'(?<=[.!?])\s+', career.description))
            self.assertIn(count, (3, 4) if index == 0 else (2, 3))


if __name__ == "__main__":
    unittest.main()
