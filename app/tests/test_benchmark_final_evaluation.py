import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts.benchmark_final_evaluation import (
    MODELS, benchmark_one, document_checks, document_fields, field_checks, minimal_game, summarize,
)
from app.final_evaluation_calculation import calculate_behaviours, extract_dimensions
from app.tests.test_generate_final_evaluation import (
    lawyer_game, mixed_games, questionnaire, structured_answer,
)


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


class BenchmarkRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_scores_one_structured_response_per_eval_offline(self):
        def respond(request):
            body = json.loads(request.content)
            self.assertEqual(body['model'], MODELS[0])
            self.assertEqual(body['response_format']['type'], 'json_schema')
            content = structured_answer(body['messages'], options={'response_format': body['response_format']})
            return httpx.Response(200, json={'id': 'gen', 'model': body['model'], 'provider': 'Test',
                                             'usage': {'cost': .01, 'prompt_tokens': 10, 'completion_tokens': 5},
                                             'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]})
        case = {'caseId': 'profile-01', 'questionnaire': questionnaire(), 'games': mixed_games(),
                'findingCount': 3, 'findingKinds': {}, 'gamesObserved': ['clinic', 'doctor', 'lawyer']}
        with tempfile.TemporaryDirectory() as folder, patch('builtins.print'):
            destination = Path(folder)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as network:
                result = await benchmark_one(case, MODELS[0], 1, destination, network, 'test-key')
            summary = summarize(destination)[MODELS[0]]
            attempts = json.loads((destination / 'runs' / MODELS[0].replace('/', '__')
                                   / 'profile-01-r1' / 'attempts.json').read_text(encoding='utf-8'))

        self.assertTrue(result['success'], result.get('error'))
        self.assertEqual(result['attemptCount'], 1)
        self.assertEqual(result['repairRequests'], 0)
        self.assertEqual(result['careerCount'], 7)
        self.assertEqual(result['careerFallbacks'], 7)
        self.assertEqual(attempts[0]['documentChecks'], {'validJson': True, 'allFieldsPresent': True})
        fields = document_fields(attempts[0]['payload'], attempts[0]['output'])
        self.assertIn('stageAssessments.D', {row['field'] for row in fields})
        self.assertTrue(any(row['field'].endswith('.remedy') for row in fields))
        self.assertEqual(summary['evalsCompleted'], 1)
        self.assertEqual(summary['documentChecks']['allFieldsPresent']['percent'], 100)
        self.assertEqual(summary['firstResponseChecks']['format']['tested'], len(fields))

    def test_missing_document_fields_fail_completeness(self):
        payload = {'stageAssessments': {'D': {'facts': {}, 'text': {'maxCharacters': 900}}},
                   'findings': {}, 'careerSuggestions': {},
                   'finalEvaluation': {'facts': {}, 'fields': {'headline': {'maxCharacters': 300}}}}
        self.assertEqual(document_checks(payload, '{"stageAssessments": {"D": "Nhận định."}}'),
                         {'validJson': True, 'allFieldsPresent': False})
        self.assertEqual(document_checks(payload, 'không phải JSON'),
                         {'validJson': False, 'allFieldsPresent': False})


if __name__ == '__main__':
    unittest.main()
