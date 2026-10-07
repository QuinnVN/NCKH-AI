"""Occupational weights shared with the web evaluation, without LLM scoring."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping

CATALOG = json.loads((Path(__file__).resolve().parent.parent / "data/career-catalog.json").read_text(encoding="utf-8"))
CAREER_RANKING_VERSION = CATALOG["version"]


@dataclass(frozen=True)
class RankedCareer:
    career: dict[str, Any]
    score: float
    compatibility_percent: int
    questionnaire_score: float
    vr_share: float
    dimensions: list[dict[str, Any]]
    observations: list[dict[str, Any]]


def _correlation(xs: list[float], ys: list[float]) -> float:
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    x_dev = [x - x_mean for x in xs]
    y_dev = [y - y_mean for y in ys]
    denominator = math.sqrt(sum(x * x for x in x_dev) * sum(y * y for y in y_dev))
    return sum(x * y for x, y in zip(x_dev, y_dev)) / denominator if denominator else 0.0


def questionnaire_fit(career: Mapping[str, Any], dimensions: Mapping[str, Any]) -> float:
    """Questionnaire score on 0-100, following the O*NET profile-linking approach.

    Non-Desire dimensions compare the shape of the self-reported profile with the career's O*NET
    profile through Pearson correlation. Desire dimensions compare levels with the career's work
    values. Low self-reports on core requirements (profile >= minProfile) reduce the score.
    """
    scores = {}
    for code in CATALOG["dimensionNames"]:
        score = dimensions[code].score
        if not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError(f"Invalid dimension score: {code}")
        scores[code] = score
    profile = career["profile"]
    targets = career["desireTargets"]
    desire = sum(100 - abs(scores[code] - target) for code, target in targets.items()) / len(targets)
    shape = 50 + 50 * _correlation([scores[code] for code in profile], list(profile.values()))
    penalty = CATALOG["shortfallPenalty"]
    core = [code for code, value in profile.items() if value >= penalty["minProfile"]]
    shortfall = sum(max(0, penalty["threshold"] - scores[code]) for code in core) / len(core) if core else 0
    share = CATALOG["desireShare"]
    return min(100.0, max(0.0, desire * share + shape * (1 - share) - penalty["factor"] * shortfall))


def rank_careers(dimensions: Mapping[str, Any], behaviours: Mapping[str, Any] | None = None,
                 interests: list[str] | None = None, limit: int = 7) -> list[RankedCareer]:
    selected = set(interests or []) - {"exploring"}
    restrict = bool(selected) and "exploring" not in (interests or [])
    result = []
    for career in CATALOG["careers"]:
        if restrict and career["interestGroup"] not in selected:
            continue
        questionnaire_score = questionnaire_fit(career, dimensions)
        vr_weights = career["vrWeights"]
        observed = [{"code": code, "label": item.label, "score": item.score, "evidence": item.evidence}
                    for code, item in (behaviours or {}).items()
                    if code in vr_weights and 0 <= item.score <= 100 and item.evidence.strip()]
        available = sum(vr_weights[item["code"]] for item in observed)
        vr_share = CATALOG["maxVrShare"] * available / sum(vr_weights.values())
        vr_score = sum(item["score"] * vr_weights[item["code"]] for item in observed) / available if available else 0
        score = questionnaire_score * (1 - vr_share) + vr_score * vr_share
        # The career's strongest O*NET requirements, at least three, explain the suggestion.
        ranked_codes = sorted(career["profile"], key=lambda code: (-career["profile"][code], -dimensions[code].score, code))
        facts = [{"id": code, "name": CATALOG["dimensionNames"][code], "score": dimensions[code].score,
                  "importance": career["weights"][code]}
                 for index, code in enumerate(ranked_codes) if index < 3 or career["weights"][code] >= 3]
        observed.sort(key=lambda item: (-vr_weights[item["code"]], -item["score"], item["code"]))
        result.append(RankedCareer(career, score, math.floor(score + .5), questionnaire_score, vr_share, facts, observed))
    return sorted(result, key=lambda item: (-item.score, item.career["id"]))[:limit]


def career_description(item: RankedCareer, primary: bool = True) -> str:
    career = item.career
    facts = " và ".join(f"{fact['name'].lower()} {fact['score']:g}/100" for fact in item.dimensions[:2])
    questionnaire = f"Trong bảng hỏi, bạn tự đánh giá {facts}."
    work = f"Công việc {career['name']} cần {career['description']}."
    parts = [questionnaire, work] if sum(map(ord, career["id"])) % 3 == 0 else [work, questionnaire]
    if not primary:
        parts = [f"{parts[0][:-1]}; {parts[1][0].lower()}{parts[1][1:]}"]
    if item.observations:
        observed = item.observations[0]
        parts.append(f"Trong nhiệm vụ đã trải nghiệm, kết quả VR ghi nhận {observed['evidence']}, ở mức {observed['score']}/100.")
    parts.append(f"Bạn có thể {career['activity']} để tìm hiểu cách mình đáp ứng công việc.")
    return " ".join(parts)
