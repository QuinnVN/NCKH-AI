"""Interactive MongoDB-to-OpenRouter final evaluation CLI."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable, Mapping, Protocol

from bson import json_util
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.final_evaluation import (  # noqa: E402
    DIMENSION_IDS,
    FINDING_ICON_IDS,
    FinalAssessment,
    FinalEvaluationOutputError,
    build_text_field_messages,
    normalize_email,
    normalize_name,
    parse_text_field,
    utc_now,
)
from app.final_evaluation_calculation import (  # noqa: E402
    combine_game_results,
    dimension_level,
    extract_dimensions,
    group_scores,
)
from app.final_evaluation_openrouter import (  # noqa: E402
    FINAL_EVALUATION_MODEL, FinalEvaluationOpenRouter, FinalEvaluationProviderError,
)
from app.career_ranking import rank_careers, career_description, CAREER_RANKING_VERSION  # noqa: E402


QUESTIONNAIRE_COLLECTION = "questionnaire_submissions"
GAME_RESULTS_COLLECTION = "game_results"
FINAL_EVALUATIONS_COLLECTION = "final_evaluations"
DEFAULT_MODEL = FINAL_EVALUATION_MODEL
DEFAULT_MAX_PROMPT_CHARS = 16_000
DEFAULT_MAX_TOKENS = 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 45
REMEDY_LEARNING_ACTIVITIES = {
    "information-processing": (
        "Đọc một bài báo ngắn, gạch các dữ kiện chính rồi tóm tắt nội dung trong một câu.",
        "Xem video về cách phân biệt dữ kiện và ý kiến, rồi tự phân loại các câu trong một bài viết.",
    ),
    "problem-solving": (
        "Chọn một vấn đề trong học tập, viết hai cách xử lý và so sánh chúng theo một tiêu chí rõ ràng.",
        "Tham gia một buổi giải tình huống theo nhóm và ghi lại vì sao nhóm chọn phương án cuối.",
    ),
    "decision-making": (
        "Đọc tài liệu về cách đặt tiêu chí ra quyết định, rồi áp dụng hai tiêu chí cho một lựa chọn hằng ngày.",
        "Ghi lựa chọn, lý do và kết quả của một quyết định nhỏ để tự xem lại sau một tuần.",
    ),
    "adaptability": (
        "Thử nhận một vai trò mới trong hoạt động nhóm và ghi lại điều đã thay đổi trong cách làm.",
        "Lập kế hoạch cho một việc hằng tuần, rồi tập điều chỉnh khi có yêu cầu mới mà vẫn giữ mục tiêu chính.",
    ),
    "pressure-response": (
        "Lập danh sách việc trong ngày, đánh dấu hạn hoàn thành và chọn việc quan trọng nhất để làm trước.",
        "Thử chia một bài tập lớn thành các phần ngắn có thời hạn và ghi lại phần nào thường bị chậm.",
    ),
    "social-interaction": (
        "Tham gia một nhóm thảo luận và tập tóm tắt ý người khác trước khi trình bày ý mình.",
        "Xem video về lắng nghe chủ động, rồi thử đặt một câu hỏi xác nhận trong cuộc trò chuyện.",
    ),
}


class FinalEvaluationCLIError(RuntimeError):
    pass


class Generator(Protocol):
    async def generate(self, messages: list[dict[str, str]], **kwargs: Any) -> str: ...


def _json_safe(document: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json_util.dumps(dict(document)))


def _sort_value(document: Mapping[str, Any]) -> str:
    for key in ("submittedAtUtc", "submittedAt", "completedAtUtc", "completedAt",
                "createdAtUtc", "createdAt", "updatedAtUtc", "updatedAt", "_id"):
        value = document.get(key)
        if isinstance(value, datetime):
            return value.isoformat()
        if value is not None:
            return str(value)
    return ""


def list_participant_names(collection: Collection) -> list[str]:
    raw_names = collection.distinct("participant.name", {"participant.name": {"$type": "string"}})
    raw_names += collection.distinct("participantName", {"participantName": {"$type": "string"}})
    names: dict[str, str] = {}
    for raw_name in raw_names:
        try:
            name = normalize_name(raw_name)
        except ValueError:
            continue
        names.setdefault(name.casefold(), name)
    return sorted(names.values(), key=str.casefold)


def questionnaire_dimension_ids(document: Mapping[str, Any]) -> set[str]:
    """Find DESMAP dimension ids across common questionnaire document shapes."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key).upper()
                if key_text in DIMENSION_IDS:
                    found.add(key_text)
                if str(key).casefold() in {
                    "id", "code", "dimension", "dimensionid", "dimension_id"
                } and isinstance(item, str) and item.upper() in DIMENSION_IDS:
                    found.add(item.upper())
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(document)
    return found


def choose_participant(names: list[str], *, input_fn=input, output_fn=print) -> str:
    if not names:
        raise FinalEvaluationCLIError("Không tìm thấy participantName trong questionnaire_submissions.")
    output_fn("\nDanh sách người tham gia:")
    for index, name in enumerate(names, start=1):
        output_fn(f"  {index}. {name}")
    output_fn("  all. Đánh giá tất cả người chưa có kết quả")
    while True:
        answer = input_fn("\nChọn số thứ tự, all hoặc q để thoát: ").strip()
        if answer.casefold() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if answer.casefold() == "all":
            return "all"
        try:
            selected = int(answer)
        except ValueError:
            output_fn("Vui lòng nhập một số trong danh sách.")
            continue
        if 1 <= selected <= len(names):
            return names[selected - 1]
        output_fn("Số đã chọn nằm ngoài danh sách.")


def _name_token_signature(value: str) -> tuple[str, ...]:
    return tuple(sorted(normalize_name(value).casefold().split()))


def resolve_game_participant_name(collection: Collection, participant_name: str) -> str:
    """Use an exact name, or a unique reordered-token match for legacy data."""
    if collection.count_documents({"participantName": participant_name, "status": "completed"}) > 0:
        return participant_name
    signature = _name_token_signature(participant_name)
    matches = []
    for candidate in collection.distinct("participantName", {"participantName": {"$type": "string"}}):
        try:
            if _name_token_signature(candidate) == signature:
                matches.append(candidate)
        except ValueError:
            continue
    return matches[0] if len(matches) == 1 else participant_name


def _extract_email(document: Mapping[str, Any]) -> str:
    candidates: Iterable[Any] = (
        document.get("participantEmail"),
        document.get("participant_email"),
        document.get("email"),
        document.get("participant", {}).get("email")
        if isinstance(document.get("participant"), Mapping)
        else None,
    )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return normalize_email(candidate)
        except ValueError:
            continue
    raise FinalEvaluationCLIError(
        "Bản questionnaire mới nhất không có participantEmail/email hợp lệ."
    )


def load_participant_data(
    questionnaire_collection: Collection,
    game_collection: Collection,
    participant_name: str,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], str]:
    questionnaires = list(questionnaire_collection.find({"participant.name": participant_name}))
    if not questionnaires:
        questionnaires = list(questionnaire_collection.find({"participantName": participant_name}))
    if not questionnaires:
        questionnaires = [
            document
            for document in questionnaire_collection.find(
                {"$or": [{"participant.name": {"$type": "string"}},
                         {"participantName": {"$type": "string"}}]}
            )
            if normalize_name(
                document.get("participant", {}).get("name", "")
                if isinstance(document.get("participant"), Mapping)
                else document.get("participantName", "")
            ).casefold() == participant_name.casefold()
        ]
    if not questionnaires:
        raise FinalEvaluationCLIError("Không tìm thấy questionnaire của người đã chọn.")
    questionnaires.sort(key=_sort_value, reverse=True)
    questionnaire = questionnaires[0]
    email = _extract_email(questionnaire)
    available_dimensions = set(extract_dimensions(questionnaire))
    missing_dimensions = sorted(set(DIMENSION_IDS) - available_dimensions)
    if missing_dimensions:
        raise FinalEvaluationCLIError(
            "Questionnaire không có đủ 28 mã DESMAP. Thiếu: " + ", ".join(missing_dimensions)
        )

    game_participant_name = resolve_game_participant_name(game_collection, participant_name)
    game_query = {"participantName": game_participant_name, "status": "completed"}
    games = list(game_collection.find(game_query).sort("completedAtUtc", ASCENDING))
    if not games:
        raise FinalEvaluationCLIError(
            "Người đã chọn chưa có game_result hoàn tất; không tạo FinalAssessment một phần."
        )
    return _json_safe(questionnaire), email, [_json_safe(game) for game in games], game_participant_name


def _bounded_messages(messages: list[dict[str, str]], max_chars: int) -> list[dict[str, str]]:
    total = sum(len(message["content"]) for message in messages)
    if total > max_chars:
        raise FinalEvaluationCLIError(
            f"Dữ liệu và system prompt dài {total} ký tự, vượt giới hạn {max_chars}. "
            "Hãy tăng FINAL_EVALUATION_MAX_PROMPT_CHARS hoặc rút gọn dữ liệu nguồn."
        )
    return messages


async def _write_field(
    service: Generator, *, field: str, instruction: str, facts: Mapping[str, Any],
    max_characters: int, max_prompt_chars: int, max_tokens: int,
    sentence_range: tuple[int, int] | None = None,
) -> str:
    def parse_answer(answer: str) -> str:
        text = parse_text_field(answer, max_characters=max_characters)
        if sentence_range is not None:
            count = len(re.split(r'(?<=[.!?])\s+', text.strip()))
            if not sentence_range[0] <= count <= sentence_range[1]:
                raise FinalEvaluationOutputError("Nhận xét nghề không đúng số câu được yêu cầu.")
        return text

    messages = _bounded_messages(build_text_field_messages(
        field=field, instruction=instruction, facts=facts, max_characters=max_characters
    ), max_prompt_chars)
    options = {"temperature": 0.2, "top_p": 0.9, "max_tokens": max_tokens}
    answer = await service.generate(messages, options=options, max_message_chars=max_prompt_chars)
    try:
        return parse_answer(answer)
    except FinalEvaluationOutputError:
        pass

    reminder = (
        f"\nViết lại đúng một giá trị, tối đa {max_characters} ký tự. "
        "Không trả JSON, markdown, tên trường hoặc lời giải thích.\n/no_think"
    )
    repair_messages = [messages[0], {
        "role": "user",
        "content": messages[1]["content"].removesuffix("\n/no_think") + reminder,
    }]
    _bounded_messages(repair_messages, max_prompt_chars)
    answer = await service.generate(
        repair_messages,
        options={**options, "temperature": 0.0, "top_p": 1.0},
        max_message_chars=max_prompt_chars,
    )
    try:
        return parse_answer(answer)
    except FinalEvaluationOutputError as exc:
        raise FinalEvaluationOutputError(
            f"Trường {field} không hợp lệ sau khi viết lại "
            f"({len(answer.strip())}/{max_characters} ký tự): {exc}"
        ) from exc


async def _write_icon(
    service: Generator, *, field: str, facts: Mapping[str, Any], max_prompt_chars: int,
) -> str | None:
    messages = _bounded_messages(
        build_text_field_messages(
            field=field,
            instruction="Chọn đúng một icon phù hợp với năng lực được mô tả trong facts.",
            facts=facts,
            max_characters=20,
        ),
        max_prompt_chars,
    )
    answer = await service.generate(
        messages,
        options={"temperature": 0.0, "top_p": 1.0, "max_tokens": 16},
        max_message_chars=max_prompt_chars,
    )
    try:
        icon = parse_text_field(answer, max_characters=20)
    except FinalEvaluationOutputError:
        return None
    return icon if icon in FINDING_ICON_IDS else None


async def generate_assessment(
    service: Generator, *, participant_name: str, participant_email: str,
    questionnaire: Mapping[str, Any], game_results: list[Mapping[str, Any]],
    completed_at: str, max_prompt_chars: int, max_tokens: int,
) -> FinalAssessment:
    """Weight VR in code, ask the writer for isolated values, then assemble the contract."""
    dimensions = extract_dimensions(questionnaire)
    missing = sorted(set(DIMENSION_IDS) - set(dimensions))
    if missing:
        raise FinalEvaluationCLIError("Questionnaire thiếu điểm cho: " + ", ".join(missing))
    groups = group_scores(dimensions)
    try:
        vr = combine_game_results(game_results, dimensions)
    except ValueError as exc:
        raise FinalEvaluationCLIError(str(exc)) from exc
    experience_name = vr.experience_name
    behaviours = vr.behaviours
    alignment = vr.alignment
    weighted_vr = vr.weighted_results
    findings = vr.findings
    selected_interests = questionnaire.get("careerInterests", [])
    candidates = rank_careers(dimensions, behaviours, selected_interests, limit=7)
    if len(candidates) < 7:
        raise FinalEvaluationCLIError(
            "Không có đủ bảy nghề ứng viên cho các nhóm careerInterests đã chọn."
        )

    async def write(field: str, instruction: str, facts: Mapping[str, Any], limit: int,
                    sentence_range: tuple[int, int] | None = None) -> str:
        return await _write_field(service, field=field, instruction=instruction, facts=facts,
            max_characters=limit, max_prompt_chars=max_prompt_chars, max_tokens=max_tokens,
            sentence_range=sentence_range)

    stage_assessments = {}
    stage_instructions = {
        "D": "Viết 2-3 câu về điều người tham gia coi trọng trong công việc. Không gọi điểm thấp là điểm yếu.",
        "E": "Viết 2-3 câu về các năng lực đang thể hiện và một hướng luyện có căn cứ.",
        "S": "Viết 2-3 câu về cách phối hợp hoặc đóng góp khi làm cùng người khác.",
        "M": "Viết 2-3 câu về cách xử lý thông tin, phân tích và quyết định.",
        "A": "Viết 2-3 câu về cách phản ứng khi yêu cầu hoặc thông tin thay đổi.",
        "P": "Viết 2-3 câu về cách duy trì hiệu quả khi có áp lực. Không chẩn đoán khả năng chịu đựng.",
    }
    for group in "DESMAP":
        items = sorted((item for code, item in dimensions.items() if code.startswith(group)),
                       key=lambda item: item.score, reverse=True)
        facts = {"group": group, "groupScoreCalculatedByBackend": groups[group],
                 "dimensions": [{"code": item.code, "name": item.name, "score": round(item.score),
                                  "description": item.description} for item in items]}
        stage_assessments[group] = await write(
            f"stageAssessments.{group}", stage_instructions[group], facts, 900
        )

    finding_values = []
    for plan in findings:
        base_facts = {"kindCalculatedByBackend": plan.kind,
                      "findingTitle": plan.title, "questionnaireFact": plan.questionnaire_fact,
                      "vrFact": plan.vr_fact}
        icon = await _write_icon(
            service, field=f"behaviourComparison.findings.{plan.id}.icon",
            facts=base_facts, max_prompt_chars=max_prompt_chars,
        )
        questionnaire_result = await write(
            f"behaviourComparison.findings.{plan.id}.questionnaireResult",
            "Viết một câu mô tả đúng kết quả tự đánh giá. Không nhắc đến VR.", base_facts, 500)
        vr_evidence = await write(
            f"behaviourComparison.findings.{plan.id}.vrEvidence",
            "Viết một câu mô tả đúng bằng chứng VR. Không biến phần không quan sát được thành điểm yếu.", base_facts, 500)
        summary = await write(
            f"behaviourComparison.findings.{plan.id}.summary",
            "Viết một câu kết luận đối chiếu hai nguồn theo đúng kind đã được backend tính; không đưa lời khuyên.", base_facts, 500)
        finding = {"id": plan.id, "kind": plan.kind, "title": plan.title,
            "questionnaireResult": questionnaire_result, "vrEvidence": vr_evidence,
            "summary": summary}
        if plan.kind in {"emerging", "development"}:
            previous_remedies = [item["remedy"] for item in finding_values if "remedy" in item]
            finding["remedy"] = await write(
                f"behaviourComparison.findings.{plan.id}.remedy",
                "Gợi ý một hoạt động học tập hoặc luyện tập cụ thể để phát triển kỹ năng này ngoài VR. "
                "Viết một câu ghép ngắn; không lặp ý, cách mở đầu hoặc cấu trúc của previousRemedies.",
                {"kindCalculatedByBackend": plan.kind, "findingTitle": plan.title,
                 "summary": summary, "behaviourCode": plan.behaviour_code,
                 "learningActivities": REMEDY_LEARNING_ACTIVITIES.get(plan.behaviour_code, ()),
                 "previousRemedies": previous_remedies}, 300)
        if icon is not None:
            finding["icon"] = icon
        finding_values.append(finding)

    career_values = []
    for index, ranked in enumerate(candidates):
        career = ranked.career
        score = ranked.compatibility_percent
        sentence_range = (3, 4) if index == 0 else (2, 3)
        try:
            description = await write(
                f"careerSuggestions.{career['id']}.description",
                f"Viết một đoạn nhận xét tự nhiên bằng tiếng Việt trong {sentence_range[0]}-{sentence_range[1]} câu ngắn. Tự chọn cách mở đầu và thứ tự ý, không theo khuôn cố định. "
                "Dẫn ít nhất hai yếu tố questionnaireEvidence bằng tên và điểm dạng số/100; phân biệt tự đánh giá với VR. "
                "Dẫn một tiêu chí vrEvidence cùng điểm có sẵn, liên hệ với công việc mà không biến điểm tiêu chí thành thao tác đã quan sát. "
                "Góp ý bằng hoạt động thử nghề cụ thể từ exploratoryActivity. Không chê bai, gắn nhãn hay lặp lời phủ định bảo đảm thành công. "
                "previousDescriptions chỉ để tránh lặp cách diễn đạt, không phải dữ kiện của nghề đang viết.",
                {"careerName": career['name'], "careerRequirements": career['description'],
                 "compatibilityPercentCalculatedByBackend": score,
                 "questionnaireEvidence": ranked.dimensions[:4], "vrEvidence": ranked.observations[:2],
                 "vrExperienceName": experience_name, "exploratoryActivity": career['activity'],
                 "previousDescriptions": [item['description'] for item in career_values[-2:]]}, 1200,
                sentence_range=sentence_range)
        except FinalEvaluationOutputError:
            description = career_description(ranked, primary=index == 0)
        supported = [item for item in ranked.dimensions if item['name'].casefold() in description.casefold() and f"{item['score']:g}/100" in description]
        if len(supported) < 2 or (ranked.observations and not any(
            item['evidence'] in description and f"{item['score']}/100" in description for item in ranked.observations
        )) or any(term in description.lower() for term in ("yếu kém", "kém cỏi", "không có năng lực", "không phù hợp", "bảo đảm thành công", "đảm bảo thành công")):
            description = career_description(ranked, primary=index == 0)
        career_values.append({"id": career['id'], "name": career['name'],
            "compatibilityPercent": score, "description": description})

    strongest = max(behaviours.values(), key=lambda item: item.score) if behaviours else None
    weakest = min(behaviours.values(), key=lambda item: item.score) if behaviours else None
    development_finding = next((item for item in findings if item.kind == "development"), None)
    emerging_finding = next((item for item in findings if item.kind == "emerging"), None)
    common_facts = {"experienceName": experience_name, "groupScoresCalculatedByBackend": groups,
                    "behaviourScoresCalculatedByBackend": {key: value.score for key, value in behaviours.items()},
                    "behaviourEvidence": {key: value.evidence for key, value in behaviours.items()},
                    "weightedVrResults": weighted_vr,
                    "alignmentCalculatedByBackend": alignment,
                    "developmentFinding": ({"title": development_finding.title,
                                            "questionnaireFact": development_finding.questionnaire_fact,
                                            "vrFact": development_finding.vr_fact}
                                           if development_finding else None)}
    final_text = {}
    final_instructions = {
        "headline": "Viết một câu kết luận ngắn, dễ hiểu, không gắn nhãn tính cách.",
        "workStyle": "Viết 1-2 câu mô tả cách người tham gia xử lý nhiệm vụ VR.",
        "benefit": "Viết 1-2 câu về lợi ích của cách làm đã quan sát trong công việc tương tự.",
        "challenge": "Nếu có developmentFinding, nêu một thách thức có căn cứ từ phát hiện đó; nếu không, nêu điều cần thử thêm trong bối cảnh khác mà không gọi điểm thấp nhất là điểm yếu.",
        "improvement": "Nếu có developmentFinding, nêu một hành động luyện kỹ năng đó; nếu không, nêu một hoạt động giúp thử thêm năng lực đang có. Viết 1-2 câu cụ thể.",
        "evidence": "Tóm tắt bằng chứng hành vi VR trong 1-2 câu.",
    }
    for field, instruction in final_instructions.items():
        final_text[field] = await write(f"finalEvaluation.{field}", instruction, common_facts, 650)

    strength_label = strongest.label if strongest else f"Nhóm {max(groups, key=groups.get)}"
    development_label = (
        development_finding.title if development_finding
        else f"Khám phá thêm {emerging_finding.title.lower()}" if emerging_finding
        else weakest.label if weakest else f"Nhóm {min(groups, key=groups.get)}"
    )
    document = {"version": 1, "participantName": normalize_name(participant_name),
        "participantEmail": normalize_email(participant_email), "completedAt": completed_at,
        "stageAssessments": stage_assessments,
        "dimensionLevels": {code: dimension_level(dimensions[code].score) for code in DIMENSION_IDS},
        "behaviourComparison": {"experienceName": experience_name, "findings": finding_values},
        "careerSuggestions": career_values,
        "finalEvaluation": {"experienceName": experience_name, **final_text,
            "strengthLabel": strength_label, "developmentLabel": development_label}}
    return FinalAssessment.model_validate(document)


def save_assessment(collection: Collection, assessment: FinalAssessment) -> None:
    document = assessment.model_dump(mode="json", by_alias=True, exclude_none=True)
    document["careerRankingVersion"] = CAREER_RANKING_VERSION
    document["generationProvider"] = "openrouter"
    document["generationModel"] = FINAL_EVALUATION_MODEL
    collection.create_index(
        [("participantName", ASCENDING), ("participantEmail", ASCENDING)],
        unique=True,
        name="participant_identity_unique",
    )
    collection.create_index([("completedAt", DESCENDING)], name="completed_at_desc")
    collection.replace_one(
        {
            "participantName": assessment.participant_name,
            "participantEmail": assessment.participant_email,
        },
        document,
        upsert=True,
    )


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise FinalEvaluationCLIError(f"{name} phải là số nguyên.") from exc
    if not minimum <= value <= maximum:
        raise FinalEvaluationCLIError(f"{name} phải nằm trong khoảng {minimum}-{maximum}.")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chọn người tham gia từ MongoDB và tạo đánh giá cuối qua OpenRouter với DeepSeek V4.1 Flash."
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--participant", help="Chọn trực tiếp participantName, bỏ qua menu.")
    selection.add_argument("--all", action="store_true", help="Đánh giá tất cả người chưa có kết quả.")
    parser.add_argument("--dry-run", action="store_true", help="In JSON nhưng không ghi MongoDB.")
    parser.add_argument(
        "--no-start-llm",
        action="store_true",
        help="Tùy chọn cũ được giữ để tương thích; final eval hiện chỉ dùng OpenRouter.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.mongodb_uri:
        raise FinalEvaluationCLIError("MONGODB_URI chưa được cấu hình.")
    model = DEFAULT_MODEL
    max_prompt_chars = _int_env(
        "FINAL_EVALUATION_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS, 8_000, 120_000
    )
    max_tokens = _int_env("FINAL_EVALUATION_MAX_TOKENS", DEFAULT_MAX_TOKENS, 128, 2_048)
    request_timeout = _int_env(
        "FINAL_EVALUATION_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS, 5, 120,
    )

    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5_000)
    service = FinalEvaluationOpenRouter(getattr(settings, "openrouter_api_key", None), timeout_seconds=request_timeout)
    try:
        database = client[settings.mongodb_database]
        questionnaire_collection = database[QUESTIONNAIRE_COLLECTION]
        game_collection = database[GAME_RESULTS_COLLECTION]
        evaluation_collection = database[FINAL_EVALUATIONS_COLLECTION]
        names = list_participant_names(questionnaire_collection)
        if not names:
            raise FinalEvaluationCLIError("Không tìm thấy participantName trong questionnaire_submissions.")
        if args.participant:
            requested = normalize_name(args.participant)
            participant_name = next(
                (name for name in names if name.casefold() == requested.casefold()), ""
            )
            if not participant_name:
                raise FinalEvaluationCLIError(
                    f"Không tìm thấy participantName '{requested}' trong questionnaire_submissions."
                )
            selected_names = [participant_name]
        else:
            selected = "all" if args.all else choose_participant(names)
            selected_names = names if selected == "all" else [selected]
        batch = args.all or (not args.participant and selected == "all")
        existing_names = set()
        for name in evaluation_collection.distinct(
            "participantName", {"participantName": {"$type": "string"}}
        ):
            try:
                existing_names.add(normalize_name(name).casefold())
            except ValueError:
                continue
        completed_count = skipped_count = failed_count = 0
        for participant_name in selected_names:
            if participant_name.casefold() in existing_names:
                print(f"Bỏ qua {participant_name}: đã có dữ liệu trong final_evaluations.")
                skipped_count += 1
                continue
            try:
                questionnaire, email, game_results, game_participant_name = load_participant_data(
                    questionnaire_collection, game_collection, participant_name
                )
                if evaluation_collection.count_documents({"participantEmail": email}, limit=1):
                    print(f"Bỏ qua {participant_name}: email đã có dữ liệu trong final_evaluations.")
                    skipped_count += 1
                    continue
                if game_participant_name != participant_name:
                    print(
                        "Đã nối game_results theo tên có cùng các thành phần: "
                        f"'{participant_name}' -> '{game_participant_name}'."
                    )
                completed_at = utc_now()
                print(
                    f"\nĐang đánh giá {participant_name} với {len(game_results)} game_result hoàn tất "
                    f"bằng model '{model}'...",
                    end="", flush=True,
                )
                analysis_started_at = time.perf_counter()
                try:
                    assessment = await generate_assessment(
                        service, participant_name=participant_name, participant_email=email,
                        questionnaire=questionnaire, game_results=game_results,
                        completed_at=completed_at, max_prompt_chars=max_prompt_chars,
                        max_tokens=max_tokens,
                    )
                except BaseException:
                    print()
                    raise
                analysis_seconds = round(time.perf_counter() - analysis_started_at)
                print(f" Hoàn thành ({analysis_seconds}s).")
                if args.dry_run:
                    print(json.dumps(assessment.model_dump(mode="json", by_alias=True, exclude_none=True), ensure_ascii=False, indent=2))
                    print("Dry run: chưa ghi MongoDB.")
                else:
                    save_assessment(evaluation_collection, assessment)
                    existing_names.add(participant_name.casefold())
                    print(
                        "Đã lưu đánh giá vào collection final_evaluations cho "
                        f"{assessment.participant_name} ({assessment.participant_email})."
                    )
                completed_count += 1
            except Exception as exc:
                if not batch:
                    raise
                failed_count += 1
                message = str(exc)
                if "mongodb" in message.casefold() or "auth" in message.casefold():
                    message = "Không thể kết nối hoặc xác thực MongoDB. Kiểm tra MONGODB_URI."
                print(f"Không thể đánh giá {participant_name}: {message}", file=sys.stderr)
        if batch:
            print(f"Tổng kết: hoàn thành {completed_count}, bỏ qua {skipped_count}, lỗi {failed_count}.")
        return 1 if failed_count else 0
    finally:
        await service.close()
        client.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        return asyncio.run(run(parse_args(argv)))
    except KeyboardInterrupt:
        print("\nĐã hủy.")
        return 130
    except (FinalEvaluationCLIError, FinalEvaluationOutputError, FinalEvaluationProviderError) as exc:
        print(f"Không thể tạo đánh giá: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        safe_message = str(exc)
        if "mongodb" in safe_message.casefold() or "auth" in safe_message.casefold():
            safe_message = "Không thể kết nối hoặc xác thực MongoDB. Kiểm tra MONGODB_URI."
        print(f"Không thể tạo đánh giá: {safe_message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
