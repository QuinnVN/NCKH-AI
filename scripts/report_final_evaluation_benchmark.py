"""Export aggregate benchmark results and deidentified per-eval CSV."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_final_evaluation import MODELS, summarize, read, save

NAMES = {
    MODELS[0]: 'DeepSeek V4.1 Flash',
    MODELS[1]: 'Qwen3.8 Flash',
    MODELS[2]: 'MiMo V2.6 Flash',
}


def export(source: Path, destination: Path) -> None:
    summary = summarize(source)
    manifest = read(source / 'manifest.json')
    paired = read(source / 'paired-summary.json')
    destination.mkdir(parents=True, exist_ok=True)
    save(destination / 'summary.json', {'manifest': manifest, 'summary': summary, 'paired': paired,
                                       'rubricVersion': '2026-10-04-observable-v2'})
    rows = []
    failures = Counter()
    for file in source.glob('runs/*/*/result.json'):
        row = read(file)
        rows.append(row)
        if not row['success']:
            failures[(row['model'], row['errorType'])] += 1
    config_file = source / 'run-config.json'
    config = read(config_file) if config_file.exists() else {}
    profile_count = len({row['caseId'] for row in rows})
    repeats = config.get('repeats', 1)
    parallel = config.get('concurrentEvals', 6)
    fields = ['caseId', 'model', 'repeat', 'success', 'seconds', 'costUsd', 'attemptCount',
              'findingCount', 'careerFallbacks', 'errorType', 'error']
    with (destination / 'per-eval.csv').open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda x: (x['caseId'], x['model'], x['repeat'])))
    def amount(value, decimals=5):
        return f'{value:.{decimals}f}' if value is not None else 'Không có'
    performance_rows, quality_rows, billing_rows, paired_rows = [], [], [], []
    for model in MODELS:
        item = summary[model]
        rate = item['firstAllObservableChecks']['percent']
        performance_rows.append(
            f"| {NAMES[model]} | {item['evalsCompleted']}/{item['evalsAttempted']} | "
            f"{amount(item['meanCompletedCostUsd'])} | {amount(item['costPerDeliveredEvalIncludingFailuresUsd'])} | "
            f"{amount(item['meanCompletedSeconds'], 1)} | {amount(item['medianCompletedSeconds'], 1)} | "
            f"{amount(item['p95CompletedSeconds'], 1)} | {amount(rate, 1)}% |")
        checks = item['firstResponseChecks']
        def percent(name):
            value = checks.get(name)
            return f"{value['percent']:.1f}% ({value['passed']}/{value['tested']})" if value else 'Không có'
        quality_rows.append(f"| {NAMES[model]} | {percent('twoQuestionnaireScores')} | "
                            f"{percent('vrCriterionLabelOrPhraseAndScore')} | {percent('separatesSources')} | "
                            f"{percent('onlyProvidedScores')} | {percent('noNumericScores')} | "
                            f"{percent('noInternalTerms')} |")
        tokens = item['tokens']
        cache = 100 * tokens.get('cached_tokens', 0) / tokens['prompt_tokens'] if tokens.get('prompt_tokens') else 0
        billing_rows.append(f"| {NAMES[model]} | {item['totalCostUsd']:.5f} | {tokens.get('prompt_tokens', 0):,} | "
                            f"{tokens.get('completion_tokens', 0):,} | {cache:.1f}% | "
                            f"{item['extraRequests']} | {item['careerFallbackPercent']:.1f}% |")
        pair = paired['models'][model]
        paired_rows.append(f"| {NAMES[model]} | {pair['profiles']} | {amount(pair['meanCostUsd'])} | "
                           f"{amount(pair['meanSeconds'], 1)} |")
    total_cost = sum(item['totalCostUsd'] for item in summary.values())
    missing_usage = sum(item['unpricedSuccessResponses'] for item in summary.values())
    failure_lines = [f"- {NAMES[m]}: {kind}, {count} hồ sơ." for (m, kind), count in sorted(failures.items())]
    diagnostic_file = source / 'dimension-name-diagnostic.json'
    diagnostic = read(diagnostic_file) if diagnostic_file.exists() else []
    diagnostic_cost = sum((call.get('usage') or {}).get('cost', 0) for result in diagnostic for call in result['calls'])
    review_file = source / 'manual-review.json'
    review = read(review_file) if review_file.exists() else {'notes': []}
    review_notes = '\n\n'.join(review['notes']) or 'Chưa có phần đọc mẫu.'
    diagnostic_notes_file = source / 'diagnostic-notes.json'
    diagnostic_notes = read(diagnostic_notes_file)['notes'] if diagnostic_notes_file.exists() else ''
    content = f'''# Benchmark final eval qua OpenRouter, 04/10/2026

Benchmark {profile_count} hồ sơ thật, mỗi hồ sơ {repeats} lần trên ba model, tổng {len(rows)} lần final eval. MongoDB có {manifest['sourceFinalDocuments']} hồ sơ cuối; một hồ sơ không đủ dữ liệu bảng hỏi/VR để chạy. Đầu vào chỉ giữ 28 điểm DESMAP, nhóm nghề quan tâm và các trường rubric cần cho tính toán. Không xuất tên, email, transcript hay dữ liệu nhận diện. Không ghi MongoDB.

## Chi phí và tốc độ thực tế

| Model | Hoàn thành | USD/eval hoàn thành | USD/kết quả hợp lệ, gồm lần thất bại | Trung bình giây | Trung vị giây | P95 giây | Qua kiểm tra prompt tự động, lượt đầu |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(performance_rows)}

"Hoàn thành" nghĩa là sinh đủ hồ sơ và vượt kiểm tra hợp đồng dữ liệu. Hồ sơ hoàn thành có thể chứa câu dự phòng do hệ thống thay nhận xét nghề. Chi phí một eval hoàn thành bao gồm retry và viết lại trong chính eval đó. Chi phí một kết quả hợp lệ còn cộng tiền của các eval thất bại rồi chia cho số kết quả hoàn thành. Tổng tiền {len(rows)} eval: **{total_cost:.5f} USD**.

Thời gian đo từ lúc bắt đầu sinh hồ sơ đến khi lắp ráp và kiểm tra xong, có tính retry, viết lại và lưu log. Không tính bước đọc MongoDB chuẩn bị bộ dữ liệu. Mỗi eval gọi model tuần tự theo trường; có tối đa {parallel} eval cùng chạy. Đây là tốc độ hoàn tất, không phải thời gian đến token đầu tiên.

## So sánh trên cùng những hồ sơ hoàn thành ở cả ba model

| Model | Số hồ sơ chung | USD/eval trung bình | Giây/eval trung bình |
| --- | ---: | ---: | ---: |
{chr(10).join(paired_rows)}

Các hồ sơ chung: {', '.join(paired['commonCompletedCaseIds']) or 'không có'}. Bảng này giảm sai lệch do model chỉ hoàn thành những hồ sơ dễ hơn. Số hồ sơ chung nhỏ nên chưa đủ để khái quát tốc độ hoặc chi phí cho mọi loại hồ sơ.

## Mức đáp ứng prompt

Đo câu model trả về trước khi hệ thống thay câu dự phòng. Dùng phản hồi đầu tiên của mỗi trường để thấy khả năng làm đúng ngay; dữ liệu JSON cũng có kết quả sau viết lại. Điểm tự động là tỷ lệ đoạn văn vượt tất cả các kiểm tra áp dụng cho trường đó, gồm định dạng, giới hạn ký tự theo prompt, không có mã nội bộ, không có cụm từ chê bai đã định nghĩa, dẫn chứng và phân biệt nguồn. Trường icon không được cộng vào tỷ lệ đoạn văn.

| Model | Hai yếu tố bảng hỏi và điểm | Tên tiêu chí VR và điểm | Phân biệt tự đánh giá/VR | Chỉ dùng điểm đã cung cấp | Không lặp điểm trong ô đối chiếu | Không có mã nội bộ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(quality_rows)}

"Tên tiêu chí VR và điểm" kiểm tra tên năng lực và giá trị điểm tương ứng xuất hiện, không chứng minh mọi diễn giải đều đúng. "Chỉ dùng điểm đã cung cấp" chấp nhận cả điểm của thành phần VR nằm trong chuỗi dẫn chứng, nhưng chưa phát hiện hết trường hợp gán đúng số cho sai yếu tố. Kiểm tra giọng góp ý hiện dùng danh sách cụm từ; chưa thay thế đánh giá ngữ nghĩa. Tỷ lệ tự động không phải độ chính xác hướng nghiệp hoặc điểm chấm bởi chuyên gia. Những đoạn chưa sinh do eval dừng sớm không nằm trong mẫu số của tỷ lệ đoạn văn; xem tỷ lệ hoàn thành để đánh giá khả năng cung cấp cả hồ sơ.

## Cache, token và các lượt bổ sung

| Model | Tổng USD | Token vào | Token ra | Token vào đọc cache | Lượt retry/viết lại bổ sung | Nhận xét nghề bị thay dự phòng |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(billing_rows)}

Chi phí lấy trực tiếp từ `usage.cost` của OpenRouter cho mọi phản hồi được tính phí, không lấy giá token thấp nhất trên trang catalog để suy ra chi phí. Dữ liệu có cache tự động và nhiều provider; tỷ lệ cache cao có thể làm chi phí lần đầu khác trung bình này. Không ép một provider cụ thể, không chuyển sang model khác. Mỗi model được yêu cầu tắt reasoning. Token, provider và mã generation được lưu theo từng request.

Có {missing_usage} phản hồi HTTP 200 không cung cấp `usage.cost`. Phản hồi lỗi này cũng thiếu completion và generation ID để truy vấn phí. Tổng chi phí trong bảng là phần được ghi nhận, chưa xác nhận phí của phản hồi thiếu usage. Xem `unpricedSuccessResponses` trong JSON; tên khóa đếm HTTP 200, không có nghĩa phản hồi đã sinh nội dung thành công.

OpenRouter mô tả cách ghi nhận chi phí trong [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting). Model được xác minh qua catalog ở thời điểm chuẩn bị: [DeepSeek V4.1 Flash](https://openrouter.ai/deepseek/deepseek-v4.1-flash), [Qwen3.8 Flash](https://openrouter.ai/qwen/qwen3.8-flash), [MiMo V2.6 Flash](https://openrouter.ai/xiaomi/mimo-v2.6-flash).

## Lỗi ảnh hưởng đến việc chọn model

{chr(10).join(failure_lines)}

Có ba vấn đề chung của dữ liệu và luồng xử lý cần tách khỏi năng lực model:

1. **Các yếu tố bảng hỏi thiếu tên và mô tả.** Nhiều facts chỉ có mã như `D3` và điểm. Các mẫu được đọc cho thấy model gán những ý nghĩa khác nhau cho cùng mã. Theo catalog dự án, D3 là tự chủ; một số câu lại diễn giải thành ổn định, cam kết hoặc tiến độ. Hoàn thành JSON chưa chứng minh các nhận xét này có căn cứ. Chưa thay bộ dữ liệu benchmark bằng tên được bổ sung.
2. **Bộ lọc nghề đòi chép nguyên chuỗi dẫn chứng.** Chuỗi VR chứa mã như `(doctor)` hoặc `(lawyer)`, trong khi prompt cấm đưa mã nội bộ vào nhận xét. Model có thể nêu đúng tên tiêu chí và điểm nhưng vẫn bị thay câu. Cần kiểm tra bằng chứng theo tên tiêu chí, giá trị và nguồn thay vì đòi chép nguyên văn. Tỷ lệ thay dự phòng ở trên đo hành vi của bộ lọc, không phải tỷ lệ mọi câu model viết sai.
3. **Headline có hai giới hạn khác nhau.** Prompt cho phép 650 ký tự, nhưng hợp đồng `FinalAssessment` chỉ nhận 300. `ValidationError` tại headline có thể xuất hiện dù model làm đúng giới hạn được gửi. Benchmark giữ nguyên luồng để đo hành vi hiện tại; chưa sửa giới hạn giữa chừng.

## Thử riêng việc bổ sung tên yếu tố

Sau khi hoàn tất đo tốc độ 60 eval, thử thêm tên chuẩn từ catalog cho nhóm D và E ở ba hồ sơ cố định `profile-02`, `profile-03`, `profile-04`. Giữ nguyên điểm, instruction, system prompt, temperature, top-p và giới hạn. Phản hồi gốc lấy từ log có sẵn; chỉ trả phí cho phía đã bổ sung tên. Có {len(diagnostic)} trường được thử, {sum(r['success'] for r in diagnostic)} trường vượt kiểm tra định dạng sau thay tên, chi phí bổ sung {diagnostic_cost:.5f} USD. Thử này chỉ kiểm tra ảnh hưởng của tên yếu tố; không tính vào giá hay tốc độ full eval ở các bảng trên.

{diagnostic_notes}

## Đọc mẫu về nội dung

{review_notes}

Đây là đọc mẫu bởi Codex, chưa phải chấm bởi chuyên gia hướng nghiệp. Đọc câu gốc, không dùng câu dự phòng để đánh giá khả năng viết của model.

## Phương pháp và giới hạn

- Dùng trực tiếp `generate_assessment` trong công cụ production, giữ cùng prompt theo trường và cơ chế viết lại/dự phòng. Temperature 0.2, top-p 0.9; lượt sửa dùng 0 và 1 như production. Giới hạn 1.024 token, timeout 45 giây/request, tối đa một retry cho lỗi tạm thời.
- Các facts ban đầu giống nhau giữa ba model. `previousDescriptions`, `previousRemedies` và một số summary phụ thuộc câu model vừa viết, đúng như luồng thực tế; các lượt sau không có prompt byte-identical. Thứ tự model được xoay theo hồ sơ để giảm lệch theo thời điểm.
- Tính điểm nghề và chọn phát hiện thực hiện bằng Python giống nhau cho cả ba model. Đây là benchmark bộ viết nhận xét, không xác minh trọng số nghề hoặc khả năng dự báo nghề nghiệp.
- Mỗi hồ sơ/model chỉ chạy một lần. Số mẫu 20, cùng một thời điểm và key OpenRouter. Chưa có đánh giá chuyên gia độc lập; phần đọc mẫu là kiểm tra của Codex. Routing provider, cache và tải mạng có thể thay đổi kết quả lần chạy khác.
- Không sửa model production, prompt production, kết quả cũ hay công thức tính điểm trong benchmark.

## Chạy lại và dữ liệu

```powershell
.\\.venv\\Scripts\\python.exe scripts/benchmark_final_evaluation.py prepare --output recordings/final-eval-benchmark-new
.\\.venv\\Scripts\\python.exe scripts/benchmark_final_evaluation.py run --output recordings/final-eval-benchmark-new --parallel 6
.\\.venv\\Scripts\\python.exe scripts/benchmark_final_evaluation.py diagnose-names --output recordings/final-eval-benchmark-new
.\\.venv\\Scripts\\python.exe scripts/report_final_evaluation_benchmark.py --source recordings/final-eval-benchmark-new --output docs/verification/final-eval-benchmark-new
```

Thư mục `recordings/final-eval-benchmark-2026-10-04` giữ đầu vào đã bỏ thông tin nhận diện, manifest/hash, log request, câu trả lời gốc, hồ sơ lắp ráp và thử bổ sung tên. Thư mục recordings được Git bỏ qua. [summary.json](summary.json) chứa số tổng hợp và hash; [per-eval.csv](per-eval.csv) chứa thời gian, chi phí và lỗi theo mã hồ sơ. Rerun trong cùng thư mục sẽ tái sử dụng `result.json`; dùng thư mục mới để đo lại.
'''
    (destination / 'report.md').write_text(content, encoding='utf-8')
    print(json.dumps({'report': str(destination / 'report.md'), 'fullEvalCostUsd': total_cost,
                      'diagnosticCostUsd': diagnostic_cost, 'rows': len(rows)}))


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export(args.source, args.output)
