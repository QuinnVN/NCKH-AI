import json
import asyncio
from pathlib import Path
import tempfile
import unittest

from scripts.evaluate_sales_jev import evaluate, observations_report, prepare, calibration_curves, predict
from app.sales_openrouter import QUESTION_SET_VERSION, THRESHOLD_VERSION


def reference(case='case', session='session', speaker='speaker', split='holdout'):
    return dict(caseId=case, sourceSessionId=session, speakerGroupId=speaker, split=split,
                referenceLabels={'apology': True, 'unauthorizedRefund': False},
                review={'independent': True, 'reviewerId': 'human', 'reviewedAtUtc': '2026-10-03T00:00:00Z'})


def prediction(case='case', **labels):
    return dict(caseId=case, questionSetVersion=QUESTION_SET_VERSION, thresholdVersion=THRESHOLD_VERSION,
                labels={label: {'noul': value} for label, value in labels.items()})


class SalesEvaluationTests(unittest.TestCase):
    def test_uncertain_and_missing_predictions_do_not_become_negative_decisions(self):
        report = evaluate([reference()], [prediction(apology=.81, unauthorizedRefund=.89)])
        self.assertEqual(report['perLabel']['apology']['tp'], 1)
        self.assertEqual(report['perLabel']['unauthorizedRefund']['uncertain'], 1)
        self.assertEqual(report['falsePenaltyLabelDecisions'], 0)
        missing = evaluate([reference()], [])
        self.assertEqual(missing['missingPredictionTurns'], 1)
        self.assertEqual(missing['perLabel']['apology']['fn'], 0)
        self.assertEqual(missing['perLabel']['apology']['recallIncludingAbstention'], 0)

    def test_false_penalty_and_small_sample_uncertainty_are_reported(self):
        report = evaluate([reference()], [prediction(apology=.9, unauthorizedRefund=.99)])
        self.assertEqual(report['falsePenaltyLabelDecisions'], 1)
        self.assertLess(report['perLabel']['apology']['precisionWilson95'][0], .98)
        self.assertFalse(report['rolloutApproved'])

    def test_session_and_speaker_variants_cannot_cross_splits(self):
        for second in [reference('second', 'session', 'other', 'calibration'),
                       reference('second', 'other', 'speaker', 'calibration')]:
            with self.assertRaisesRegex(ValueError, 'leaks'):
                evaluate([reference(), second], [])

    def test_reference_review_cannot_be_model_generated_or_unattributed(self):
        row = reference()
        row['review']['independent'] = False
        with self.assertRaisesRegex(ValueError, 'independent'):
            evaluate([row], [])

    def test_versions_and_duplicate_predictions_are_not_silently_mixed(self):
        with self.assertRaisesRegex(ValueError, 'unique'):
            evaluate([reference()], [prediction(), prediction()])
        second = prediction('second')
        second['thresholdVersion'] = 'different'
        with self.assertRaisesRegex(ValueError, 'frozen'):
            evaluate([reference(), reference('second', 'another', 'another')], [prediction(), second])

    def test_prepare_does_not_copy_stored_labels_into_gold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sales-session-a.json'
            path.write_text(json.dumps({'sessionId': 'a', 'turns': [{'turnId': 'b', 'transcript': 'Dạ chị.', 'turnAssessment': {'abuse': True}}]}), encoding='utf-8')
            rows = prepare(Path(directory))
            self.assertIsNone(rows[0]['referenceLabels'])
            self.assertIsNone(rows[0]['transcriptCorrected'])
            self.assertFalse(rows[0]['review']['independent'])
            self.assertNotIn('turnAssessment', rows[0])

    def test_missing_cost_and_text_only_latency_cannot_claim_end_to_end_acceptance(self):
        report = observations_report([{'jevSeconds': .4, 'usageCostUsd': .001}, {'jevSeconds': .5}])
        self.assertEqual(report['missingCostSamples'], 1)
        self.assertEqual(report['timings']['endOfSpeechToPlaybackSeconds']['samples'], 0)
        self.assertFalse(report['headsetVerified'])

    def test_calibration_curves_exclude_holdout_and_do_not_apply_thresholds(self):
        report = calibration_curves([reference(), reference('cal', 'cal-session', 'cal-speaker', 'calibration')],
                                    [prediction('cal', apology=.81, unauthorizedRefund=.89)])
        self.assertEqual(report['reviewedTurns'], 1)
        self.assertFalse(report['thresholdsAppliedToProduction'])
        self.assertEqual(report['acceptanceCurves']['apology'][0]['tp'], 1)

    def test_unknown_threshold_version_and_huge_noul_are_safe(self):
        huge = prediction(apology=10 ** 400, unauthorizedRefund=0)
        self.assertEqual(evaluate([reference()], [huge])['perLabel']['apology']['uncertain'], 1)
        huge['thresholdVersion'] = 'unrecognized'
        with self.assertRaisesRegex(ValueError, 'Unknown threshold'):
            evaluate([reference()], [huge])

    def test_malformed_versions_and_observation_overflow_are_rejected(self):
        row = prediction()
        row['questionSetVersion'] = ['invalid']
        with self.assertRaisesRegex(ValueError, 'frozen'):
            evaluate([reference()], [row])
        report = observations_report([{'jevSeconds': 10 ** 400, 'usageCostUsd': 10 ** 400}])
        self.assertEqual(report['timings']['jevSeconds']['samples'], 0)
        self.assertEqual(report['missingCostSamples'], 1)

    def test_prediction_does_not_send_reference_labels_and_failure_is_private(self):
        class Service:
            async def classify(self, transcript, context):
                self.context = context
                if transcript == 'failure':
                    raise RuntimeError('private provider input')
                return {'labels': {'apology': {'noul': .9}}, 'metadata': {'usage': {'cost': .001}}}
        service = Service()
        rows = [dict(reference(), transcriptStt='Dạ chị.', objective=1),
                dict(reference('second'), transcriptStt='failure')]
        result = asyncio.run(predict(rows, classifier=service))
        self.assertNotIn('referenceLabels', service.context)
        self.assertNotIn('review', service.context)
        self.assertEqual(result[0]['labels']['apology']['noul'], .9)
        self.assertEqual(result[1]['errorCode'], 'classifier_failed')
        self.assertNotIn('private', json.dumps(result))
