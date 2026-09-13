import json
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
                "id": "creative_work",
                "name": "Hứng thú sáng tạo",
                "description": "Mức độ yêu thích việc tạo ra ý tưởng và sản phẩm mới.",
                "category": "interest",
                "score": 88,
            },
            {
                "id": "analytical_thinking",
                "name": "Tư duy phân tích",
                "description": "Khả năng phân tích dữ kiện và giải quyết vấn đề.",
                "category": "ability",
                "score": 82,
            },
            {
                "id": "communication",
                "name": "Giao tiếp",
                "description": "Khả năng lắng nghe và diễn đạt rõ ràng.",
                "category": "trait",
                "score": 74,
            },
        ],
    }


def valid_model_answer() -> str:
    return json.dumps(
        {
            "assessment_id": "assessment-001",
            "suggestions": [
                {
                    "career_name": "Nhà thiết kế trải nghiệm người dùng",
                    "match_percentage": 91,
                },
                {
                    "career_name": "Chuyên viên nghiên cứu thị trường",
                    "match_percentage": 84,
                },
                {
                    "career_name": "Chuyên viên truyền thông",
                    "match_percentage": 78,
                },
            ],
        },
        ensure_ascii=False,
    )


class CareerAssessmentContractTests(unittest.TestCase):
    def test_request_uses_categorized_dimensions_without_supplied_careers(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())

        self.assertEqual(request.dimensions[0].category, "interest")
        self.assertNotIn("careers", request.model_dump())

        old_request = {**valid_request_data(), "careers": []}
        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(old_request)

    def test_rejects_duplicate_dimensions_and_invalid_category(self):
        duplicate = valid_request_data()
        duplicate["dimensions"].append(dict(duplicate["dimensions"][0]))
        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(duplicate)

        invalid_category = valid_request_data()
        invalid_category["dimensions"][0]["category"] = "skill"
        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(invalid_category)

    def test_thinking_prompt_prioritizes_interests_and_requests_open_suggestions(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        messages = build_assessment_messages(request)
        system_prompt = messages[0]["content"]

        self.assertTrue(messages[-1]["content"].endswith("/think"))
        self.assertIn('"category":"interest"', messages[-1]["content"])
        self.assertIn("Ưu tiên", system_prompt)
        self.assertIn("Không giới hạn", system_prompt)
        self.assertIn("từ 1 đến 5 nghề", system_prompt)

    def test_parses_ranked_suggestions_and_removes_hidden_reasoning(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        content = f"<think>private reasoning</think>\n{valid_model_answer()}"

        response = parse_assessment_response(content, request)

        self.assertEqual(response.assessment_id, "assessment-001")
        self.assertEqual(len(response.suggestions), 3)
        self.assertEqual(response.suggestions[0].match_percentage, 91)
        self.assertEqual(
            set(response.suggestions[0].model_dump()),
            {"career_name", "match_percentage"},
        )

    def test_rejects_changed_assessment_id(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        changed = valid_model_answer().replace("assessment-001", "assessment-002")

        with self.assertRaises(CareerAssessmentOutputError):
            parse_assessment_response(changed, request)

    def test_rejects_invalid_suggestion_lists(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        base = json.loads(valid_model_answer())
        cases = []

        empty = {**base, "suggestions": []}
        cases.append(empty)

        too_many = {
            **base,
            "suggestions": [
                {"career_name": f"Nghề {index}", "match_percentage": 90 - index}
                for index in range(6)
            ],
        }
        cases.append(too_many)

        duplicate = {
            **base,
            "suggestions": [
                {"career_name": "Nhà thiết kế", "match_percentage": 90},
                {"career_name": "  NHÀ THIẾT KẾ  ", "match_percentage": 80},
            ],
        }
        cases.append(duplicate)

        out_of_order = {
            **base,
            "suggestions": [
                {"career_name": "Nhà thiết kế", "match_percentage": 70},
                {"career_name": "Nhà nghiên cứu", "match_percentage": 80},
            ],
        }
        cases.append(out_of_order)

        extra_field = json.loads(valid_model_answer())
        extra_field["suggestions"][0]["reason"] = "Không được phép"
        cases.append(extra_field)

        invalid_percentage = json.loads(valid_model_answer())
        invalid_percentage["suggestions"][0]["match_percentage"] = 101
        cases.append(invalid_percentage)

        for content in cases:
            with self.subTest(content=content), self.assertRaises(
                CareerAssessmentOutputError
            ):
                parse_assessment_response(
                    json.dumps(content, ensure_ascii=False),
                    request,
                )

    def test_accepts_28_categorized_dimensions(self):
        data = valid_request_data()
        data["dimensions"] = [
            {
                "id": f"dimension_{index}",
                "name": f"Nhóm {index}",
                "description": "Mô tả nhóm.",
                "category": "interest" if index % 2 else "ability",
                "score": index,
            }
            for index in range(1, 29)
        ]

        request = CareerAssessmentRequest.model_validate(data)

        self.assertEqual(len(request.dimensions), 28)

    def test_rejects_29_dimensions(self):
        data = valid_request_data()
        data["dimensions"] = [
            {
                "id": f"dimension_{index}",
                "name": f"Nhóm {index}",
                "description": "Mô tả nhóm.",
                "category": "other",
                "score": index % 101,
            }
            for index in range(1, 30)
        ]

        with self.assertRaises(ValueError):
            CareerAssessmentRequest.model_validate(data)


class CareerAssessmentEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_suggestions_use_structured_generation_options(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        service = AsyncMock()
        service.configured = True
        service.generate.return_value = valid_model_answer()

        with patch.object(main, "llm_service", service), patch.object(main, "log"):
            result = await main.assess_careers(request)

        self.assertEqual(result.suggestions[0].match_percentage, 91)
        _, kwargs = service.generate.await_args
        response_format = kwargs["options"]["response_format"]
        suggestion_schema = response_format["json_schema"]["schema"]["properties"][
            "suggestions"
        ]
        self.assertEqual(suggestion_schema["minItems"], 1)
        self.assertEqual(suggestion_schema["maxItems"], 5)
        self.assertEqual(
            set(suggestion_schema["items"]["properties"]),
            {"career_name", "match_percentage"},
        )
        self.assertEqual(kwargs["options"]["max_tokens"], 4096)

    async def test_invalid_first_answer_is_repaired_once_without_thinking(self):
        request = CareerAssessmentRequest.model_validate(valid_request_data())
        service = AsyncMock()
        service.configured = True
        service.generate.side_effect = ["not-json", valid_model_answer()]

        with patch.object(main, "llm_service", service), patch.object(main, "log"):
            result = await main.assess_careers(request)

        self.assertEqual(result.suggestions[0].career_name, "Nhà thiết kế trải nghiệm người dùng")
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
