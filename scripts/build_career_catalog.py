"""Export the O*NET-based career catalog to the AI and web consumers.

Weights come from data/onet-extract.json (see scripts/build_onet_extract.py) through the reviewed
mapping in data/onet-career-mapping.json. Includes information from the O*NET Database by the U.S.
Department of Labor, Employment and Training Administration (USDOL/ETA), used under the CC BY 4.0
license. The DESMAP team has modified all or some of this information. USDOL/ETA has not approved,
endorsed, or tested these modifications.
"""
import json
from pathlib import Path
from statistics import fmean
from app.final_evaluation_calculation import CAREERS_BY_INTEREST

ROOT = Path(__file__).resolve().parent.parent
EXTRA_CAREERS = {
    "business-entrepreneurship": [("sales-representative", "Nhân viên kinh doanh", "khám phá nhu cầu, tư vấn giải pháp và duy trì quan hệ khách hàng")],
    "technology-engineering": [("automotive-engineer", "Kỹ sư ô tô", "thiết kế, chẩn đoán và kiểm tra hệ thống ô tô")],
}
ACTIVITIES = {
    "nha-khoi-nghiep": "phác thảo một ý tưởng kinh doanh và hỏi khách hàng về nhu cầu",
    "chuyen-vien-phat-trien-kinh-doanh": "lập kế hoạch tiếp cận khách hàng và xin phản hồi về cách đặt câu hỏi",
    "quan-ly-san-pham": "chọn một sản phẩm, xác định nhu cầu người dùng và xếp thứ tự cải tiến",
    "chuyen-vien-marketing": "viết hai thông điệp cho cùng một sản phẩm và thử với người đọc",
    "chuyen-vien-tai-chinh": "phân tích một bảng thu chi và giải thích các rủi ro bằng số liệu",
    "quan-ly-ban-hang": "thực hành tư vấn một nhu cầu mua hàng rồi xin phản hồi về cách lắng nghe",
    "chuyen-vien-thuong-mai-dien-tu": "thiết kế một gian hàng mẫu và so sánh hai cách giới thiệu sản phẩm",
    "thiet-ke-do-hoa": "thiết kế một poster theo yêu cầu cụ thể và chỉnh lại sau góp ý",
    "thiet-ke-ux-ui": "phỏng vấn người dùng về một màn hình và thử bản phác thảo mới",
    "kien-truc-su": "phác thảo một không gian với giới hạn diện tích và kiểm tra các ràng buộc",
    "thiet-ke-noi-that": "bố trí một căn phòng mẫu theo nhu cầu và ngân sách của người sử dụng",
    "thiet-ke-san-pham": "làm mẫu một vật dụng và ghi lại điều cần sửa sau khi thử dùng",
    "dao-dien-nghe-thuat": "xây dựng ý tưởng hình ảnh cho một chiến dịch và trình bày cho nhóm",
    "hoa-si-minh-hoa": "minh họa một câu chuyện ngắn và xin phản hồi về cách truyền đạt bằng hình ảnh",
    "ky-su-moi-truong": "phân tích một vấn đề chất thải và đề xuất quy trình xử lý có căn cứ",
    "chuyen-vien-phat-trien-ben-vung": "đánh giá tác động môi trường của một hoạt động và trao đổi với các bên liên quan",
    "ky-su-nang-luong-tai-tao": "so sánh hai giải pháp năng lượng theo chi phí và hiệu quả",
    "chuyen-vien-bao-ton": "tham gia một hoạt động bảo tồn và ghi lại dữ kiện quan sát được",
    "nghien-cuu-moi-truong": "đặt một câu hỏi môi trường và lập cách thu thập bằng chứng để kiểm tra",
    "chuyen-vien-esg": "đọc một báo cáo phát triển bền vững và kiểm tra căn cứ của các chỉ tiêu",
    "quan-ly-tai-nguyen": "lập phương án sử dụng một nguồn tài nguyên và giải thích các đánh đổi",
    "doctor": "học cách phân tích một tình huống y khoa cơ bản dưới hướng dẫn chuyên môn",
    "dieu-duong": "tìm hiểu quy trình chăm sóc và thực hành trao đổi nhu cầu trong tình huống mô phỏng",
    "duoc-si": "đọc tài liệu dược học nhập môn và luyện kiểm tra thông tin theo quy trình",
    "chuyen-gia-tam-ly": "tham gia bài tập lắng nghe có hướng dẫn và xin phản hồi về cách đặt câu hỏi",
    "chuyen-gia-dinh-duong": "phân tích một thực đơn mẫu và trình bày căn cứ từ tài liệu dinh dưỡng",
    "chuyen-vien-y-te-cong-cong": "đọc dữ liệu sức khỏe cộng đồng và đề xuất một hoạt động truyền thông",
    "ky-thuat-vien-xet-nghiem": "tìm hiểu quy trình xét nghiệm và luyện ghi nhận kết quả theo hướng dẫn",
    "lawyer": "đọc một vụ việc và viết lập luận liên kết từng kết luận với chứng cứ",
    "chuyen-vien-phap-che": "đọc một quy định và viết bản phân tích rủi ro cho tình huống cụ thể",
    "chuyen-vien-chinh-sach-cong": "so sánh hai phương án chính sách bằng dữ liệu và viết đề xuất ngắn",
    "chuyen-vien-tuan-thu": "kiểm tra một quy trình theo tiêu chuẩn và ghi rõ căn cứ của từng nhận định",
    "nha-bao": "viết một bản tin ngắn và kiểm tra từng thông tin với nguồn gốc",
    "chuyen-vien-quan-he-cong-chung": "viết phản hồi cho một tình huống truyền thông và xin góp ý về cách diễn đạt",
    "nha-sang-tao-noi-dung": "làm một nội dung ngắn rồi dùng phản hồi người xem để chỉnh sửa",
    "bien-tap-vien": "biên tập một bài viết và giải thích từng chỉnh sửa về thông tin hoặc cách diễn đạt",
    "chuyen-vien-truyen-thong-so": "lập một nội dung truyền thông và xác định cách đo phản hồi",
    "bien-kich": "viết một cảnh ngắn có nhân vật và xung đột rồi chỉnh lại sau phản hồi",
    "nha-san-xuat-truyen-thong": "lập kế hoạch sản xuất một nội dung với thời hạn và nguồn lực cụ thể",
    "chuyen-vien-logistics": "sắp xếp một lịch giao hàng và điều chỉnh khi có phát sinh",
    "chuyen-vien-chuoi-cung-ung": "phân tích luồng hàng và đề xuất cách giảm một điểm chậm",
    "quan-ly-van-hanh": "vẽ một quy trình làm việc và thử cải tiến một bước",
    "ky-thuat-vien-dien": "học một quy trình kiểm tra điện an toàn dưới hướng dẫn chuyên môn",
    "ky-thuat-vien-co-khi": "tìm hiểu một cơ cấu và ghi lại trình tự kiểm tra lỗi an toàn",
    "chuyen-vien-quan-ly-chat-luong": "kiểm tra một sản phẩm theo tiêu chuẩn và phân tích nguyên nhân sai lệch",
    "dieu-phoi-san-xuat": "lập lịch cho một nhóm công việc rồi điều chỉnh khi thiếu nguồn lực",
    "teacher": "giải thích một khái niệm cho người mới rồi điều chỉnh theo phản hồi",
    "chuyen-vien-dao-tao": "thiết kế một buổi học ngắn và kiểm tra điều người học hiểu được",
    "chuyen-vien-nhan-su": "thực hành một cuộc trao đổi về nhu cầu công việc và ghi lại cách hỗ trợ",
    "tu-van-huong-nghiep": "đọc một hồ sơ nghề mẫu và đặt câu hỏi giúp người khác tự tìm hiểu",
    "cong-tac-xa-hoi": "tham gia hoạt động cộng đồng và ghi nhận cách phối hợp nguồn hỗ trợ",
    "quan-ly-giao-duc": "lập kế hoạch một chương trình học và xác định cách đánh giá tiến độ",
    "giao-vien-giao-duc-dac-biet": "tìm hiểu cách điều chỉnh hướng dẫn cho nhu cầu học tập khác nhau",
    "chuyen-vien-phong-thi-nghiem": "luyện ghi chép đo lường và kiểm tra sai số theo hướng dẫn",
    "chuyen-vien-cong-nghe-sinh-hoc": "đọc một thí nghiệm sinh học và giải thích các bước kiểm soát",
    "nha-khoa-hoc-du-lieu": "thử một mô hình dữ liệu đơn giản và kiểm tra kết quả trên dữ liệu mới",
    "chuyen-vien-thong-ke": "phân tích một tập số liệu và diễn giải độ không chắc chắn",
    "nghien-cuu-thi-truong": "thiết kế một khảo sát nhỏ và giải thích kết quả theo mẫu quan sát",
    "nghien-cuu-y-sinh": "đọc một nghiên cứu y sinh và phân biệt kết quả với giả thuyết",
    "ky-su-phan-mem": "làm một chương trình nhỏ và giải thích cách kiểm tra lỗi",
    "chuyen-vien-phan-tich-du-lieu": "làm sạch một bảng dữ liệu và trình bày kết luận có căn cứ",
    "chuyen-gia-an-ninh-mang": "tìm hiểu một tình huống bảo mật trong môi trường thực hành được phép",
    "ky-su-ai": "thử một mô hình AI nhỏ và ghi lại cách đánh giá sai số",
    "ky-su-tu-dong-hoa": "mô phỏng một hệ thống tự động và kiểm tra phản ứng khi điều kiện thay đổi",
    "ky-su-dien-tu": "đọc sơ đồ mạch và học cách đo kiểm an toàn",
    "quan-tri-he-thong": "lập kế hoạch xử lý sự cố hệ thống trong môi trường thử nghiệm",
    "sales-representative": "thực hành tư vấn một sản phẩm và xin góp ý về cách khám phá nhu cầu",
    "automotive-engineer": "tìm hiểu cấu tạo một hệ thống ô tô và ghi lại cách kiểm tra lỗi an toàn",
}


def build_catalog():
    dimensions = [f"{stage}{i}" for stage, count in zip("DESMAP", [6, 6, 3, 3, 4, 6]) for i in range(1, count + 1)]
    aliases = {"bac-si": "doctor", "luat-su": "lawyer", "giao-vien": "teacher"}
    extract = json.loads((ROOT / "data/onet-extract.json").read_text(encoding="utf-8"))
    mapping = {career["id"]: career for career in json.loads((ROOT / "data/onet-career-mapping.json").read_text(encoding="utf-8"))["careers"]}
    careers = []
    for group, occupations in CAREERS_BY_INTEREST.items():
        for old_id, name, requirements in [*occupations, *EXTRA_CAREERS.get(group, [])]:
            career_id = aliases.get(old_id, old_id)
            occupations_data = [extract["occupations"][item["code"]] for item in mapping[career_id]["onetSoc"]]
            # Shape of the career relative to a typical O*NET occupation (z-score, averaged over codes).
            profile = {code: round(fmean(item["groups"][code] for item in occupations_data), 3) for code in dimensions[6:]}
            # Only above-average requirements raise a weight: 1 at or below average, 5 at +2 SD.
            weights = dict.fromkeys(dimensions[:6], 1)
            weights.update({code: round(min(5, 1 + 2 * max(0, value)), 2) for code, value in profile.items()})
            # Work Values Extent 1-7 rescaled to the 0-100 questionnaire range.
            targets = {code: round((fmean(item["workValues"][code] for item in occupations_data) - 1) / 6 * 100) for code in dimensions[:6]}
            careers.append({"id": career_id, "aliases": [old_id] if old_id in aliases else [],
                "name": name, "interestGroup": group, "description": requirements,
                "onetSoc": [item["code"] for item in mapping[career_id]["onetSoc"]],
                "weights": weights, "profile": profile, "desireTargets": targets,
                "vrWeights": {"information-processing": weights["M1"],
                    "problem-solving": max(weights["E2"], weights["E5"]), "decision-making": weights["M3"],
                    "adaptability": max(weights["A3"], weights["A4"]),
                    "pressure-response": max(weights["P1"], weights["P2"], weights["P3"]),
                    "social-interaction": max(weights["E3"], weights["S2"])},
                "activity": ACTIVITIES[career_id],
                "nameSlug": career_id if career_id in {*aliases.values(), "sales-representative", "automotive-engineer"} else ""})
    names = ["Thu nhập, phúc lợi, ổn định", "Học hỏi, phát triển, thử thách", "Tự chủ", "Ý nghĩa và đóng góp", "Công nhận và ảnh hưởng", "Điều kiện và cân bằng công việc",
             "Kỹ năng nền tảng", "Giải quyết vấn đề phức tạp", "Kỹ năng tương tác xã hội", "Kỹ năng kỹ thuật", "Kỹ năng hệ thống", "Quản lý nguồn lực",
             "Vai trò hướng nhiệm vụ", "Vai trò duy trì quan hệ", "Vai trò định hướng cá nhân", "Tư duy phân tích", "Tư duy sáng tạo", "Tư duy thực tiễn",
             "Chuẩn bị cho tương lai", "Chủ động và chịu trách nhiệm", "Khám phá khả năng mới", "Tự tin vượt qua khó khăn",
             "Áp lực thời gian và tốc độ", "Áp lực khối lượng công việc", "Áp lực tư duy và quyết định", "Áp lực cảm xúc", "Áp lực tương tác và xung đột", "Áp lực trách nhiệm và hậu quả"]
    # Matching follows the O*NET linking report: profile shape by correlation, Desire by distance
    # to the career's work values, and a bounded penalty for low scores on core requirements.
    return {"version": "desmap-careers-v4", "source": extract["attribution"], "onetVersion": extract["onetVersion"],
            "dimensionNames": dict(zip(dimensions, names, strict=True)), "desireShare": .15,
            "shortfallPenalty": {"threshold": 50, "factor": .3, "minProfile": 1},
            "maxVrShare": .25, "careers": careers}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-root", type=Path, required=True)
    args = parser.parse_args()
    catalog = build_catalog()
    for path in [ROOT / "data/career-catalog.json", args.web_root / "src/lib/assessment/career-catalog.json"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(catalog['careers'])} career profiles.")
