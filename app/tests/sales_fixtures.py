"""Part 1 attempts for Part 2 tests; Part 2 always continues the Part 1 customer."""
import json
from pathlib import Path

PART1_NEEDS = ("Chào bạn! Mình muốn tìm một đôi giày để chạy bộ nhẹ khoảng 3 đến 5 cây số mỗi sáng. "
               "Mình mới tập nên thích đôi nào êm chân, nhẹ và thoáng một chút.\n"
               "Mình thường chạy trên đường nhựa, thỉnh thoảng đi bộ nữa. Ngân sách của mình khoảng "
               "một triệu rưỡi trở xuống. Bạn chọn giúp mình một đôi phù hợp nhé!")
PART1_SHOES = [
    {"shoeId": "Adudu", "name": "Adudu", "price": 1290000, "details": "Chạy bộ đường nhựa\nĐệm êm · Nhẹ · Thoáng"},
    {"shoeId": "Nyke", "name": "Nyke", "price": 1450000, "details": "Bóng rổ trong nhà\nÔm cổ chân · Đế chắc"},
    {"shoeId": "Columbu", "name": "Columbu", "price": 1890000, "details": "Chạy địa hình\nĐế bám sâu · Chống nước"},
]


def write_part1(directory, session_id: str, run_id: str, attempt_id: str | None = None, *,
                customer: str = "Lan", needs: str = PART1_NEEDS, selected: str = "Adudu") -> str:
    """Store a completed Part 1 attempt that a Part 2 session can link to."""
    attempt_id = attempt_id or "part1-" + session_id
    attempt = {"attemptId": attempt_id, "salesSessionId": session_id, "runId": run_id, "customerId": customer,
               "customerNeeds": needs, "selectedShoeId": selected, "bestFitShoeId": "Adudu",
               "availableShoes": PART1_SHOES}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"sales-persuasion-{attempt_id}.json").write_text(
        json.dumps(attempt, ensure_ascii=False), encoding="utf-8")
    return attempt_id
