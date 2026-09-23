import unittest

from app.final_evaluation import (
    DIMENSION_IDS,
    FinalAssessment,
    FinalEvaluationOutputError,
    build_text_field_messages,
    parse_score_field,
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
        "behaviourComparison": {"experienceName": "Luật sư", "findings": [
            {"id": "information-processing", "kind": "confirmed", "title": "Xử lý thông tin",
             "questionnaireResult": "Bạn tự đánh giá cao.", "vrEvidence": "Bạn phân loại đúng.",
             "summary": "Hai nguồn phù hợp."},
            {"id": "decision-making", "kind": "emerging", "title": "Ra quyết định",
             "questionnaireResult": "Bạn tự đánh giá ở mức vừa.", "vrEvidence": "Bạn chọn đúng.",
             "summary": "VR thể hiện rõ hơn."},
            {"id": "problem-solving", "kind": "development", "title": "Giải quyết vấn đề",
             "questionnaireResult": "Bạn tự đánh giá cao.", "vrEvidence": "Bạn còn bỏ sót dữ kiện.",
             "summary": "Đây là hướng cần luyện."},
        ]},
        "careerSuggestions": [
            {"id": f"nghe-{index}", "name": f"Nghề {index}",
             "compatibilityPercent": 90 - index,
             "description": "Đây là hướng nghề có căn cứ để khám phá."}
            for index in range(1, 8)
        ],
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
        self.assertIsNone(result.behaviour_comparison.findings[0].remedy)

    def test_contract_accepts_no_development_finding(self):
        answer = valid_answer()
        answer["behaviourComparison"]["findings"] = answer["behaviourComparison"]["findings"][:2]
        result = FinalAssessment.model_validate(answer)
        self.assertEqual({item.kind for item in result.behaviour_comparison.findings},
                         {"confirmed", "emerging"})

    def test_finding_remedy_is_separate_from_summary(self):
        answer = valid_answer()
        finding = answer["behaviourComparison"]["findings"][2]
        finding["remedy"] = "Khi thông tin thay đổi, bạn có thể bỏ sót bước kiểm tra; hãy rà lại dữ kiện trước khi quyết định."
        parsed = FinalAssessment.model_validate(answer)
        self.assertEqual(parsed.behaviour_comparison.findings[2].summary, "Đây là hướng cần luyện.")
        self.assertEqual(parsed.behaviour_comparison.findings[2].remedy, finding["remedy"])
        finding["remedy"] = ""
        with self.assertRaises(ValueError):
            FinalAssessment.model_validate(answer)

    def test_confirmed_finding_cannot_have_remedy(self):
        answer = valid_answer()
        answer["behaviourComparison"]["findings"][0]["remedy"] = "Hãy luyện thêm."
        with self.assertRaisesRegex(ValueError, "confirmed findings must not have remedy"):
            FinalAssessment.model_validate(answer)

    def test_contract_rejects_missing_dimension(self):
        answer = valid_answer()
        answer["dimensionLevels"].pop("P6")
        with self.assertRaises(ValueError):
            FinalAssessment.model_validate(answer)

    def test_finding_icon_is_optional_and_restricted_to_supported_values(self):
        answer = valid_answer()
        finding = {"id": "information-processing", "kind": "confirmed",
                   "title": "Xử lý thông tin", "questionnaireResult": "Tự đánh giá cao.",
                   "vrEvidence": "Phân loại đúng.", "summary": "Hai nguồn phù hợp."}
        answer["behaviourComparison"]["findings"][0] = finding
        self.assertIsNone(FinalAssessment.model_validate(answer).behaviour_comparison.findings[0].icon)
        finding["icon"] = "analysis"
        self.assertEqual(FinalAssessment.model_validate(answer).behaviour_comparison.findings[0].icon, "analysis")
        finding["icon"] = "unknown"
        with self.assertRaises(ValueError):
            FinalAssessment.model_validate(answer)

    def test_contract_rejects_duplicate_finding_kinds(self):
        answer = valid_answer()
        finding = {"id": "information-processing", "kind": "confirmed",
                   "title": "Xử lý thông tin", "questionnaireResult": "Tự đánh giá cao.",
                   "vrEvidence": "Phân loại đúng.", "summary": "Hai nguồn phù hợp."}
        answer["behaviourComparison"]["findings"][1]["kind"] = "confirmed"
        with self.assertRaises(ValueError):
            FinalAssessment.model_validate(answer)

    def test_text_prompt_requests_one_value_and_no_json(self):
        messages = build_text_field_messages(field="finalEvaluation.headline",
            instruction="Viết một câu.", facts={"scoreCalculatedByBackend": 75}, max_characters=200)
        self.assertIn('"field":"finalEvaluation.headline"', messages[1]["content"])
        self.assertTrue(messages[1]["content"].endswith("/no_think"))
        self.assertIn("Không trả JSON", messages[0]["content"])
        self.assertIn("Không đưa nguyên văn các chuỗi như `scoreDelta`", messages[0]["content"])

    def test_text_parser_rejects_json_and_insufficient_evidence(self):
        self.assertEqual(parse_text_field("Một nhận xét cụ thể.", max_characters=100), "Một nhận xét cụ thể.")
        for answer in ('{"headline":"x"}', "INSUFFICIENT_EVIDENCE"):
            with self.subTest(answer=answer), self.assertRaises(FinalEvaluationOutputError):
                parse_text_field(answer, max_characters=100)

    def test_score_parser_accepts_only_one_integer(self):
        self.assertEqual(parse_score_field("82"), 82)
        for answer in ("82%", "101", "Điểm: 82", '{"score":82}'):
            with self.subTest(answer=answer), self.assertRaises(FinalEvaluationOutputError):
                parse_score_field(answer)


if __name__ == "__main__":
    unittest.main()
