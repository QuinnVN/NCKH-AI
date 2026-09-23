"""Deterministic calculations used to assemble a final DESMAP evaluation."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    behaviour_code: str


@dataclass(frozen=True)
class CareerCandidate:
    id: str
    name: str
    interest_group: str
    requirements: str


@dataclass(frozen=True)
class VrSummary:
    experience_name: str
    behaviours: dict[str, BehaviourFact]
    weighted_results: list[dict[str, Any]]
    findings: list[FindingPlan]
    alignment: int | None


CAREERS_BY_INTEREST: dict[str, tuple[tuple[str, str, str], ...]] = {
    "business-entrepreneurship": (
        ("nha-khoi-nghiep", "Nhà khởi nghiệp", "nhận ra cơ hội, quyết định trong bất định, thích ứng và thuyết phục"),
        ("chuyen-vien-phat-trien-kinh-doanh", "Chuyên viên phát triển kinh doanh", "phân tích thị trường, xây dựng quan hệ và theo đuổi mục tiêu"),
        ("quan-ly-san-pham", "Quản lý sản phẩm", "hiểu nhu cầu, ưu tiên dữ kiện và phối hợp nhiều nhóm"),
        ("chuyen-vien-marketing", "Chuyên viên marketing", "hiểu khách hàng, sáng tạo thông điệp và điều chỉnh theo phản hồi"),
        ("chuyen-vien-tai-chinh", "Chuyên viên tài chính", "phân tích số liệu, kiểm soát rủi ro và quyết định có căn cứ"),
        ("quan-ly-ban-hang", "Quản lý bán hàng", "giao tiếp, dẫn dắt mục tiêu và xử lý tình huống khách hàng"),
        ("chuyen-vien-thuong-mai-dien-tu", "Chuyên viên thương mại điện tử", "đọc dữ liệu, hiểu hành vi mua và thử nghiệm giải pháp"),
    ),
    "design-creative": (
        ("thiet-ke-do-hoa", "Nhà thiết kế đồ họa", "tư duy hình ảnh, phát triển ý tưởng và làm việc theo yêu cầu"),
        ("thiet-ke-ux-ui", "Nhà thiết kế UX/UI", "nghiên cứu người dùng, giải quyết vấn đề và cải tiến qua phản hồi"),
        ("kien-truc-su", "Kiến trúc sư", "kết hợp sáng tạo, phân tích ràng buộc và quản lý chi tiết"),
        ("thiet-ke-noi-that", "Nhà thiết kế nội thất", "hiểu nhu cầu, hình dung không gian và cân bằng nhiều điều kiện"),
        ("thiet-ke-san-pham", "Nhà thiết kế sản phẩm", "tạo giải pháp, thử nghiệm và điều chỉnh theo nhu cầu sử dụng"),
        ("dao-dien-nghe-thuat", "Giám đốc nghệ thuật", "định hướng ý tưởng, giao tiếp và phối hợp sản xuất sáng tạo"),
        ("hoa-si-minh-hoa", "Họa sĩ minh họa", "diễn đạt ý tưởng bằng hình ảnh, tập trung và phát triển phong cách"),
    ),
    "environment-sustainability": (
        ("ky-su-moi-truong", "Kỹ sư môi trường", "phân tích hệ thống, giải quyết vấn đề và tuân thủ tiêu chuẩn"),
        ("chuyen-vien-phat-trien-ben-vung", "Chuyên viên phát triển bền vững", "đánh giá tác động, phối hợp các bên và lập kế hoạch"),
        ("ky-su-nang-luong-tai-tao", "Kỹ sư năng lượng tái tạo", "năng lực kỹ thuật, phân tích dữ liệu và thích ứng công nghệ"),
        ("chuyen-vien-bao-ton", "Chuyên viên bảo tồn", "quan sát thực địa, làm việc dài hạn và phối hợp cộng đồng"),
        ("nghien-cuu-moi-truong", "Nhà nghiên cứu môi trường", "thu thập bằng chứng, phân tích và kết luận thận trọng"),
        ("chuyen-vien-esg", "Chuyên viên ESG", "đọc tiêu chuẩn, phân tích rủi ro và giao tiếp với tổ chức"),
        ("quan-ly-tai-nguyen", "Chuyên viên quản lý tài nguyên", "lập kế hoạch, cân bằng lợi ích và ra quyết định có căn cứ"),
    ),
    "health-wellbeing": (
        ("bac-si", "Bác sĩ", "xử lý thông tin, quyết định, giải quyết vấn đề và làm việc dưới áp lực"),
        ("dieu-duong", "Điều dưỡng", "chăm sóc, phối hợp, chú ý chi tiết và phản ứng ổn định"),
        ("duoc-si", "Dược sĩ", "kiến thức chuyên môn, độ chính xác và tư vấn rõ ràng"),
        ("chuyen-gia-tam-ly", "Chuyên gia tâm lý", "lắng nghe, phân tích hành vi và xây dựng quan hệ tin cậy"),
        ("chuyen-gia-dinh-duong", "Chuyên gia dinh dưỡng", "đánh giá thông tin, tư vấn và theo dõi thay đổi"),
        ("chuyen-vien-y-te-cong-cong", "Chuyên viên y tế công cộng", "phân tích dữ liệu, lập chương trình và làm việc với cộng đồng"),
        ("ky-thuat-vien-xet-nghiem", "Kỹ thuật viên xét nghiệm", "tuân thủ quy trình, độ chính xác và tập trung"),
    ),
    "law-public-service": (
        ("luat-su", "Luật sư", "dùng bằng chứng, lập luận, ra quyết định và trình bày thuyết phục"),
        ("chuyen-vien-phap-che", "Chuyên viên pháp chế", "đọc quy định, phân tích rủi ro và tư vấn có căn cứ"),
        ("cong-chung-vien", "Công chứng viên", "kiểm tra hồ sơ, độ chính xác và trách nhiệm thủ tục"),
        ("chuyen-vien-hanh-chinh-cong", "Chuyên viên hành chính công", "xử lý quy trình, phục vụ người dân và phối hợp"),
        ("chuyen-vien-chinh-sach-cong", "Chuyên viên chính sách công", "nghiên cứu, đánh giá tác động và viết đề xuất"),
        ("chuyen-vien-tuan-thu", "Chuyên viên tuân thủ", "nhận diện rủi ro, kiểm tra quy tắc và báo cáo rõ ràng"),
        ("chuyen-vien-quan-he-quoc-te", "Chuyên viên quan hệ quốc tế", "phân tích bối cảnh, giao tiếp và thích ứng văn hóa"),
    ),
    "media-communication": (
        ("nha-bao", "Nhà báo", "tìm kiếm thông tin, đặt câu hỏi và trình bày chính xác"),
        ("chuyen-vien-quan-he-cong-chung", "Chuyên viên quan hệ công chúng", "giao tiếp, xử lý tình huống và quản lý thông điệp"),
        ("nha-sang-tao-noi-dung", "Nhà sáng tạo nội dung", "phát triển ý tưởng, hiểu người xem và thử nghiệm định dạng"),
        ("bien-tap-vien", "Biên tập viên", "đánh giá thông tin, chỉnh sửa chi tiết và giữ tính nhất quán"),
        ("chuyen-vien-truyen-thong-so", "Chuyên viên truyền thông số", "đọc dữ liệu, xây dựng nội dung và điều chỉnh chiến dịch"),
        ("bien-kich", "Biên kịch", "xây dựng câu chuyện, phát triển ý tưởng và tiếp nhận phản hồi"),
        ("nha-san-xuat-truyen-thong", "Nhà sản xuất truyền thông", "lập kế hoạch, phối hợp đội ngũ và xử lý thay đổi"),
    ),
    "operations-trades": (
        ("chuyen-vien-logistics", "Chuyên viên logistics", "lập kế hoạch, xử lý phát sinh và theo dõi tiến độ"),
        ("chuyen-vien-chuoi-cung-ung", "Chuyên viên chuỗi cung ứng", "phân tích luồng hàng, phối hợp và quản lý rủi ro"),
        ("quan-ly-van-hanh", "Quản lý vận hành", "tối ưu quy trình, quyết định và duy trì hiệu quả"),
        ("ky-thuat-vien-dien", "Kỹ thuật viên điện", "năng lực kỹ thuật, tuân thủ an toàn và xử lý sự cố"),
        ("ky-thuat-vien-co-khi", "Kỹ thuật viên cơ khí", "đọc hệ thống, thao tác chính xác và giải quyết lỗi"),
        ("chuyen-vien-quan-ly-chat-luong", "Chuyên viên quản lý chất lượng", "kiểm tra tiêu chuẩn, phân tích lỗi và cải tiến"),
        ("dieu-phoi-san-xuat", "Điều phối sản xuất", "sắp xếp nguồn lực, phối hợp và phản ứng với thay đổi"),
    ),
    "people-education": (
        ("giao-vien", "Giáo viên", "truyền đạt, quan sát người học và điều chỉnh cách hướng dẫn"),
        ("chuyen-vien-dao-tao", "Chuyên viên đào tạo", "thiết kế hoạt động học, giao tiếp và đánh giá tiến bộ"),
        ("chuyen-vien-nhan-su", "Chuyên viên nhân sự", "lắng nghe, xử lý thông tin và hỗ trợ quan hệ lao động"),
        ("tu-van-huong-nghiep", "Chuyên viên tư vấn hướng nghiệp", "lắng nghe, phân tích hồ sơ và hướng dẫn có căn cứ"),
        ("cong-tac-xa-hoi", "Nhân viên công tác xã hội", "đồng cảm, phối hợp nguồn lực và xử lý tình huống"),
        ("quan-ly-giao-duc", "Chuyên viên quản lý giáo dục", "lập kế hoạch, phối hợp và đánh giá chương trình"),
        ("giao-vien-giao-duc-dac-biet", "Giáo viên giáo dục đặc biệt", "kiên nhẫn, quan sát và điều chỉnh hỗ trợ cá nhân"),
    ),
    "science-research": (
        ("nha-nghien-cuu", "Nhà nghiên cứu", "đặt giả thuyết, phân tích bằng chứng và làm việc có hệ thống"),
        ("chuyen-vien-phong-thi-nghiem", "Chuyên viên phòng thí nghiệm", "tuân thủ quy trình, đo lường và ghi nhận chính xác"),
        ("chuyen-vien-cong-nghe-sinh-hoc", "Chuyên viên công nghệ sinh học", "kiến thức khoa học, phân tích và thử nghiệm"),
        ("nha-khoa-hoc-du-lieu", "Nhà khoa học dữ liệu", "mô hình hóa, phân tích dữ liệu và kiểm tra giả thuyết"),
        ("chuyen-vien-thong-ke", "Chuyên viên thống kê", "tư duy định lượng, kiểm tra dữ liệu và diễn giải kết quả"),
        ("nghien-cuu-thi-truong", "Chuyên viên nghiên cứu thị trường", "thiết kế nghiên cứu, phân tích và hiểu hành vi"),
        ("nghien-cuu-y-sinh", "Nhà nghiên cứu y sinh", "kết hợp khoa học sức khỏe, thí nghiệm và đánh giá bằng chứng"),
    ),
    "technology-engineering": (
        ("ky-su-phan-mem", "Kỹ sư phần mềm", "phân tích hệ thống, giải quyết vấn đề và thích ứng công nghệ"),
        ("chuyen-vien-phan-tich-du-lieu", "Chuyên viên phân tích dữ liệu", "làm sạch dữ liệu, phân tích và trình bày kết luận"),
        ("chuyen-gia-an-ninh-mang", "Chuyên gia an ninh mạng", "nhận diện rủi ro, điều tra và phản ứng dưới áp lực"),
        ("ky-su-ai", "Kỹ sư AI", "toán và lập trình, thử nghiệm mô hình và đánh giá kết quả"),
        ("ky-su-tu-dong-hoa", "Kỹ sư tự động hóa", "thiết kế hệ thống, xử lý lỗi và tích hợp thiết bị"),
        ("ky-su-dien-tu", "Kỹ sư điện tử", "tư duy kỹ thuật, đo kiểm và giải quyết sự cố"),
        ("quan-tri-he-thong", "Quản trị hệ thống", "vận hành hạ tầng, xử lý sự cố và duy trì ổn định"),
    ),
}


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


def weighted_behaviour_data(
    game_id: str, behaviours: Mapping[str, BehaviourFact]
) -> dict[str, dict[str, float | int | str]]:
    """Multiply each observed VR score by the experienced-career weight."""
    result: dict[str, dict[str, float | int | str]] = {}
    for code, weight in EXPERIENCE_WEIGHTS.get(game_id, {}).items():
        behaviour = behaviours.get(code)
        if behaviour is None:
            continue
        result[code] = {
            "label": behaviour.label,
            "score": behaviour.score,
            "weight": weight,
            "weightedContribution": round(behaviour.score * weight, 2),
            "evidence": behaviour.evidence,
        }
    return result


def build_findings(behaviours: Mapping[str, BehaviourFact]) -> list[FindingPlan]:
    plans = []
    for item in sorted(behaviours.values(), key=lambda value: abs(value.score - value.self_score), reverse=True):
        kind = (
            "development" if item.score < 70 or item.self_score - item.score >= 15
            else "emerging" if item.self_score < 61 or item.score - item.self_score >= 10
            else "confirmed" if item.self_score >= 61
            else "emerging"
        )
        plans.append(FindingPlan(item.code, kind, item.label,
            f"Điểm tự đánh giá liên quan: {item.self_score}/100.", f"{item.evidence}: {item.score}/100.",
            item.score, item.self_score, item.code))
    return plans


def combine_game_results(
    games: list[Mapping[str, Any]], dimensions: Mapping[str, DimensionFact]
) -> VrSummary:
    """Use every completed VR activity, merging repeat runs of the same game."""
    by_game: dict[str, list[dict[str, BehaviourFact]]] = {}
    for game in games:
        game_id = str(game.get("gameId", "")).casefold()
        behaviours = calculate_behaviours(game, dimensions)
        if behaviours:
            by_game.setdefault(game_id, []).append(behaviours)
    if not by_game:
        raise ValueError("Không có hành vi VR có thể chấm từ game_results đã hoàn tất.")

    pooled: dict[str, list[BehaviourFact]] = {}
    weighted_results: list[dict[str, Any]] = []
    all_findings: list[FindingPlan] = []
    alignments: list[int] = []
    names: list[str] = []
    per_game: dict[str, dict[str, BehaviourFact]] = {}
    for game_id, runs in by_game.items():
        name = "Bác sĩ" if game_id in {"doctor", "clinic"} else EXPERIENCE_NAMES.get(game_id, game_id)
        if name not in names:
            names.append(name)
        game_behaviours: dict[str, BehaviourFact] = {}
        for code in dict.fromkeys(code for run in runs for code in run):
            facts = [run[code] for run in runs if code in run]
            score = round(fmean(fact.score for fact in facts))
            evidence = "; ".join(dict.fromkeys(fact.evidence for fact in facts))
            game_behaviours[code] = BehaviourFact(
                code, facts[0].label, score, evidence, facts[0].self_score
            )
            pooled.setdefault(code, []).append(game_behaviours[code])
        alignment = behavioural_alignment(game_id, game_behaviours)
        per_game[game_id] = game_behaviours
        if alignment is not None:
            alignments.append(alignment)
        weighted_results.append({
            "gameId": game_id, "experienceName": name, "runCount": len(runs),
            "behaviours": weighted_behaviour_data(game_id, game_behaviours),
            "weightedAlignment": alignment,
        })
        for plan in build_findings(game_behaviours):
            all_findings.append(replace(
                plan, id=f"{game_id}-{plan.id}",
                vr_fact=f"Trong trải nghiệm {name}, {plan.vr_fact}",
            ))

    combined: dict[str, BehaviourFact] = {}
    for code, facts in pooled.items():
        combined[code] = BehaviourFact(
            code, facts[0].label, round(fmean(fact.score for fact in facts)),
            "; ".join(
                f"{item['experienceName']} ({item['gameId']}): "
                f"{per_game[item['gameId']][code].evidence} "
                f"({per_game[item['gameId']][code].score}/100)"
                for item in weighted_results if code in per_game[item["gameId"]]
            ),
            facts[0].self_score,
        )

    selected = select_balanced_findings(all_findings)
    return VrSummary(
        ", ".join(names), combined, weighted_results, selected[:12],
        round(fmean(alignments)) if alignments else None,
    )


def select_balanced_findings(candidates: list[FindingPlan]) -> list[FindingPlan]:
    """Choose three distinct perspectives, then add other observed factors."""
    if not candidates:
        raise ValueError("Không có bằng chứng hành vi VR để đối chiếu.")
    selected: list[FindingPlan] = []
    used_ids: set[str] = set()
    used_behaviours: set[str] = set()
    rankings = {
        "confirmed": sorted(
            candidates,
            key=lambda item: (min(item.score, item.self_score), -abs(item.score - item.self_score), item.score),
            reverse=True,
        ),
        "emerging": sorted(
            candidates,
            key=lambda item: (item.score - item.self_score > 0,
                              abs(item.score - item.self_score), max(item.score, item.self_score)),
            reverse=True,
        ),
        "development": sorted(
            candidates,
            key=lambda item: (item.score, -(item.self_score - item.score)),
        ),
    }
    for kind in ("confirmed", "emerging", "development"):
        ranked = rankings[kind]
        source = next(
            (item for item in ranked if item.behaviour_code not in used_behaviours),
            next((item for item in ranked if item.id not in used_ids), ranked[0]),
        )
        finding_id = source.id if source.id not in used_ids else f"{source.id}-{kind}"
        title = source.title if source.behaviour_code not in used_behaviours else f"{source.title} cần xem thêm"
        questionnaire_fact, vr_fact = source.questionnaire_fact, source.vr_fact
        if kind == "confirmed":
            vr_fact += (
                " Đây là mức thể hiện rõ nhất tương đối trong các hành vi VR đã chấm; "
                "không suy ra điểm mạnh tuyệt đối nếu điểm còn thấp."
            )
        elif kind == "emerging":
            if source.score > source.self_score:
                vr_fact += " Hành vi VR thể hiện rõ hơn mức tự đánh giá liên quan."
            else:
                questionnaire_fact += (
                    " Đây là tiềm năng từ tự đánh giá; hành vi VR hiện chưa xác nhận mức đó."
                )
        else:
            vr_fact += (
                " Đây là yếu tố nên luyện hoặc kiểm chứng thêm trong nhiệm vụ khó hơn."
            )
        selected.append(replace(
            source, id=finding_id, kind=kind, title=title,
            questionnaire_fact=questionnaire_fact, vr_fact=vr_fact,
        ))
        used_ids.add(finding_id)
        used_behaviours.add(source.behaviour_code)

    for item in sorted(candidates, key=lambda fact: abs(fact.score - fact.self_score), reverse=True):
        if item.behaviour_code not in used_behaviours and len(selected) < 6:
            selected.append(item)
            used_behaviours.add(item.behaviour_code)
    return selected


def career_candidates(interests: Any) -> list[CareerCandidate]:
    """Build the candidate pool only from selected career-interest groups."""
    raw_selected = [item for item in interests if isinstance(item, str)] if isinstance(interests, list) else []
    exploring = "exploring" in raw_selected
    selected = list(dict.fromkeys(item for item in raw_selected if item in CAREERS_BY_INTEREST))
    raw: list[tuple[str, str, str, str]] = []
    if exploring or not selected:
        for group, careers in CAREERS_BY_INTEREST.items():
            raw.extend((career_id, name, group, requirements) for career_id, name, requirements in careers[:2])
    else:
        for group in selected:
            raw.extend(
                (career_id, name, group, requirements)
                for career_id, name, requirements in CAREERS_BY_INTEREST[group]
            )
    unique: dict[str, CareerCandidate] = {}
    for career_id, name, group, requirements in raw:
        unique.setdefault(career_id, CareerCandidate(career_id, name, group, requirements))
    return list(unique.values())
