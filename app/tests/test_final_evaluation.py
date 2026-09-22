import unittest

from app.final_evaluation import (
    DIMENSION_IDS,
    FinalAssessment,
    FinalEvaluationOutputError,
    build_text_field_messages,
    parse_text_field,
)


NAME = "Nguyễn Văn An"
EMAIL = "an@example.com"
COMPLETED_AT = "2026-09-22T10:30:00.000Z"


def valid_answer() -> dict:
    return {"version": 1, "participantName": NAME, "participantEmail": EMAIL,
        "completedAt": COMPLETED_AT,
        "stageAssessments": {key: f"Nhận định có căn cứ cho nhóm {key}." for key in "DESMAP"},
        "dimensionLevels": {key: "neutral" for key in DIMENSION_IDS},
        "behaviourComparison": {"experienceName": "Luật sư", "findings": []},
        "careerSuggestions": [{"id": "luat-su", "name": "Luật sư",
            "compatibilityPercent": 82, "description": "Đây là hướng nghề có căn cứ để khám phá."}],
        "finalEvaluation": {"experienceName": "Luật sư", "headline": "Bạn kiểm tra dữ kiện trước khi chọn hướng.",
            "workStyle": "Bạn đối chiếu dữ kiện trước khi kết luận.", "benefit": "Cách này giúp quyết định có căn cứ.",
            "challenge": "Bạn cần ưu tiên nhanh hơn khi thời gian ngắn.",
            "improvement": "Hãy chọn ba dữ kiện quan trọng trước khi xử lý.",
            "strengthLabel": "Phân tích dữ kiện", "developmentLabel": "Ưu tiên công việc",
            "evidence": "Bạn dùng các bằng chứng chính trong tình huống VR."}}


class FinalEvaluationContractTests(unittest.TestCase):
    def test_contract_accepts_complete_backend_assembled_document(self):
        result = FinalAssessment.model_validate(valid_answer())
        self.assertEqual(result.participant_email, EMAIL)
        self.assertEqual(len(result.dimension_levels), 28)

    def test_contract_rejects_missing_dimension(self):
        answer = valid_answer()
        answer["dimensionLevels"].pop("P6")
        with self.assertRaises(ValueError):
            FinalAssessment.model_validate(answer)

    def test_text_prompt_requests_one_value_and_no_json(self):
        messages = build_text_field_messages(field="finalEvaluation.headline",
            instruction="Viết một câu.", facts={"scoreCalculatedByBackend": 75}, max_characters=200)
        self.assertIn('"field":"finalEvaluation.headline"', messages[1]["content"])
        self.assertTrue(messages[1]["content"].endswith("/no_think"))
        self.assertIn("Không trả JSON", messages[0]["content"])

    def test_text_parser_rejects_json_and_insufficient_evidence(self):
        self.assertEqual(parse_text_field("Một nhận xét cụ thể.", max_characters=100), "Một nhận xét cụ thể.")
        for answer in ('{"headline":"x"}', "INSUFFICIENT_EVIDENCE"):
            with self.subTest(answer=answer), self.assertRaises(FinalEvaluationOutputError):
                parse_text_field(answer, max_characters=100)


if __name__ == "__main__":
    unittest.main()
