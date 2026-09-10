import unittest
from unittest.mock import AsyncMock, patch

from app import main
from app.career_assessment import (
    CareerAssessmentOutputError,
    CareerAssessmentRequest,
    build_assessment_messages,
    parse_assessment_response,
)


def valid_request_data() -> dict:
    return {
        "assessment_id": "assessment-001",
        "dimensions": [
            {
                "id": "analytical_thinking",
                "name": "Tư duy phân tích",
                "description": "Khả năng phân tích dữ kiện và giải quyết vấn đề.",
                "score": 82,
            },
            {
                "id": "communication",
                "name": "Giao tiếp",
                "description": "Khả năng lắng nghe và diễn đạt rõ ràng.",
                "score": 74,
            },
        ],
        "careers": [
            {
                "id": "doctor",
                "name": "Bác sĩ",
                "description": "Khám, chẩn đoán và điều trị cho người bệnh.",
                "criteria": [
                    {"dimension_id": "analytical_thinking", "importance": 5},
                    {"dimension_id": "communication", "importance": 4},
                ],
            }
        ],
    }


def valid_model_answer() -> str:
    return (
        '{"assessment_id":"assessment-001","results":['
        '{"career_id":"doctor","career_name":"Bác sĩ",'
        '"match_percentage":81,"evaluation":"Bạn có nền tảng tư duy phân tích '
        'tốt và khả năng giao tiếp khá phù hợp với nghề bác sĩ. Bạn nên tiếp tục '
        'rèn luyện kỹ năng lắng nghe trong các tình huống áp lực."}]}'
    )


class CareerAssessmentContractTests(unittest.TestCase):
    def test_contract_has_no_schema_version(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        self.assertNotIn("schema_version", request.model_dump())

        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(
                {**valid_request_data(), "schema_version": "1.0"}
            )

    def test_rejects_duplicate_and_unknown_dimension_references(self):
        duplicate = valid_request_data()
        duplicate["dimensions"].append(dict(duplicate["dimensions"][0]))
        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(duplicate)

        unknown = valid_request_data()
        unknown["careers"][0]["criteria"][0]["dimension_id"] = "missing"
        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(unknown)

    def test_thinking_is_enabled_only_in_assessment_message(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        messages = build_assessment_messages(request)

        self.assertTrue(messages[-1]["content"].endswith("/think"))
        self.assertNotIn("schema_version", messages[-1]["content"])

    def test_parses_valid_response_and_removes_hidden_reasoning(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        content = f"<think>private reasoning</think>\n{valid_model_answer()}"

        response = parse_assessment_response(content, request)

        self.assertEqual(response.assessment_id, "assessment-001")
        self.assertEqual(response.results[0].match_percentage, 81)
        self.assertNotIn("schema_version", response.model_dump())

    def test_rejects_changed_identity_or_order(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        changed = valid_model_answer().replace('"career_id":"doctor"', '"career_id":"lawyer"')

        with self.assertRaises(CareerAssessmentOutputError):
            parse_assessment_response(changed, request)


class CareerAssessmentEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_result_is_returned_with_structured_generation_options(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        service = AsyncMock()
        service.configured = True
        service.generate.return_value = valid_model_answer()

        with patch.object(main, "llm_service", service), patch.object(main, "log"):
            result = await main.assess_careers(request)

        self.assertEqual(result.results[0].match_percentage, 81)
        _, kwargs = service.generate.await_args
        self.assertEqual(kwargs["options"]["response_format"]["type"], "json_schema")
        self.assertEqual(kwargs["options"]["max_tokens"], 4096)

    async def test_invalid_first_answer_is_repaired_once_without_thinking(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        service = AsyncMock()
        service.configured = True
        service.generate.side_effect = ["not-json", valid_model_answer()]

        with patch.object(main, "llm_service", service), patch.object(main, "log"):
            result = await main.assess_careers(request)

        self.assertEqual(result.results[0].career_id, "doctor")
        self.assertEqual(service.generate.await_count, 2)
        repair_messages = service.generate.await_args_list[1].args[0]
        self.assertTrue(repair_messages[-1]["content"].endswith("/no_think"))


if __name__ == "__main__":
    unittest.main()
