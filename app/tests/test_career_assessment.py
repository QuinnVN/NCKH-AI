import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

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
        '"match_percentage":81}]}'
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
        self.assertIn("không viết", messages[0]["content"].lower())

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

    def test_rejects_changed_name_count_extra_fields_and_invalid_percentage(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        cases = (
            valid_model_answer().replace('"career_name":"Bác sĩ"', '"career_name":"Luật sư"'),
            valid_model_answer().replace(
                '"match_percentage":81',
                '"match_percentage":81,"evaluation":"Không hợp lệ"',
            ),
            valid_model_answer().replace('"match_percentage":81', '"match_percentage":101'),
            '{"assessment_id":"assessment-001","results":[]}',
        )

        for content in cases:
            with self.subTest(content=content), self.assertRaises(CareerAssessmentOutputError):
                parse_assessment_response(content, request)

    def test_accepts_28_dimensions_and_28_criteria(self):
        data = valid_request_data()
        data["dimensions"] = [
            {
                "id": f"dimension_{index}",
                "name": f"Nhóm {index}",
                "description": "Mô tả nhóm.",
                "score": index,
            }
            for index in range(1, 29)
        ]
        data["careers"][0]["criteria"] = [
            {"dimension_id": f"dimension_{index}", "importance": 3}
            for index in range(1, 29)
        ]

        request = CareerAssessmentRequest.model_validate(data)

        self.assertEqual(len(request.dimensions), 28)
        self.assertEqual(len(request.careers[0].criteria), 28)

    def test_rejects_29_dimensions(self):
        data = valid_request_data()
        data["dimensions"] = [
            {
                "id": f"dimension_{index}",
                "name": f"Nhóm {index}",
                "description": "Mô tả nhóm.",
                "score": index % 101,
            }
            for index in range(1, 30)
        ]

        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(data)

    def test_rejects_29_criteria(self):
        data = valid_request_data()
        data["dimensions"] = [
            {
                "id": f"dimension_{index}",
                "name": f"Nhóm {index}",
                "description": "Mô tả nhóm.",
                "score": index,
            }
            for index in range(1, 29)
        ]
        data["careers"][0]["criteria"] = [
            {"dimension_id": f"dimension_{index}", "importance": 3}
            for index in range(1, 30)
        ]

        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(data)


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
        self.assertEqual(kwargs["options"]["temperature"], 0.2)
        self.assertEqual(kwargs["options"]["response_format"]["type"], "json_schema")
        self.assertTrue(kwargs["options"]["response_format"]["json_schema"]["strict"])
        self.assertEqual(
            set(
                kwargs["options"]["response_format"]["json_schema"]["schema"]["properties"]["results"]["items"]["properties"]
            ),
            {"career_id", "career_name", "match_percentage"},
        )
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

    async def test_invalid_repair_returns_502_after_exactly_two_requests(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        service = AsyncMock()
        service.configured = True
        service.generate.side_effect = ["not-json", "still-not-json"]

        with patch.object(main, "llm_service", service), patch.object(main, "log"):
            with self.assertRaises(HTTPException) as context:
                await main.assess_careers(request)

        self.assertEqual(context.exception.status_code, 502)
        self.assertEqual(service.generate.await_count, 2)


if __name__ == "__main__":
    unittest.main()
