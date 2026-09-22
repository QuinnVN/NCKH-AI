"""Validated contract and prompt builders for the post-VR final evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "final_evaluation_system.txt"
DIMENSION_IDS = tuple(
    [f"D{index}" for index in range(1, 7)]
    + [f"E{index}" for index in range(1, 7)]
    + [f"S{index}" for index in range(1, 4)]
    + [f"M{index}" for index in range(1, 4)]
    + [f"A{index}" for index in range(1, 5)]
    + [f"P{index}" for index in range(1, 7)]
)
STAGE_IDS = ("D", "E", "S", "M", "A", "P")
DimensionLevel = Literal[
    "not-compatible",
    "low-compatible",
    "neutral",
    "fairly-compatible",
    "well-compatible",
]
FindingKind = Literal["confirmed", "emerging", "development"]
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("participantName must be text")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(normalized) <= 100:
        raise ValueError("participantName must contain 1 through 100 characters")
    return normalized


def normalize_email(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("participantEmail must be text")
    normalized = value.strip().lower()
    if len(normalized) > 254 or EMAIL_PATTERN.fullmatch(normalized) is None:
        raise ValueError("participantEmail must be a valid email address")
    return normalized


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class BehaviourFinding(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    kind: FindingKind
    title: str = Field(min_length=1, max_length=120)
    questionnaire_result: str = Field(alias="questionnaireResult", min_length=1, max_length=600)
    vr_evidence: str = Field(alias="vrEvidence", min_length=1, max_length=600)
    summary: str = Field(min_length=1, max_length=600)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if SLUG_PATTERN.fullmatch(value) is None:
            raise ValueError("finding id must be a lowercase ASCII slug")
        return value


class BehaviourComparison(StrictModel):
    experience_name: str = Field(alias="experienceName", min_length=1, max_length=120)
    findings: list[BehaviourFinding] = Field(max_length=12)

    @model_validator(mode="after")
    def unique_ids(self) -> "BehaviourComparison":
        ids = [item.id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding ids must be unique")
        return self


class CareerSuggestion(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    compatibility_percent: int = Field(alias="compatibilityPercent", ge=0, le=100)
    description: str = Field(min_length=1, max_length=900)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if SLUG_PATTERN.fullmatch(value) is None:
            raise ValueError("career id must be a lowercase ASCII slug")
        return value


class FinalEvaluationSummary(StrictModel):
    experience_name: str = Field(alias="experienceName", min_length=1, max_length=120)
    headline: str = Field(min_length=1, max_length=300)
    work_style: str = Field(alias="workStyle", min_length=1, max_length=700)
    benefit: str = Field(min_length=1, max_length=700)
    challenge: str = Field(min_length=1, max_length=700)
    improvement: str = Field(min_length=1, max_length=700)
    strength_label: str = Field(alias="strengthLabel", min_length=1, max_length=120)
    development_label: str = Field(alias="developmentLabel", min_length=1, max_length=120)
    evidence: str = Field(min_length=1, max_length=700)


class FinalAssessment(StrictModel):
    version: Literal[1]
    participant_name: str = Field(alias="participantName", min_length=1, max_length=100)
    participant_email: str = Field(alias="participantEmail", min_length=3, max_length=254)
    completed_at: str = Field(alias="completedAt")
    stage_assessments: dict[str, str] = Field(alias="stageAssessments")
    dimension_levels: dict[str, DimensionLevel] = Field(alias="dimensionLevels")
    behaviour_comparison: BehaviourComparison = Field(alias="behaviourComparison")
    career_suggestions: list[CareerSuggestion] = Field(alias="careerSuggestions", min_length=1, max_length=3)
    final_evaluation: FinalEvaluationSummary = Field(alias="finalEvaluation")

    @field_validator("participant_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_name(value)

    @field_validator("participant_email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("completedAt must use ISO 8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("completedAt must include a timezone")
        return value

    @field_validator("stage_assessments")
    @classmethod
    def validate_stages(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(STAGE_IDS):
            raise ValueError("stageAssessments must contain exactly D, E, S, M, A and P")
        if any(not isinstance(text, str) or not text.strip() or len(text) > 1200 for text in value.values()):
            raise ValueError("every stage assessment must be non-empty and at most 1200 characters")
        return value

    @field_validator("dimension_levels")
    @classmethod
    def validate_dimensions(cls, value: dict[str, DimensionLevel]) -> dict[str, DimensionLevel]:
        if set(value) != set(DIMENSION_IDS):
            raise ValueError("dimensionLevels must contain exactly the 28 DESMAP dimension ids")
        return value

    @model_validator(mode="after")
    def validate_careers(self) -> "FinalAssessment":
        ids = [item.id for item in self.career_suggestions]
        if len(ids) != len(set(ids)):
            raise ValueError("career suggestion ids must be unique")
        percentages = [item.compatibility_percent for item in self.career_suggestions]
        if percentages != sorted(percentages, reverse=True):
            raise ValueError("career suggestions must be ordered by compatibilityPercent")
        return self


class FinalEvaluationOutputError(ValueError):
    """The model output failed the final-assessment contract."""


def load_system_prompt() -> str:
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError("the final evaluation system prompt is empty")
    return prompt


def build_text_field_messages(
    *, field: str, instruction: str, facts: Mapping[str, Any], max_characters: int
) -> list[dict[str, str]]:
    """Ask the model for one prose value, never a document or a number."""
    payload = {
        "field": field,
        "instruction": instruction,
        "facts": facts,
        "maxCharacters": max_characters,
    }
    return [
        {"role": "system", "content": load_system_prompt()},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
            + "\n/no_think",
        },
    ]


def parse_text_field(content: str, *, max_characters: int) -> str:
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
    fenced = re.fullmatch(r"```(?:text)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if not text or text == "INSUFFICIENT_EVIDENCE":
        raise FinalEvaluationOutputError("The model did not have enough evidence for a required text field.")
    if text.startswith(('{', '[')) or len(text) > max_characters:
        raise FinalEvaluationOutputError("The model returned an invalid single-field value.")
    return text
