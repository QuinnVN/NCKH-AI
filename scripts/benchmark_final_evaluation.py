"""Read-only MongoDB final-evaluation benchmark using the production generator.

Only deidentified rubric inputs and responses are exported under recordings.
Model overrides apply to this experiment's transport, never production settings.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys
import time
import unicodedata

import httpx
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import get_settings
from app.career_ranking import CAREER_RANKING_VERSION, CATALOG
from app.final_evaluation import PROMPT_PATH, parse_text_field, FinalEvaluationOutputError
from app.final_evaluation_calculation import extract_dimensions, combine_game_results
from app.final_evaluation_openrouter import FinalEvaluationOpenRouter
from scripts.generate_final_evaluation import load_participant_data, generate_assessment, _write_field

MODELS = ['deepseek/deepseek-v4.1-flash', 'qwen/qwen3.8-flash', 'xiaomi/mimo-v2.6-flash']
FIXED_COMPLETED_AT = '2026-10-04T00:00:00Z'
NEGATIVE_TERMS = ('yếu kém', 'kém cỏi', 'không có năng lực', 'không phù hợp',
                  'bảo đảm thành công', 'đảm bảo thành công')
INTERNAL = re.compile(r'\b(?:scoreDelta|gameId|runCount|criterionScores|weightedContribution|backend|'
                      r'kindCalculatedByBackend|doctor|clinic|D[1-6]|E[1-6]|S[1-3]|M[1-3]|A[1-4]|P[1-6])\b')


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(value) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()


def normalized(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFC', text).casefold().split())


def criterion_phrases(observation: dict) -> list[str]:
    phrases = [normalized(observation['label'])]
    for piece in observation['evidence'].split(';'):
        criterion = piece.split(':', 1)[-1]
        criterion = re.sub(r'\(\d+(?:\.\d+)?/100\)', '', criterion).strip()
        criterion = re.sub(r'^(?:điểm|kết quả)\s+', '', criterion, flags=re.I)
        if criterion:
            phrases.append(normalized(criterion))
    return phrases


def minimal_game(game: dict) -> dict:
    game_id = game.get('gameId', '').lower()
    data = game.get('data', game)
    if game_id == 'lawyer':
        lawyer = data.get('lawyer', data)
        result = {'lawyer': {'criterionScores': lawyer.get('criterionScores', {})}}
    elif game_id == 'sale':
        first, second = data.get('part1', {}), data.get('part2', {})
        result = {
            'part1': {k: first[k] for k in ('score', 'selectedShoeId', 'bestFitShoeId') if k in first},
            'part2': {k: second[k] for k in ('score', 'causeIdentification', 'solutionSuitability',
                       'criterionScores', 'assessmentStatus') if k in second},
        }
    elif game_id == 'doctor':
        result = {'cases': [{k: row[k] for k in ('categorizationAccuracyPercent',
                   'essentialCategorizationAccuracyPercent') if k in row}
                  for row in data.get('cases', []) if isinstance(row, dict)]}
    elif game_id == 'clinic':
        result = {'patientResults': [{'scoreDelta': row['scoreDelta']} for row in data.get('patientResults', [])
                                    if isinstance(row, dict) and 'scoreDelta' in row]}
    else:
        result = {}
    return {'gameId': game_id, 'status': 'completed', 'data': result}


def prepare(destination: Path) -> None:
    settings = get_settings()
    if not settings.mongodb_uri:
        raise RuntimeError('MONGODB_URI is required')
    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5000)
    cases, excluded = [], Counter()
    try:
        db = client[settings.mongodb_database]
        stored = list(db['final_evaluations'].find({}, {'participantName': 1}))
        seen = set()
        for record in stored:
            name = record.get('participantName')
            if not isinstance(name, str) or name.casefold() in seen:
                excluded['missing_or_duplicate_identity'] += 1
                continue
            seen.add(name.casefold())
            try:
                q, _, games, _ = load_participant_data(db['questionnaire_submissions'], db['game_results'], name)
                dims = extract_dimensions(q)
                before = combine_game_results(games, dims)
                # Whitelist only fields actually read by the production calculator.
                clean_q = {'careerInterests': q.get('careerInterests', []), 'dimensions': [
                    {'id': d.code, 'score': d.score, 'name': d.name, 'description': d.description}
                    for d in dims.values()]}
                clean_games = [minimal_game(game) for game in games]
                after = combine_game_results(clean_games, extract_dimensions(clean_q))
                if asdict(before) != asdict(after):
                    raise RuntimeError('deidentification_changed_calculation')
                kinds = dict(Counter(f.kind for f in before.findings))
                cases.append({'questionnaire': clean_q, 'games': clean_games,
                              'gamesObserved': sorted({g['gameId'] for g in games}),
                              'findingKinds': kinds, 'findingCount': len(before.findings),
                              'fingerprint': digest({'q': clean_q, 'g': clean_games})})
            except Exception as error:
                # Never export source identity or raw database exceptions.
                excluded[type(error).__name__] += 1
    finally:
        client.close()
    cases.sort(key=lambda c: c['fingerprint'])
    for i, case in enumerate(cases, 1):
        case['caseId'] = f'profile-{i:02d}'
    catalog = httpx.get('https://openrouter.ai/api/v1/models', timeout=30).json()['data']
    models = []
    for model_id in MODELS:
        item = next((m for m in catalog if m['id'] == model_id), None)
        if item is None:
            raise RuntimeError(f'Requested model unavailable: {model_id}')
        models.append({k: item.get(k) for k in ('id', 'name', 'pricing', 'supported_parameters')})
    save(destination / 'cases.json', cases)
    save(destination / 'manifest.json', {
        'preparedAtUtc': datetime.now(timezone.utc).isoformat(), 'sourceFinalDocuments': len(stored),
        'eligibleProfiles': len(cases), 'excludedReasons': dict(excluded),
        'datasetSha256': digest(cases), 'models': models,
        'systemPromptSha256': hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest(),
        'generatorSha256': hashlib.sha256((ROOT / 'scripts/generate_final_evaluation.py').read_bytes()).hexdigest(),
        'rankingVersion': CAREER_RANKING_VERSION, 'maxTokens': 1024, 'maxPromptChars': 16000,
        'timeoutSeconds': 45, 'reasoningEnabled': False, 'providerDataCollection': 'deny',
        'method': 'Full production generator, same field prompts and repair/fallback logic; one sequential field at a time per eval.',
        'identityExported': False, 'databaseWrites': False,
    })
    print(json.dumps({'preparedProfiles': len(cases), 'excluded': dict(excluded),
                      'findingCounts': dict(Counter(c['findingCount'] for c in cases)),
                      'gameSets': dict(Counter(','.join(c['gamesObserved']) for c in cases))}), flush=True)


def field_checks(payload: dict, text: str) -> dict:
    field, facts = payload['field'], payload['facts']
    icon = field.endswith('.icon')
    checks = {}
    if icon:
        checks['format'] = text.strip() in {'analysis', 'adaptability', 'priority', 'communication',
                                          'collaboration', 'creativity', 'resilience', 'leadership', 'INSUFFICIENT_EVIDENCE'}
        return checks
    try:
        parse_text_field(text, max_characters=payload['maxCharacters'])
        checks['format'] = True
    except FinalEvaluationOutputError:
        checks['format'] = False
    checks['strictPlainText'] = not bool(re.search(r'```|^\s*[-*#]|\*\*|^\s*[\[{]', text)) and not (
        len(text.strip()) > 1 and text.strip()[0] == text.strip()[-1] and text.strip()[0] in (chr(34), chr(39)))
    checks['noInternalTerms'] = INTERNAL.search(text) is None
    checks['constructiveTone'] = not any(t in normalized(text) for t in NEGATIVE_TERMS)
    checks['contractLength'] = len(text.strip()) <= (300 if field == 'finalEvaluation.headline' else payload['maxCharacters'])
    if field.startswith('careerSuggestions.'):
        supported = [d for d in facts['questionnaireEvidence']
                     if d['name'].casefold() in text.casefold() and f"{d['score']:g}/100" in text]
        observed = facts['vrEvidence']
        checks['twoQuestionnaireScores'] = len(supported) >= 2
        checks['vrEvidenceExactProduction'] = not observed or any(
            d['evidence'] in text and f"{d['score']}/100" in text for d in observed)
        checks['vrEvidenceCaseInsensitive'] = not observed or any(
            normalized(d['evidence']) in normalized(text) and f"{d['score']}/100" in text for d in observed)
        checks['separatesSources'] = 'tự đánh giá' in normalized(text) and bool(
            re.search(r'\bVR\b|mô phỏng|trải nghiệm', text, re.I))
        # This is a label/score check, not a claim that semantic entailment was proven.
        checks['vrCriterionLabelOrPhraseAndScore'] = not observed or any(
            any(phrase in normalized(text) for phrase in criterion_phrases(d))
            and f"{d['score']}/100" in text for d in observed)
        scores = re.findall(r'(\d+(?:\.\d+)?)\s*/\s*100', text)
        allowed = {float(x['score']) for x in facts['questionnaireEvidence'] + observed}
        allowed.update(float(s) for d in observed for s in re.findall(r'(\d+(?:\.\d+)?)\s*/\s*100', d['evidence']))
        allowed.add(float(facts.get('compatibilityPercentCalculatedByBackend', -1)))
        # Can detect unsupported numeric values, not swapped labels with coincident scores.
        checks['onlyProvidedScores'] = all(float(s) in allowed for s in scores)
    elif '.findings.' in field:
        checks['noNumericScores'] = not bool(re.search(
            r'\d+(?:[.,]\d+)?\s*(?:/\s*100|điểm|%)|(?:điểm|mức|đạt)\s+(?:là\s+)?\d+', text, re.I))
        if field.endswith('.questionnaireResult'):
            checks['noVrInQuestionnaire'] = not bool(re.search(r'\bVR\b|mô phỏng|trải nghiệm', text, re.I))
        elif field.endswith('.remedy'):
            checks['outsideVrRemedy'] = not bool(re.search(r'\bVR\b|chơi lại|mô phỏng|trải nghiệm Bác sĩ|trải nghiệm Luật sư', text, re.I))
    return checks


async def benchmark_one(case: dict, model: str, repeat: int, destination: Path,
                        network: httpx.AsyncClient, api_key: str) -> dict:
    folder = destination / 'runs' / model.replace('/', '__') / f"{case['caseId']}-r{repeat}"
    if (folder / 'result.json').exists():
        return read(folder / 'result.json')
    folder.mkdir(parents=True, exist_ok=True)
    attempts = []

    async def forward(request: httpx.Request) -> httpx.Response:
        outgoing = json.loads(request.content)
        outgoing['model'] = model
        user = outgoing['messages'][1]['content'].split('\n/no_think')[0]
        # Repair requests append a reminder after the JSON. Decode just the object.
        payload, _ = json.JSONDecoder().raw_decode(user)
        row = {'field': payload['field'], 'payload': payload, 'requestModel': model,
               'temperature': outgoing['temperature'], 'maxTokens': outgoing['max_tokens'],
               'startedAtUtc': datetime.now(timezone.utc).isoformat()}
        started = time.perf_counter()
        try:
            headers = {k: v for k, v in request.headers.items() if k.lower() != 'content-length'}
            response = await network.post(str(request.url), headers=headers, json=outgoing)
            row['httpStatus'] = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.is_success:
                choice = (body.get('choices') or [{}])[0]
                text = (choice.get('message') or {}).get('content')
                row.update(generationId=body.get('id'), returnedModel=body.get('model'),
                           provider=body.get('provider'), usage=body.get('usage'),
                           finishReason=choice.get('finish_reason'), output=text)
                if isinstance(text, str):
                    row['checks'] = field_checks(payload, text)
            return response
        except httpx.HTTPError as error:
            row['networkError'] = type(error).__name__
            raise
        finally:
            row['seconds'] = round(time.perf_counter() - started, 5)
            attempts.append(row)
            save(folder / 'attempts.json', attempts)

    start = time.perf_counter()
    result = {'caseId': case['caseId'], 'model': model, 'repeat': repeat, 'success': False,
              'findingCount': case['findingCount'], 'findingKinds': case['findingKinds'],
              'gamesObserved': case['gamesObserved'], 'startedAtUtc': datetime.now(timezone.utc).isoformat()}
    async with httpx.AsyncClient(transport=httpx.MockTransport(forward)) as proxy:
        service = FinalEvaluationOpenRouter(api_key, client=proxy)
        try:
            assessment = await generate_assessment(
                service, participant_name='Hồ sơ kiểm thử', participant_email='benchmark@example.invalid',
                questionnaire=case['questionnaire'], game_results=case['games'], completed_at=FIXED_COMPLETED_AT,
                max_prompt_chars=16000, max_tokens=1024)
            output = assessment.model_dump(mode='json', by_alias=True, exclude_none=True)
            save(folder / 'assessment.json', output)
            latest = {a['field']: a for a in attempts if isinstance(a.get('output'), str)}
            career_fields = [a for a in latest.values() if a['field'].startswith('careerSuggestions.')]
            fallback = 0
            for career in output['careerSuggestions']:
                raw = latest[f"careerSuggestions.{career['id']}.description"]['output']
                try:
                    raw = parse_text_field(raw, max_characters=1200)
                except FinalEvaluationOutputError:
                    pass
                fallback += int(career['description'] != raw)
            result.update(success=True, careerCount=len(career_fields), careerFallbacks=fallback)
        except Exception as error:
            result['errorType'] = type(error).__name__
            # Final-generator and writer messages contain no source names after deidentification.
            result['error'] = str(error)[:400]
    result['seconds'] = round(time.perf_counter() - start, 5)
    result['attemptCount'] = len(attempts)
    result['distinctFields'] = len({a['field'] for a in attempts})
    costs = [a['usage']['cost'] for a in attempts if isinstance(a.get('usage'), dict)
             and isinstance(a['usage'].get('cost'), (int, float))]
    result['costUsd'] = sum(costs)
    result['pricedResponses'] = len(costs)
    result['unpricedSuccessResponses'] = sum(a.get('httpStatus') == 200 and not isinstance(
        (a.get('usage') or {}).get('cost'), (int, float)) for a in attempts)
    save(folder / 'result.json', result)
    print(json.dumps({k: result.get(k) for k in ('caseId', 'model', 'repeat', 'success', 'seconds',
                                               'costUsd', 'attemptCount', 'careerFallbacks', 'errorType')}), flush=True)
    return result


async def run(destination: Path, limit: int | None, repeats: int, parallel: int) -> None:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError('OPENROUTER_API_KEY is required')
    manifest = read(destination / 'manifest.json')
    if manifest['systemPromptSha256'] != hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest():
        raise RuntimeError('Production prompt changed since prepare')
    cases = read(destination / 'cases.json')
    if limit:
        cases = cases[:limit]
    semaphore = asyncio.Semaphore(parallel)
    save(destination / 'run-config.json', {'profiles': len(cases), 'repeats': repeats,
         'concurrentEvals': parallel, 'evalCount': len(cases) * repeats * len(MODELS)})
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10),
                                 limits=httpx.Limits(max_connections=parallel + 3)) as network:
        async def job(case, model, repeat):
            async with semaphore:
                return await benchmark_one(case, model, repeat, destination, network, settings.openrouter_api_key)
        # Interleave model order over cases to limit temporal/provider bias.
        jobs = []
        for repeat in range(1, repeats + 1):
            for index, case in enumerate(cases):
                for model in MODELS[index % 3:] + MODELS[:index % 3]:
                    jobs.append(job(case, model, repeat))
        await asyncio.gather(*jobs)
    summarize(destination)


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(destination: Path) -> dict:
    groups = defaultdict(list)
    for file in destination.glob('runs/*/*/result.json'):
        groups[read(file)['model']].append((read(file), read(file.parent / 'attempts.json')))
    summary = {}
    for model, entries in groups.items():
        results = [r for r, _ in entries]
        success = [r for r in results if r['success']]
        first_counts, final_counts = defaultdict(list), defaultdict(list)
        first_all, last_all = [], []
        career_lengths = []
        career_over_limit = 0
        providers = Counter()
        tokens = Counter()
        network_errors = Counter()
        responses = 0
        fallback = sum(r.get('careerFallbacks', 0) for r in success)
        for result, attempts in entries:
            first, last = {}, {}
            for a in attempts:
                providers[a.get('provider', 'unreported')] += 1
                if a.get('networkError'):
                    network_errors[a['networkError']] += 1
                if a.get('httpStatus') and a['httpStatus'] >= 400:
                    network_errors[f"HTTP{a['httpStatus']}"] += 1
                for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                    tokens[name] += (a.get('usage') or {}).get(name, 0)
                tokens['cached_tokens'] += ((a.get('usage') or {}).get('prompt_tokens_details') or {}).get('cached_tokens', 0)
                tokens['reasoning_tokens'] += ((a.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
                if isinstance(a.get('output'), str):
                    # Recompute from original output so rubric updates cannot mix versions.
                    a['checks'] = field_checks(a['payload'], a['output'])
                    responses += 1
                    first.setdefault(a['field'], a)
                    last[a['field']] = a
            for collection, target in ((first, first_counts), (last, final_counts)):
                for a in collection.values():
                    if target is first_counts and a['field'].startswith('careerSuggestions.'):
                        career_lengths.append(len(a['output'].strip()))
                        career_over_limit += int(len(a['output'].strip()) > a['payload']['maxCharacters'])
                    for name, ok in a['checks'].items():
                        target[name].append(ok)
                    observable_checks = {k: v for k, v in a['checks'].items() if k not in {
                        'vrEvidenceExactProduction', 'vrEvidenceCaseInsensitive', 'contractLength'}}
                    if not a['field'].endswith('.icon'):
                        (first_all if target is first_counts else last_all).append(all(observable_checks.values()))
        def rates(counts):
            return {name: {'passed': sum(values), 'tested': len(values),
                           'percent': round(100 * sum(values) / len(values), 3)} for name, values in counts.items()}
        summary[model] = {
            'evalsAttempted': len(results), 'evalsCompleted': len(success),
            'completionPercent': round(100 * len(success) / len(results), 3),
            'totalCostUsd': sum(r['costUsd'] for r in results),
            'meanCostPerAttemptUsd': statistics.mean(r['costUsd'] for r in results),
            'meanCompletedCostUsd': statistics.mean(r['costUsd'] for r in success) if success else None,
            'costPerDeliveredEvalIncludingFailuresUsd': sum(r['costUsd'] for r in results) / len(success) if success else None,
            'meanCompletedSeconds': statistics.mean(r['seconds'] for r in success) if success else None,
            'medianCompletedSeconds': quantile([r['seconds'] for r in success], .5),
            'p95CompletedSeconds': quantile([r['seconds'] for r in success], .95),
            'meanCallsPerCompletedEval': statistics.mean(r['attemptCount'] for r in success) if success else None,
            'extraRequests': sum(r['attemptCount'] - r['distinctFields'] for r in results),
            'careerFallbacks': fallback, 'careerDescriptionsDelivered': sum(r.get('careerCount', 0) for r in success),
            'careerFallbackPercent': 100 * fallback / (7 * len(success)) if success else None,
            'unpricedSuccessResponses': sum(r['unpricedSuccessResponses'] for r in results),
            'tokens': dict(tokens), 'providers': dict(providers), 'errors': dict(network_errors),
            'firstResponseChecks': rates(first_counts), 'lastResponseChecks': rates(final_counts),
            'firstAllObservableChecks': {'passed': sum(first_all), 'tested': len(first_all),
                                        'percent': 100 * sum(first_all) / len(first_all) if first_all else None},
            'lastAllObservableChecks': {'passed': sum(last_all), 'tested': len(last_all),
                                       'percent': 100 * sum(last_all) / len(last_all) if last_all else None},
            'firstCareerResponseLengths': {'medianCharacters': quantile(career_lengths, .5),
                                          'p95Characters': quantile(career_lengths, .95),
                                          'overLimit': career_over_limit, 'tested': len(career_lengths)},
        }
    complete_by_model = {model: {r['caseId']: r for r, _ in entries if r['success'] and r['repeat'] == 1}
                         for model, entries in groups.items()}
    common = set.intersection(*(set(x) for x in complete_by_model.values())) if len(groups) == len(MODELS) else set()
    paired = {'commonCompletedCaseIds': sorted(common), 'models': {}}
    for model, complete in complete_by_model.items():
        values = [complete[c] for c in sorted(common)]
        paired['models'][model] = {
            'profiles': len(values),
            'meanCostUsd': statistics.mean(r['costUsd'] for r in values) if values else None,
            'meanSeconds': statistics.mean(r['seconds'] for r in values) if values else None,
            'medianSeconds': quantile([r['seconds'] for r in values], .5),
        }
    save(destination / 'paired-summary.json', paired)
    save(destination / 'summary.json', summary)
    return summary


async def diagnose_names(destination: Path) -> None:
    """Paired diagnostic only: replace opaque dimension names with catalog labels.

    This does not change the full benchmark dataset, production or full-eval scores.
    Original first answers are reused, not billed a second time.
    """
    settings = get_settings()
    records = []
    semaphore = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10)) as network:
        async def probe(case_id: str, model: str, field: str):
            async with semaphore:
                source = destination / 'runs' / model.replace('/', '__') / f'{case_id}-r1' / 'attempts.json'
                before = next((r for r in read(source) if r['field'] == field and isinstance(r.get('output'), str)), None)
                if before is None:
                    return
                payload = json.loads(json.dumps(before['payload']))
                for item in payload['facts']['dimensions']:
                    item['name'] = CATALOG['dimensionNames'][item['code']]
                calls = []
                async def forward(request):
                    outgoing = json.loads(request.content)
                    outgoing['model'] = model
                    headers = {k: v for k, v in request.headers.items() if k.lower() != 'content-length'}
                    started = time.perf_counter()
                    response = await network.post(str(request.url), headers=headers, json=outgoing)
                    body = response.json() if response.is_success else {}
                    calls.append({'status': response.status_code, 'usage': body.get('usage'),
                                  'provider': body.get('provider'), 'seconds': time.perf_counter() - started,
                                  'output': ((body.get('choices') or [{}])[0].get('message') or {}).get('content')})
                    return response
                result = {'caseId': case_id, 'model': model, 'field': field,
                          'before': before['output'], 'factsAfter': payload['facts']}
                async with httpx.AsyncClient(transport=httpx.MockTransport(forward)) as proxy:
                    writer = FinalEvaluationOpenRouter(settings.openrouter_api_key, client=proxy)
                    try:
                        result['after'] = await _write_field(
                            writer, field=field, instruction=payload['instruction'], facts=payload['facts'],
                            max_characters=payload['maxCharacters'], max_prompt_chars=16000, max_tokens=1024)
                        result['success'] = True
                    except Exception as error:
                        result.update(success=False, errorType=type(error).__name__)
                result['calls'] = calls
                records.append(result)
        await asyncio.gather(*(probe(case_id, model, field)
                               for case_id in ('profile-02', 'profile-03', 'profile-04')
                               for model in MODELS for field in ('stageAssessments.D', 'stageAssessments.E')))
    save(destination / 'dimension-name-diagnostic.json', records)
    print(json.dumps({'diagnosticFields': len(records), 'completed': sum(r['success'] for r in records),
                      'extraCostUsd': sum((c.get('usage') or {}).get('cost', 0)
                                          for r in records for c in r['calls'])}), flush=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'run', 'summarize', 'diagnose-names'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--parallel', type=int, default=6)
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.output)
    elif args.command == 'run':
        asyncio.run(run(args.output, args.limit, args.repeats, args.parallel))
    elif args.command == 'diagnose-names':
        asyncio.run(diagnose_names(args.output))
    else:
        print(json.dumps(summarize(args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
