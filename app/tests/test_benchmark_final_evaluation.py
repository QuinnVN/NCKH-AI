import unittest

from scripts.benchmark_final_evaluation import field_checks, minimal_game
from app.final_evaluation_calculation import calculate_behaviours, extract_dimensions
from app.tests.test_generate_final_evaluation import questionnaire, lawyer_game


class BenchmarkTests(unittest.TestCase):
    def test_whitelist_preserves_scores_without_identity_or_transcript(self):
        game = lawyer_game()
        game.update(participantName='Private Person', participantEmail='private@example.com')
        game['data']['lawyer']['transcript'] = 'Private utterance'
        clean = minimal_game(game)
        dims = extract_dimensions(questionnaire())
        self.assertEqual(calculate_behaviours(game, dims), calculate_behaviours(clean, dims))
        self.assertNotIn('Private', str(clean))

    def test_distinguishes_production_evidence_guard_from_case_insensitive_check(self):
        payload = {'field': 'careerSuggestions.test.description', 'maxCharacters': 1200,
                   'facts': {'questionnaireEvidence': [{'name': 'Phân tích', 'score': 75},
                                                       {'name': 'Sáng tạo', 'score': 80}],
                             'vrEvidence': [{'label': 'Phân loại dữ kiện',
                                             'evidence': 'Bác sĩ: phân loại dữ kiện', 'score': 70}]}}
        text = 'Bạn tự đánh giá Phân tích 75/100 và Sáng tạo 80/100. VR ghi nhận bác sĩ: phân loại dữ kiện 70/100.'
        checks = field_checks(payload, text)
        self.assertTrue(checks['twoQuestionnaireScores'])
        self.assertFalse(checks['vrEvidenceExactProduction'])
        self.assertTrue(checks['vrEvidenceCaseInsensitive'])
        self.assertTrue(checks['onlyProvidedScores'])
        checks = field_checks(payload, text + ' 99/100.')
        self.assertFalse(checks['onlyProvidedScores'])

    def test_checks_plain_text_and_vr_remedies(self):
        payload = {'field': 'behaviourComparison.findings.x.remedy', 'facts': {}, 'maxCharacters': 300}
        checks = field_checks(payload, 'Chơi lại VR để cải thiện điểm 75.')
        self.assertFalse(checks['outsideVrRemedy'])
        self.assertFalse(checks['noNumericScores'])
        self.assertTrue(field_checks(payload, 'Đọc tài liệu và thử phân tích 3 tình huống.')['noNumericScores'])
        self.assertFalse(field_checks(payload, '**Một hoạt động cụ thể.**')['strictPlainText'])


if __name__ == '__main__':
    unittest.main()
