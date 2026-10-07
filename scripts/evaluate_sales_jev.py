"""Prepare independent Sales review records and evaluate tri-state predictions.

Provider calls require the explicit predict command. Gameplay and rollout
settings are never changed.
Prepared transcripts are diagnostic exports and must follow recording retention.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.sales_openrouter import JevSalesClassifier, PENALTY_LABELS, QUESTION_SET_VERSION, THRESHOLD_VERSION  # noqa: E402


def read_records(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in records):
        raise ValueError('Each JSONL row must be an object')
    return records


def prepare(recordings: Path) -> list[dict[str, Any]]:
    """Never use stored model labels as independent reference labels."""
    records = []
    for path in sorted(recordings.glob('sales-session-*.json')):
        session = json.loads(path.read_text(encoding='utf-8-sig'))
        if session.get('diagnosticsDeleted'):
            continue
        for index, turn in enumerate(session.get('turns', [])):
            transcript = turn.get('transcript')
            if not isinstance(transcript, str) or not transcript.strip():
                continue
            session_id, turn_id = session.get('sessionId'), turn.get('turnId')
            if not session_id or not turn_id:
                continue
            evidence = next((entry for entry in session.get('evidenceLedger', [])
                             if isinstance(entry, dict) and entry.get('turnId') == turn_id), {})
            known_facts = evidence.get('factsKnownBefore')
            if not isinstance(known_facts, list):
                known_facts = sorted({fact for earlier in session.get('turns', [])[:index]
                                      for fact in earlier.get('disclosedFactIds', []) if isinstance(fact, str)})
            records.append({
                'caseId': f'{session_id}:{turn_id}',
                'sourceSessionId': session_id,
                'speakerGroupId': None,
                'source': {'file': path.name, 'turnId': turn_id},
                'objective': turn.get('objectiveActiveDuringTurn'),
                'knownFacts': known_facts,
                'unresolvedPromiseTypes': evidence.get('unresolvedPromiseTypesBefore', []),
                'challengeShown': evidence.get('challengeShownBefore', False),
                'split': 'development',
                'transcriptStt': transcript,
                'transcriptCorrected': None,
                'priorDialogue': [
                    {'player': earlier.get('transcript'), 'lan': earlier.get('customerText')}
                    for earlier in session.get('turns', [])[max(0, index - 3):index]
                ],
                'referenceLabels': None,
                'referenceEnding': None,
                'review': {'independent': False, 'reviewerId': None, 'reviewedAtUtc': None},
            })
    return records


def wilson(successes: int, count: int) -> list[float] | None:
    if not count:
        return None
    z = 1.959963984540054
    p = successes / count
    center = (p + z * z / (2 * count)) / (1 + z * z / count)
    margin = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / (1 + z * z / count)
    return [max(0., center - margin), min(1., center + margin)]


def quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def accepted_status(label: str, value: Any) -> str:
    """Evaluate Noul, preserving missing values as uncertain, never false."""
    if not isinstance(value, dict):
        return 'uncertain'
    p = value.get('noul')
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 1:
        return 'uncertain'
    low, high = (.1, .9) if label in PENALTY_LABELS else (.2, .8)
    return 'true' if p >= high else 'false' if p <= low else 'uncertain'


async def predict(records: list[dict], *, transcript_field='transcriptStt', classifier=None) -> list[dict]:
    """Run isolated classification without passing human references to Jev."""
    service = classifier or JevSalesClassifier()
    ids = set()
    for row in records:
        case_id = row.get('caseId')
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError('Prediction case IDs must be unique nonempty strings')
        ids.add(case_id)
        if not isinstance(row.get(transcript_field), str) or not row[transcript_field].strip():
            raise ValueError('Every prediction record requires the selected transcript field')
    predictions = []
    for row in records:
        context = {'objective': row.get('objective'), 'knownFacts': row.get('knownFacts', []),
                   'priorDialogue': row.get('priorDialogue', []),
                   'challengeShown': row.get('challengeShown', False),
                   'unresolvedPromiseTypes': row.get('unresolvedPromiseTypes', [])}
        started = time.monotonic()
        prediction = {'caseId': row['caseId'], 'questionSetVersion': QUESTION_SET_VERSION,
                      'thresholdVersion': THRESHOLD_VERSION, 'transcriptField': transcript_field}
        try:
            classified = await service.classify(row[transcript_field], context)
            prediction.update(labels=classified.get('labels', {}), metadata=classified.get('metadata', {}))
        except Exception:
            # Do not include provider exceptions, which may contain private input.
            prediction.update(labels={}, errorCode='classifier_failed')
        prediction['jevSeconds'] = time.monotonic() - started
        predictions.append(prediction)
    return predictions


def evaluate(references: list[dict], predictions: list[dict], split: str = 'holdout') -> dict:
    ids: set[str] = set()
    session_splits: dict[str, str] = {}
    speaker_splits: dict[str, str] = {}
    for reference in references:
        case_id = reference.get('caseId')
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError('Reference case IDs must be unique nonempty strings')
        ids.add(case_id)
        current_split = reference.get('split')
        if current_split not in {'development', 'calibration', 'holdout'}:
            raise ValueError('Reference split must be development, calibration or holdout')
        for field, assigned in [('sourceSessionId', session_splits), ('speakerGroupId', speaker_splits)]:
            group = reference.get(field)
            if not isinstance(group, str) or not group.strip():
                if current_split != 'development':
                    raise ValueError('Reviewed calibration/holdout requires verified session and speaker groups')
                continue
            if group in assigned and assigned[group] != current_split:
                raise ValueError(f'{field} leaks across dataset splits')
            assigned[group] = current_split
    by_case: dict[str, dict] = {}
    for prediction in predictions:
        case_id = prediction.get('caseId')
        if not isinstance(case_id, str) or case_id in by_case:
            raise ValueError('Prediction case IDs must be unique strings')
        by_case[case_id] = prediction
    chosen = [row for row in references if row['split'] == split]
    for row in chosen:
        review = row.get('review', {})
        if not isinstance(review, dict) or review.get('independent') is not True or not review.get('reviewerId') or not review.get('reviewedAtUtc'):
            raise ValueError('Selected references require independent human review provenance')
        labels = row.get('referenceLabels')
        if not isinstance(labels, dict) or not labels or any(type(value) is not bool for value in labels.values()):
            raise ValueError('Reviewed referenceLabels must contain explicit boolean labels')
    version_rows = [
        (row.get('questionSetVersion'), row.get('thresholdVersion'))
        for row in predictions if row.get('caseId') in {ref['caseId'] for ref in chosen}
    ]
    if any(not all(isinstance(item, str) and item for item in version) for version in version_rows):
        raise ValueError('Predictions require one frozen question set and threshold version per evaluation')
    versions = set(version_rows)
    if len(versions) > 1:
        raise ValueError('Predictions require one frozen question set and threshold version per evaluation')
    if any(version[1] != THRESHOLD_VERSION for version in versions):
        raise ValueError('Unknown threshold version; use the corresponding versioned evaluator')
    counts: dict[str, dict[str, int]] = {}
    exact = decided_rows = false_penalties = mistaken_endings = known_endings = 0
    for row in chosen:
        prediction = by_case.get(row['caseId'], {})
        predicted_labels = prediction.get('labels', {})
        all_decided = exact_row = True
        for label, expected in row['referenceLabels'].items():
            count = counts.setdefault(label, dict(tp=0, tn=0, fp=0, fn=0, uncertain=0, positives=0, negatives=0))
            count['positives' if expected else 'negatives'] += 1
            status = accepted_status(label, predicted_labels.get(label) if isinstance(predicted_labels, dict) else None)
            if status == 'uncertain':
                count['uncertain'] += 1
                all_decided = exact_row = False
                continue
            predicted = status == 'true'
            count['tp' if expected and predicted else 'tn' if not expected and not predicted else 'fp' if predicted else 'fn'] += 1
            exact_row &= predicted == expected
            if label in PENALTY_LABELS and predicted and not expected:
                false_penalties += 1
        exact += int(exact_row)
        decided_rows += int(all_decided)
        if row.get('referenceEnding') is not None and prediction.get('endingReason') is not None:
            known_endings += 1
            mistaken_endings += int(row['referenceEnding'] != prediction['endingReason'])
    per_label = {}
    for label, count in counts.items():
        tp, fp, fn = count['tp'], count['fp'], count['fn']
        total = count['positives'] + count['negatives']
        decided = total - count['uncertain']
        per_label[label] = {
            **count,
            'decided': decided,
            'decisionRate': decided / total if total else None,
            'precision': tp / (tp + fp) if tp + fp else None,
            'precisionWilson95': wilson(tp, tp + fp),
            'recallOnDecided': tp / (tp + fn) if tp + fn else None,
            'recallIncludingAbstention': tp / count['positives'] if count['positives'] else None,
            'f1OnDecided': 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        }
    penalty_checks = {label: {
        'pointPrecisionAtLeast98Percent': result['precision'] is not None and result['precision'] >= .98,
        'atLeast30PositiveAnd30NegativeReferences': result['positives'] >= 30 and result['negatives'] >= 30,
        'precisionWilson95': result['precisionWilson95'],
    } for label, result in per_label.items() if label in PENALTY_LABELS}
    return {
        'split': split,
        'reviewedTurns': len(chosen),
        'questionThresholdVersions': [list(version) for version in sorted(versions)],
        'missingPredictionTurns': sum(row['caseId'] not in by_case for row in chosen),
        'exactMatchTurns': exact,
        'exactMatchRate': exact / len(chosen) if chosen else None,
        'fullyDecidedTurns': decided_rows,
        'falsePenaltyLabelDecisions': false_penalties,
        'comparedEndings': known_endings,
        'mistakenEndings': mistaken_endings,
        'perLabel': per_label,
        'penaltyChecks': penalty_checks,
        'rolloutApproved': False,
        'note': 'This report cannot approve rollout. Review coverage, uncertainty, human writer review, live latency and headset checks separately.',
    }


def observations_report(records: list[dict]) -> dict:
    def finite_nonnegative(value):
        if type(value) not in (int, float) or value < 0:
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    timings = {}
    for stage in ('sttSeconds', 'jevSeconds', 'writerSeconds', 'wavSeconds', 'endOfSpeechToPlaybackSeconds'):
        values = [row[stage] for row in records if finite_nonnegative(row.get(stage))]
        timings[stage] = {'samples': len(values), 'p50': quantile(values, .5), 'p95': quantile(values, .95)}
    costs = [row['usageCostUsd'] for row in records if finite_nonnegative(row.get('usageCostUsd'))]
    writer_reviews = [row['writerReview'] for row in records if isinstance(row.get('writerReview'), dict) and row['writerReview'].get('independent') is True and row['writerReview'].get('reviewerId') and type(row['writerReview'].get('passed')) is bool]
    return {
        'timings': timings,
        'reportedUsageCostUsd': sum(costs) if costs else None,
        'costSamples': len(costs),
        'missingCostSamples': len(records) - len(costs),
        'independentlyReviewedWriterReplies': len(writer_reviews),
        'writerPassRate': sum(review['passed'] for review in writer_reviews) / len(writer_reviews) if writer_reviews else None,
        'fallbackCount': sum(row.get('fallbackUsed') is True for row in records),
        'retryCount': sum(row.get('retried') is True for row in records),
        'errorCount': sum(bool(row.get('errorCode')) for row in records),
        'headsetVerified': False,
    }


def calibration_curves(references: list[dict], predictions: list[dict]) -> dict:
    """Inspect per-label thresholds on calibration only, never on holdout."""
    # Enforce independent review and split isolation using the same validator.
    evaluate(references, predictions, 'calibration')
    selected = [row for row in references if row['split'] == 'calibration']
    by_case = {row['caseId']: row for row in predictions}
    labels = sorted({label for row in selected for label in row['referenceLabels']})
    curves = {}
    for label in labels:
        points = []
        for upper in (.7, .75, .8, .85, .9, .95, .98, .99):
            tp = fp = fn = unknown = positives = negatives = 0
            for reference in selected:
                if label not in reference['referenceLabels']:
                    continue
                expected = reference['referenceLabels'][label]
                positives += int(expected)
                negatives += int(not expected)
                prediction = by_case.get(reference['caseId'], {})
                data = prediction.get('labels', {})
                answer = data.get(label, {}) if isinstance(data, dict) else {}
                p = answer.get('noul') if isinstance(answer, dict) else None
                if type(p) not in (int, float) or not 0 <= p <= 1:
                    unknown += 1
                    continue
                predicted = p >= upper
                tp += int(predicted and expected)
                fp += int(predicted and not expected)
                fn += int(not predicted and expected)
            points.append({'acceptTrueAt': upper, 'tp': tp, 'fp': fp, 'fn': fn,
                           'missingOrInvalid': unknown, 'positiveReferences': positives,
                           'negativeReferences': negatives,
                           'precision': tp / (tp + fp) if tp + fp else None,
                           'precisionWilson95': wilson(tp, tp + fp),
                           'recall': tp / positives if positives else None})
        curves[label] = points
    return {'split': 'calibration', 'reviewedTurns': len(selected), 'acceptanceCurves': curves,
            'thresholdsAppliedToProduction': False,
            'note': 'Select both true and false thresholds after review, bump the threshold version, then evaluate untouched holdout. These curves do not approve rollout.'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_command = commands.add_parser('prepare')
    prepare_command.add_argument('--recordings', type=Path, required=True)
    prepare_command.add_argument('--output', type=Path, required=True)
    predict_command = commands.add_parser('predict', help='Call OpenRouter Jev using backend environment credentials')
    predict_command.add_argument('--records', type=Path, required=True)
    predict_command.add_argument('--transcript-field', choices=['transcriptStt', 'transcriptCorrected'], default='transcriptStt')
    predict_command.add_argument('--limit', type=int, required=True, help='Maximum number of paid classification requests')
    predict_command.add_argument('--output', type=Path, required=True)
    score_command = commands.add_parser('score')
    score_command.add_argument('--references', type=Path, required=True)
    score_command.add_argument('--predictions', type=Path, required=True)
    score_command.add_argument('--split', choices=['calibration', 'holdout'], default='holdout')
    score_command.add_argument('--observations', type=Path)
    score_command.add_argument('--output', type=Path, required=True)
    calibration_command = commands.add_parser('calibrate')
    calibration_command.add_argument('--references', type=Path, required=True)
    calibration_command.add_argument('--predictions', type=Path, required=True)
    calibration_command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; choose a new output to preserve reviewed data')
    try:
        if args.command == 'prepare':
            result = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in prepare(args.recordings))
        elif args.command == 'predict':
            if not 1 <= args.limit <= 5000:
                raise ValueError('Prediction limit must be between 1 and 5000')
            rows = asyncio.run(predict(read_records(args.records)[:args.limit], transcript_field=args.transcript_field))
            result = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
        elif args.command == 'calibrate':
            result = json.dumps(calibration_curves(read_records(args.references), read_records(args.predictions)), ensure_ascii=False, indent=2) + '\n'
        else:
            report = evaluate(read_records(args.references), read_records(args.predictions), args.split)
            if args.observations:
                report['observations'] = observations_report(read_records(args.observations))
            result = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding='utf-8')
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
