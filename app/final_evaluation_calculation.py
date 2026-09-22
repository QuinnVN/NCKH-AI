"""Deterministic calculations used to assemble a final DESMAP evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import fmean
from typing import Any, Mapping

from app.final_evaluation import DIMENSION_IDS, DimensionLevel


LEVEL_VALUES = {"not-compatible": 10.0, "low-compatible": 30.0, "neutral": 50.0,
                "fairly-compatible": 70.0, "well-compatible": 90.0}
BEHAVIOUR_DIMENSIONS = {
    "information-processing": ("M1", "M2", "M3"),
    "problem-solving": ("E1", "E2", "E3", "E4", "E5", "E6", "M1", "M2", "M3"),
    "decision-making": ("M1", "M2", "M3"), "adaptability": ("A1", "A2", "A3", "A4"),
    "pressure-response": ("P1", "P2", "P3", "P4", "P5", "P6"),
    "social-interaction": ("S1", "S2", "S3"),
}
BEHAVIOUR_LABELS = {
    "information-processing": "Xử lý thông tin", "problem-solving": "Giải quyết vấn đề",
    "decision-making": "Ra quyết định", "adaptability": "Khả năng thích ứng",
    "pressure-response": "Phản ứng với áp lực", "social-interaction": "Tương tác xã hội",
}
EXPERIENCE_NAMES = {"doctor": "Bác sĩ", "clinic": "Bác sĩ cấp cứu", "lawyer": "Luật sư", "sale": "Nhân viên bán hàng"}
EXPERIENCE_WEIGHTS = {
    "lawyer": {"problem-solving": .35, "decision-making": .25, "adaptability": .20, "social-interaction": .15, "information-processing": .05},
    "doctor": {"pressure-response": .25, "decision-making": .25, "problem-solving": .20, "adaptability": .20, "information-processing": .10},
    "clinic": {"pressure-response": .25, "decision-making": .25, "problem-solving": .20, "adaptability": .20, "information-processing": .10},
    "sale": {"social-interaction": .25, "problem-solving": .20, "decision-making": .20, "adaptability": .20, "information-processing": .15},
}


@dataclass(frozen=True)
class DimensionFact:
    code: str
    score: float
    name: str
    description: str


@dataclass(frozen=True)
class BehaviourFact:
    code: str
    label: str
    score: int
    evidence: str
    self_score: int


@dataclass(frozen=True)
class FindingPlan:
    id: str
    kind: str
    title: str
    questionnaire_fact: str
    vr_fact: str
    score: int
    self_score: int


@dataclass(frozen=True)
class CareerPlan:
    id: str
    name: str
    compatibility_percent: int
    rationale: str


CAREER_CATALOG = (
    ("bac-si", "Bác sĩ", {"E": .25, "M": .25, "A": .20, "P": .20, "S": .10}),
    ("luat-su", "Luật sư", {"M": .30, "E": .25, "S": .20, "A": .15, "P": .10}),
    ("nhan-vien-ban-hang", "Nhân viên bán hàng", {"S": .30, "A": .25, "E": .15, "M": .15, "P": .15}),
    ("chuyen-vien-phan-tich-du-lieu", "Chuyên viên phân tích dữ liệu", {"M": .35, "E": .30, "P": .15, "A": .10, "D": .10}),
    ("ky-su-phan-mem", "Kỹ sư phần mềm", {"M": .30, "E": .30, "A": .15, "P": .15, "D": .10}),
    ("quan-ly-du-an", "Quản lý dự án", {"S": .25, "M": .20, "A": .20, "P": .20, "E": .15}),
    ("chuyen-vien-trai-nghiem-nguoi-dung", "Chuyên viên trải nghiệm người dùng", {"D": .25, "E": .20, "M": .20, "S": .20, "A": .15}),
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    number = float(value)
    return number if 0 <= number <= 100 else None


def extract_dimensions(document: Mapping[str, Any]) -> dict[str, DimensionFact]:
    """Read 28 scored dimensions from keyed or list-based questionnaire shapes."""
    found: dict[str, DimensionFact] = {}

    def add(code: str, value: Any, container: Mapping[str, Any] | None = None) -> None:
        normalized = code.upper()
        if normalized not in DIMENSION_IDS or normalized in found:
            return
        item = container or (value if isinstance(value, Mapping) else {})
        raw_score = item.get("score", item.get("percent", item.get("value", value))) if isinstance(item, Mapping) else value
        if _number(raw_score) is None and isinstance(item, Mapping):
            raw, maximum = _number(item.get("raw")), _number(item.get("max"))
            raw_score = raw / maximum * 100 if raw is not None and maximum not in (None, 0) else raw_score
        score = _number(raw_score)
        if score is None and isinstance(item, Mapping) and isinstance(item.get("level"), str):
            score = LEVEL_VALUES.get(item["level"])
        if score is None:
            return
        found[normalized] = DimensionFact(normalized, score,
            str(item.get("name") or item.get("title") or normalized).strip()[:120],
            str(item.get("description") or "").strip()[:500])

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            raw_id = next((value.get(name) for name in ("id", "code", "dimensionId", "dimension_id", "dimension") if value.get(name)), None)
            if isinstance(raw_id, str):
                add(raw_id, value, value)
            for key, item in value.items():
                if str(key).upper() in DIMENSION_IDS:
                    add(str(key), item)
                if isinstance(item, Mapping):
                    raw_id = next((item.get(name) for name in ("id", "code", "dimensionId", "dimension_id", "dimension") if item.get(name)), None)
                    if isinstance(raw_id, str):
                        add(raw_id, item, item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(document)
    return found


def dimension_level(score: float) -> DimensionLevel:
    if score <= 20: return "not-compatible"
    if score <= 40: return "low-compatible"
    if score <= 60: return "neutral"
    if score <= 80: return "fairly-compatible"
    return "well-compatible"


def group_scores(dimensions: Mapping[str, DimensionFact]) -> dict[str, int]:
    return {group: round(fmean(item.score for code, item in dimensions.items() if code.startswith(group))) for group in "DESMAP"}


def _percent(value: Any, maximum: float = 100.0) -> int | None:
    number = _number(value)
    return None if number is None or maximum <= 0 else max(0, min(100, round(number / maximum * 100)))


def _average(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return round(fmean(present)) if present else None


def calculate_behaviours(game: Mapping[str, Any], dimensions: Mapping[str, DimensionFact]) -> dict[str, BehaviourFact]:
    game_id = str(game.get("gameId", "")).casefold()
    data = game.get("data") if isinstance(game.get("data"), Mapping) else game
    scores: dict[str, tuple[int | None, str]] = {}
    if game_id == "lawyer":
        lawyer = data.get("lawyer") if isinstance(data.get("lawyer"), Mapping) else data
        criteria = lawyer.get("criterionScores", {}) if isinstance(lawyer, Mapping) else {}
        scores = {
            "information-processing": (_percent(criteria.get("evidenceUse"), 40), "điểm sử dụng bằng chứng trong phần biện hộ"),
            "problem-solving": (_percent(criteria.get("logicalConnections"), 35), "điểm liên kết lập luận trong phần biện hộ"),
            "decision-making": (_percent(criteria.get("conclusionFidelity"), 15), "điểm kết luận bám sát chứng cứ"),
            "social-interaction": (_percent(criteria.get("clarityAndPersuasiveness"), 10), "điểm trình bày rõ ràng và thuyết phục"),
        }
    elif game_id == "sale":
        part1 = data.get("part1") if isinstance(data.get("part1"), Mapping) else {}
        part2 = data.get("part2") if isinstance(data.get("part2"), Mapping) else {}
        criteria = part2.get("criterionScores", {}) if isinstance(part2.get("criterionScores"), Mapping) else {}
        choice = 100 if part1.get("selectedShoeId") and part1.get("selectedShoeId") == part1.get("bestFitShoeId") else _percent(part1.get("score"))
        scores = {
            "information-processing": (_average([choice, 100 if part2.get("causeIdentification") is True else 0 if part2.get("causeIdentification") is False else None]), "lựa chọn sản phẩm và nhận diện nguyên nhân trong hai phần Sales"),
            "decision-making": (_average([choice, 100 if part2.get("solutionSuitability") is True else 0 if part2.get("solutionSuitability") is False else None]), "lựa chọn sản phẩm và mức phù hợp của giải pháp cuối"),
            "problem-solving": (_average([_percent(part1.get("score")), _percent(part2.get("score"))]), "điểm xử lý nhiệm vụ chọn sản phẩm và khách quay lại"),
            "adaptability": (_percent(criteria.get("adaptabilityAndDeescalation"), 50), "điểm thích ứng và hạ nhiệt hội thoại"),
            "social-interaction": (_percent(criteria.get("apologyAndPolicyRemedy"), 50), "điểm ghi nhận vấn đề và đưa ra hướng xử lý đúng chính sách"),
        }
    elif game_id == "doctor":
        cases = data.get("cases") if isinstance(data.get("cases"), list) else []
        accuracy = [_percent(item.get("categorizationAccuracyPercent")) for item in cases if isinstance(item, Mapping)]
        essential = [_percent(item.get("essentialCategorizationAccuracyPercent")) for item in cases if isinstance(item, Mapping)]
        scores = {"information-processing": (_average(accuracy), "độ chính xác phân loại thông tin ở các ca bệnh"),
                  "decision-making": (_average(essential), "độ chính xác với các thông tin thiết yếu của ca bệnh"),
                  "problem-solving": (_average(accuracy + essential), "kết quả phân loại dữ kiện thường và thiết yếu")}
    elif game_id == "clinic":
        patients = data.get("patientResults") if isinstance(data.get("patientResults"), list) else []
        deltas = [item.get("scoreDelta") for item in patients if isinstance(item, Mapping) and isinstance(item.get("scoreDelta"), (int, float))]
        if deltas:
            positive = round(sum(1 for value in deltas if value > 0) / len(deltas) * 100)
            scores = {"decision-making": (positive, "tỷ lệ lượt xử lý bệnh nhân có scoreDelta dương"),
                      "problem-solving": (positive, "tỷ lệ lượt xử lý bệnh nhân có scoreDelta dương")}
    result = {}
    for code, (score, evidence) in scores.items():
        self_values = [dimensions[item].score for item in BEHAVIOUR_DIMENSIONS[code] if item in dimensions]
        if score is not None and self_values:
            result[code] = BehaviourFact(code, BEHAVIOUR_LABELS[code], score, evidence, round(fmean(self_values)))
    return result


def behavioural_alignment(game_id: str, behaviours: Mapping[str, BehaviourFact]) -> int | None:
    weights = EXPERIENCE_WEIGHTS.get(game_id, {})
    available = [(behaviours[key].score, weight) for key, weight in weights.items() if key in behaviours]
    if not available: return None
    total = sum(weight for _, weight in available)
    return round(sum(score * weight for score, weight in available) / total)


def build_findings(behaviours: Mapping[str, BehaviourFact]) -> list[FindingPlan]:
    plans = []
    for item in sorted(behaviours.values(), key=lambda value: abs(value.score - value.self_score), reverse=True):
        kind = "confirmed" if item.self_score >= 61 and item.score >= 70 else "emerging" if item.self_score < 61 and item.score >= 70 else "development" if item.self_score >= 61 and item.score < 50 else None
        if kind:
            plans.append(FindingPlan(item.code, kind, item.label,
                f"Điểm tự đánh giá liên quan: {item.self_score}/100.", f"{item.evidence}: {item.score}/100.", item.score, item.self_score))
        if len(plans) == 4: break
    return plans


def build_careers(groups: Mapping[str, int], game_id: str, alignment: int | None) -> list[CareerPlan]:
    ranked = []
    experience_ids = {"doctor": "bac-si", "clinic": "bac-si", "lawyer": "luat-su", "sale": "nhan-vien-ban-hang"}
    for career_id, name, weights in CAREER_CATALOG:
        profile = round(sum(groups[group] * weight for group, weight in weights.items()))
        if alignment is not None and experience_ids.get(game_id) == career_id:
            score = round(profile * .7 + alignment * .3)
            rationale = f"điểm hồ sơ {profile}/100 và điểm hành vi nghề đã trải nghiệm {alignment}/100"
        else:
            score, rationale = profile, f"điểm tổng hợp theo các nhóm DESMAP mà nghề cần là {profile}/100"
        ranked.append(CareerPlan(career_id, name, max(0, min(100, score)), rationale))
    return sorted(ranked, key=lambda item: (-item.compatibility_percent, item.name))[:3]
