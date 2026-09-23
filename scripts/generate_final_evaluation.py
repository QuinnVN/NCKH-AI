"""Interactive MongoDB-to-Qwen final evaluation CLI."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlparse

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
    parse_score_field,
    parse_text_field,
    utc_now,
)
from app.final_evaluation_calculation import (  # noqa: E402
    career_candidates,
    combine_game_results,
    dimension_level,
    extract_dimensions,
    group_scores,
)
from app.llm_service import LLMService, LLMServiceError  # noqa: E402


QUESTIONNAIRE_COLLECTION = "questionnaire_submissions"
GAME_RESULTS_COLLECTION = "game_results"
FINAL_EVALUATIONS_COLLECTION = "final_evaluations"
DEFAULT_MODEL = "qwen3-8b"
DEFAULT_MAX_PROMPT_CHARS = 16_000
DEFAULT_MAX_TOKENS = 512
DEFAULT_LLM_START_TIMEOUT_SECONDS = 900
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


async def available_model_ids(service: LLMService) -> set[str] | None:
    """Return model aliases, or None when the OpenAI-compatible endpoint is down."""
    try:
        response = await service.client.get(f"{service.base_url}/models")
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data")
        if not isinstance(models, list):
            return set()
        return {
            str(item["id"])
            for item in models
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
    except Exception:
        return None


def start_qwen_server_window(base_url: str, model: str) -> subprocess.Popen[bytes]:
    """Start the dedicated Qwen3 8B PowerShell script in another console."""
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise FinalEvaluationCLIError(
            "Không thể tự khởi động LLM cho một LLM_BASE_URL không nằm trên máy này."
        )
    if os.name != "nt":
        raise FinalEvaluationCLIError(
            "Tự mở cửa sổ LLM hiện chỉ hỗ trợ Windows. Hãy chạy scripts/run-qwen3-8b.ps1."
        )
    script = ROOT / "scripts" / "run-qwen3-8b.ps1"
    if not script.is_file():
        raise FinalEvaluationCLIError(f"Không tìm thấy script khởi động LLM: {script}")
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise FinalEvaluationCLIError("Không tìm thấy pwsh hoặc powershell trên PATH.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    llama_server_bin = os.environ.get("LLAMA_SERVER_BIN", "").strip()
    if llama_server_bin:
        command.extend(["-LlamaServerCommand", llama_server_bin])
    command.extend(["-Port", str(port), "-Alias", model])
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


async def ensure_qwen_server(
    service: LLMService,
    model: str,
    *,
    auto_start: bool,
    timeout_seconds: int,
    output_fn=print,
) -> bool:
    """Ensure that the configured endpoint serves the requested Qwen3 8B alias."""
    model_ids = await available_model_ids(service)
    if model_ids is not None:
        if model in model_ids:
            return False
        aliases = ", ".join(sorted(model_ids)) or "không có alias nào"
        raise FinalEvaluationCLIError(
            f"Cổng LLM đang chạy nhưng không có model '{model}'. Model hiện có: {aliases}. "
            "Hãy dừng server đang chiếm cổng rồi chạy lại CLI."
        )
    if not auto_start:
        raise FinalEvaluationCLIError(
            "Qwen3 8B chưa chạy. Bỏ --no-start-llm hoặc chạy scripts/run-qwen3-8b.ps1."
        )

    output_fn("Qwen3 8B chưa chạy. Đang mở server trong một cửa sổ PowerShell khác...")
    process = start_qwen_server_window(service.base_url, model)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        if process.poll() is not None:
            raise FinalEvaluationCLIError(
                f"Cửa sổ Qwen3 8B đã đóng với mã {process.returncode}. Xem lỗi trong cửa sổ LLM."
            )
        await asyncio.sleep(1)
        model_ids = await available_model_ids(service)
        if model_ids is not None and model in model_ids:
            output_fn(f"Qwen3 8B đã sẵn sàng với alias '{model}'.")
            return True
    raise FinalEvaluationCLIError(
        f"Qwen3 8B chưa sẵn sàng sau {timeout_seconds} giây. Cửa sổ LLM vẫn đang mở."
    )


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
) -> str:
    messages = _bounded_messages(build_text_field_messages(
        field=field, instruction=instruction, facts=facts, max_characters=max_characters
    ), max_prompt_chars)
    options = {"temperature": 0.2, "top_p": 0.9, "max_tokens": max_tokens}
    answer = await service.generate(messages, options=options, max_message_chars=max_prompt_chars)
    return parse_text_field(answer, max_characters=max_characters)


async def _write_score(
    service: Generator, *, field: str, facts: Mapping[str, Any], max_prompt_chars: int,
) -> int:
    messages = _bounded_messages(
        build_text_field_messages(
            field=field,
            instruction=(
                "Đề xuất một compatibilityPercent cho nghề này. Chỉ trả một số nguyên 0-100. "
                "Dùng nhóm nghề quan tâm, điểm DESMAP và weighted VR facts đã cung cấp."
            ),
            facts=facts,
            max_characters=3,
        ),
        max_prompt_chars,
    )
    answer = await service.generate(
        messages,
        options={"temperature": 0.0, "top_p": 1.0, "max_tokens": 16},
        max_message_chars=max_prompt_chars,
    )
    return parse_score_field(answer)


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
    """Weight VR in code, ask Qwen for isolated values, then assemble the contract."""
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
    candidates = career_candidates(selected_interests)
    if len(candidates) < 7:
        raise FinalEvaluationCLIError(
            "Không có đủ bảy nghề ứng viên cho các nhóm careerInterests đã chọn."
        )

    async def write(field: str, instruction: str, facts: Mapping[str, Any], limit: int) -> str:
        return await _write_field(service, field=field, instruction=instruction, facts=facts,
            max_characters=limit, max_prompt_chars=max_prompt_chars, max_tokens=max_tokens)

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

    scored_candidates = []
    for candidate in candidates:
        score = await _write_score(
            service,
            field=f"careerSuggestions.{candidate.id}.compatibilityPercent",
            facts={
                "careerName": candidate.name,
                "careerInterestGroup": candidate.interest_group,
                "selectedCareerInterests": selected_interests,
                "careerRequirements": candidate.requirements,
                "desmapGroupScores": groups,
                "vrExperienceName": experience_name,
                "weightedVrResults": weighted_vr,
                "weightedVrAlignment": alignment,
            },
            max_prompt_chars=max_prompt_chars,
        )
        scored_candidates.append((score, candidate))
    top_careers = sorted(
        scored_candidates, key=lambda item: (-item[0], item[1].name)
    )[:7]

    career_values = []
    for score, career in top_careers:
        description = await write(
            f"careerSuggestions.{career.id}.description",
            "Viết 2 câu giải thích vì sao đây là nghề nên khám phá. Nêu rõ đây không phải bảo đảm thành công.",
            {"careerName": career.name, "careerInterestGroup": career.interest_group,
             "careerRequirements": career.requirements, "compatibilityPercentSuggestedByAI": score,
             "selectedCareerInterests": selected_interests, "desmapGroupScores": groups,
             "vrExperienceName": experience_name, "weightedVrResults": weighted_vr,
             "weightedVrAlignment": alignment}, 800)
        career_values.append({"id": career.id, "name": career.name,
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
        description="Chọn người tham gia từ MongoDB và tạo đánh giá cuối bằng Qwen3 8B."
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--participant", help="Chọn trực tiếp participantName, bỏ qua menu.")
    selection.add_argument("--all", action="store_true", help="Đánh giá tất cả người chưa có kết quả.")
    parser.add_argument("--dry-run", action="store_true", help="In JSON nhưng không ghi MongoDB.")
    parser.add_argument(
        "--no-start-llm",
        action="store_true",
        help="Không tự mở cửa sổ Qwen3 8B khi model chưa chạy.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.mongodb_uri:
        raise FinalEvaluationCLIError("MONGODB_URI chưa được cấu hình.")
    model = os.environ.get("FINAL_EVALUATION_LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if "qwen3-8b" not in model.casefold():
        raise FinalEvaluationCLIError(
            "FINAL_EVALUATION_LLM_MODEL phải trỏ tới alias Qwen3 8B, ví dụ qwen3-8b."
        )
    max_prompt_chars = _int_env(
        "FINAL_EVALUATION_MAX_PROMPT_CHARS", DEFAULT_MAX_PROMPT_CHARS, 8_000, 120_000
    )
    max_tokens = _int_env("FINAL_EVALUATION_MAX_TOKENS", DEFAULT_MAX_TOKENS, 128, 2_048)
    llm_start_timeout = _int_env(
        "FINAL_EVALUATION_LLM_START_TIMEOUT_SECONDS",
        DEFAULT_LLM_START_TIMEOUT_SECONDS,
        30,
        3_600,
    )
    llm_settings = replace(settings, llm_model=model)

    client = MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=5_000)
    service = LLMService(llm_settings)
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
        model_ready = False
        model_starting = False
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
                if not model_ready:
                    model_starting = True
                    await ensure_qwen_server(
                        service, model, auto_start=not args.no_start_llm,
                        timeout_seconds=llm_start_timeout,
                    )
                    model_ready = True
                    model_starting = False
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
                if not batch or model_starting:
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
    except (FinalEvaluationCLIError, FinalEvaluationOutputError, LLMServiceError) as exc:
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
