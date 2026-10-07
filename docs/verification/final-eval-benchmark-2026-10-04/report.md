# Benchmark final eval qua OpenRouter, 04/10/2026

Benchmark 20 hồ sơ thật, mỗi hồ sơ 1 lần trên ba model, tổng 60 lần final eval. MongoDB có 21 hồ sơ cuối; một hồ sơ không đủ dữ liệu bảng hỏi/VR để chạy. Đầu vào chỉ giữ 28 điểm DESMAP, nhóm nghề quan tâm và các trường rubric cần cho tính toán. Không xuất tên, email, transcript hay dữ liệu nhận diện. Không ghi MongoDB.

## Chi phí và tốc độ thực tế

| Model | Hoàn thành | USD/eval hoàn thành | USD/kết quả hợp lệ, gồm lần thất bại | Trung bình giây | Trung vị giây | P95 giây | Qua kiểm tra prompt tự động, lượt đầu |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| DeepSeek V4.1 Flash | 19/20 | 0.00890 | 0.00955 | 74.1 | 76.9 | 90.0 | 93.6% |
| Qwen3.8 Flash | 6/20 | 0.00489 | 0.01465 | 132.0 | 124.9 | 151.9 | 87.3% |
| MiMo V2.6 Flash | 18/20 | 0.00577 | 0.00607 | 231.6 | 220.5 | 330.3 | 60.3% |

"Hoàn thành" nghĩa là sinh đủ hồ sơ và vượt kiểm tra hợp đồng dữ liệu. Hồ sơ hoàn thành có thể chứa câu dự phòng do hệ thống thay nhận xét nghề. Chi phí một eval hoàn thành bao gồm retry và viết lại trong chính eval đó. Chi phí một kết quả hợp lệ còn cộng tiền của các eval thất bại rồi chia cho số kết quả hoàn thành. Tổng tiền 60 eval: **0.37862 USD**.

Thời gian đo từ lúc bắt đầu sinh hồ sơ đến khi lắp ráp và kiểm tra xong, có tính retry, viết lại và lưu log. Không tính bước đọc MongoDB chuẩn bị bộ dữ liệu. Mỗi eval gọi model tuần tự theo trường; có tối đa 6 eval cùng chạy. Đây là tốc độ hoàn tất, không phải thời gian đến token đầu tiên.

## So sánh trên cùng những hồ sơ hoàn thành ở cả ba model

| Model | Số hồ sơ chung | USD/eval trung bình | Giây/eval trung bình |
| --- | ---: | ---: | ---: |
| DeepSeek V4.1 Flash | 5 | 0.00869 | 72.8 |
| Qwen3.8 Flash | 5 | 0.00495 | 133.7 |
| MiMo V2.6 Flash | 5 | 0.00567 | 206.3 |

Các hồ sơ chung: profile-03, profile-10, profile-13, profile-14, profile-19. Bảng này giảm sai lệch do model chỉ hoàn thành những hồ sơ dễ hơn. Số hồ sơ chung nhỏ nên chưa đủ để khái quát tốc độ hoặc chi phí cho mọi loại hồ sơ.

## Mức đáp ứng prompt

Đo câu model trả về trước khi hệ thống thay câu dự phòng. Dùng phản hồi đầu tiên của mỗi trường để thấy khả năng làm đúng ngay; dữ liệu JSON cũng có kết quả sau viết lại. Điểm tự động là tỷ lệ đoạn văn vượt tất cả các kiểm tra áp dụng cho trường đó, gồm định dạng, giới hạn ký tự theo prompt, không có mã nội bộ, không có cụm từ chê bai đã định nghĩa, dẫn chứng và phân biệt nguồn. Trường icon không được cộng vào tỷ lệ đoạn văn.

| Model | Hai yếu tố bảng hỏi và điểm | Tên tiêu chí VR và điểm | Phân biệt tự đánh giá/VR | Chỉ dùng điểm đã cung cấp | Không lặp điểm trong ô đối chiếu | Không có mã nội bộ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DeepSeek V4.1 Flash | 97.9% (137/140) | 99.3% (139/140) | 100.0% (140/140) | 100.0% (140/140) | 90.9% (289/318) | 98.3% (686/698) |
| Qwen3.8 Flash | 96.0% (120/125) | 96.0% (120/125) | 98.4% (123/125) | 100.0% (125/125) | 89.8% (273/304) | 98.5% (604/613) |
| MiMo V2.6 Flash | 98.5% (131/133) | 98.5% (131/133) | 96.2% (128/133) | 100.0% (133/133) | 42.6% (129/303) | 88.4% (586/663) |

"Tên tiêu chí VR và điểm" kiểm tra tên năng lực và giá trị điểm tương ứng xuất hiện, không chứng minh mọi diễn giải đều đúng. "Chỉ dùng điểm đã cung cấp" chấp nhận cả điểm của thành phần VR nằm trong chuỗi dẫn chứng, nhưng chưa phát hiện hết trường hợp gán đúng số cho sai yếu tố. Kiểm tra giọng góp ý hiện dùng danh sách cụm từ; chưa thay thế đánh giá ngữ nghĩa. Tỷ lệ tự động không phải độ chính xác hướng nghiệp hoặc điểm chấm bởi chuyên gia. Những đoạn chưa sinh do eval dừng sớm không nằm trong mẫu số của tỷ lệ đoạn văn; xem tỷ lệ hoàn thành để đánh giá khả năng cung cấp cả hồ sơ.

## Cache, token và các lượt bổ sung

| Model | Tổng USD | Token vào | Token ra | Token vào đọc cache | Lượt retry/viết lại bổ sung | Nhận xét nghề bị thay dự phòng |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DeepSeek V4.1 Flash | 0.18143 | 2,650,813 | 95,248 | 81.6% | 0 | 92.5% |
| Qwen3.8 Flash | 0.08789 | 1,666,005 | 62,864 | 85.8% | 28 | 100.0% |
| MiMo V2.6 Flash | 0.10930 | 1,809,898 | 61,099 | 75.8% | 5 | 97.6% |

Chi phí lấy trực tiếp từ `usage.cost` của OpenRouter cho mọi phản hồi được tính phí, không lấy giá token thấp nhất trên trang catalog để suy ra chi phí. Dữ liệu có cache tự động và nhiều provider; tỷ lệ cache cao có thể làm chi phí lần đầu khác trung bình này. Không ép một provider cụ thể, không chuyển sang model khác. Mỗi model được yêu cầu tắt reasoning. Token, provider và mã generation được lưu theo từng request.

Có 1 phản hồi HTTP 200 không cung cấp `usage.cost`. Phản hồi lỗi này cũng thiếu completion và generation ID để truy vấn phí. Tổng chi phí trong bảng là phần được ghi nhận, chưa xác nhận phí của phản hồi thiếu usage. Xem `unpricedSuccessResponses` trong JSON; tên khóa đếm HTTP 200, không có nghĩa phản hồi đã sinh nội dung thành công.

OpenRouter mô tả cách ghi nhận chi phí trong [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting). Model được xác minh qua catalog ở thời điểm chuẩn bị: [DeepSeek V4.1 Flash](https://openrouter.ai/deepseek/deepseek-v4.1-flash), [Qwen3.8 Flash](https://openrouter.ai/qwen/qwen3.8-flash), [MiMo V2.6 Flash](https://openrouter.ai/xiaomi/mimo-v2.6-flash).

## Lỗi ảnh hưởng đến việc chọn model

- DeepSeek V4.1 Flash: ValidationError, 1 hồ sơ.
- Qwen3.8 Flash: FinalEvaluationOutputError, 10 hồ sơ.
- Qwen3.8 Flash: ValidationError, 4 hồ sơ.
- MiMo V2.6 Flash: FinalEvaluationOutputError, 1 hồ sơ.
- MiMo V2.6 Flash: FinalEvaluationProviderError, 1 hồ sơ.

Có ba vấn đề chung của dữ liệu và luồng xử lý cần tách khỏi năng lực model:

1. **Các yếu tố bảng hỏi thiếu tên và mô tả.** Nhiều facts chỉ có mã như `D3` và điểm. Các mẫu được đọc cho thấy model gán những ý nghĩa khác nhau cho cùng mã. Theo catalog dự án, D3 là tự chủ; một số câu lại diễn giải thành ổn định, cam kết hoặc tiến độ. Hoàn thành JSON chưa chứng minh các nhận xét này có căn cứ. Chưa thay bộ dữ liệu benchmark bằng tên được bổ sung.
2. **Bộ lọc nghề đòi chép nguyên chuỗi dẫn chứng.** Chuỗi VR chứa mã như `(doctor)` hoặc `(lawyer)`, trong khi prompt cấm đưa mã nội bộ vào nhận xét. Model có thể nêu đúng tên tiêu chí và điểm nhưng vẫn bị thay câu. Cần kiểm tra bằng chứng theo tên tiêu chí, giá trị và nguồn thay vì đòi chép nguyên văn. Tỷ lệ thay dự phòng ở trên đo hành vi của bộ lọc, không phải tỷ lệ mọi câu model viết sai.
3. **Headline có hai giới hạn khác nhau.** Prompt cho phép 650 ký tự, nhưng hợp đồng `FinalAssessment` chỉ nhận 300. `ValidationError` tại headline có thể xuất hiện dù model làm đúng giới hạn được gửi. Benchmark giữ nguyên luồng để đo hành vi hiện tại; chưa sửa giới hạn giữa chừng.

## Thử riêng việc bổ sung tên yếu tố

Sau khi hoàn tất đo tốc độ 60 eval, thử thêm tên chuẩn từ catalog cho nhóm D và E ở ba hồ sơ cố định `profile-02`, `profile-03`, `profile-04`. Giữ nguyên điểm, instruction, system prompt, temperature, top-p và giới hạn. Phản hồi gốc lấy từ log có sẵn; chỉ trả phí cho phía đã bổ sung tên. Có 18 trường được thử, 18 trường vượt kiểm tra định dạng sau thay tên, chi phí bổ sung 0.00226 USD. Thử này chỉ kiểm tra ảnh hưởng của tên yếu tố; không tính vào giá hay tốc độ full eval ở các bảng trên.

Toàn bộ 560 yếu tố trong 20 hồ sơ chỉ có mã làm tên và mô tả trống. Sau khi thêm tên, nhóm D ở profile-03 được cả ba model diễn giải đúng các yếu tố cao nhất là tự chủ và công nhận/ảnh hưởng, thay cho các diễn giải khác nhau trước đó. Tuy vậy, 18/18 hợp lệ về định dạng không có nghĩa 18/18 đúng nội dung: ở nhóm E của profile-02, Qwen và MiMo coi điểm kỹ năng hệ thống 0 là chưa được ghi nhận, mặc dù facts có số 0; ở nhóm D của profile-04, MiMo vẫn nói quan tâm đến ý nghĩa/đóng góp hơn công nhận, trong khi D4=25 và D5=50. Bổ sung tên giúp giảm thiếu dữ kiện nhưng vẫn cần kiểm tra ngữ nghĩa.

## Đọc mẫu về nội dung

DeepSeek thường nói rõ đây là tự đánh giá và liên hệ kết quả tiêu chí VR với nghề ở mức gợi ý. Các mô tả nghề có hoạt động thử cụ thể. Tuy nhiên, ở nhóm D vẫn có đoạn diễn giải sai ý nghĩa mã khi facts thiếu tên; ví dụ profile-03 nói công việc có mục đích rõ ràng và cơ hội học hỏi, trong khi các yếu tố cao nhất là D3 và D5, không phải D4 hay D2.

Qwen viết mạch lạc nhưng có đoạn khẳng định mạnh hơn dữ liệu, như từ tự đánh giá kỹ năng kỹ thuật 75/100 suy ra vận dụng thành thạo. Ở profile-03, Qwen suy ra ưu tiên hoàn thành mục tiêu hơn quan hệ từ điểm vai trò hướng nhiệm vụ mà đoạn facts không cung cấp điểm vai trò quan hệ để so sánh. Giới hạn độ dài cũng gây dừng nhiều eval, xem log theo hồ sơ.

MiMo có câu gọn và hoạt động thử nghề rõ. Tuy vậy, mô tả Chuyên viên phát triển kinh doanh ở profile-04 gán điểm xử lý thông tin 89/100 cho riêng trải nghiệm Nhân viên bán hàng. Facts thực tế ghi 89 là điểm tổng hợp, với Bác sĩ 78/100 và Nhân viên bán hàng 100/100. Đây là lỗi gán nguồn dù giá trị điểm và tên năng lực đều có trong facts; bộ kiểm tra số tự động không phát hiện được lỗi này.

Cả ba model đều có đoạn nhóm D không có căn cứ đủ rõ vì facts chỉ đưa mã và điểm. Phải bổ sung tên yếu tố trước khi xem tỷ lệ hoàn thành hoặc kiểm tra định dạng là bằng chứng cho chất lượng nội dung. Mẫu đọc chưa đủ để ước lượng tỷ lệ suy diễn sai trên toàn bộ bộ dữ liệu.

Đây là đọc mẫu bởi Codex, chưa phải chấm bởi chuyên gia hướng nghiệp. Đọc câu gốc, không dùng câu dự phòng để đánh giá khả năng viết của model.

## Phương pháp và giới hạn

- Dùng trực tiếp `generate_assessment` trong công cụ production, giữ cùng prompt theo trường và cơ chế viết lại/dự phòng. Temperature 0.2, top-p 0.9; lượt sửa dùng 0 và 1 như production. Giới hạn 1.024 token, timeout 45 giây/request, tối đa một retry cho lỗi tạm thời.
- Các facts ban đầu giống nhau giữa ba model. `previousDescriptions`, `previousRemedies` và một số summary phụ thuộc câu model vừa viết, đúng như luồng thực tế; các lượt sau không có prompt byte-identical. Thứ tự model được xoay theo hồ sơ để giảm lệch theo thời điểm.
- Tính điểm nghề và chọn phát hiện thực hiện bằng Python giống nhau cho cả ba model. Đây là benchmark bộ viết nhận xét, không xác minh trọng số nghề hoặc khả năng dự báo nghề nghiệp.
- Mỗi hồ sơ/model chỉ chạy một lần. Số mẫu 20, cùng một thời điểm và key OpenRouter. Chưa có đánh giá chuyên gia độc lập; phần đọc mẫu là kiểm tra của Codex. Routing provider, cache và tải mạng có thể thay đổi kết quả lần chạy khác.
- Không sửa model production, prompt production, kết quả cũ hay công thức tính điểm trong benchmark.

## Chạy lại và dữ liệu

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_final_evaluation.py prepare --output recordings/final-eval-benchmark-new
.\.venv\Scripts\python.exe scripts/benchmark_final_evaluation.py run --output recordings/final-eval-benchmark-new --parallel 6
.\.venv\Scripts\python.exe scripts/benchmark_final_evaluation.py diagnose-names --output recordings/final-eval-benchmark-new
.\.venv\Scripts\python.exe scripts/report_final_evaluation_benchmark.py --source recordings/final-eval-benchmark-new --output docs/verification/final-eval-benchmark-new
```

Thư mục `recordings/final-eval-benchmark-2026-10-04` giữ đầu vào đã bỏ thông tin nhận diện, manifest/hash, log request, câu trả lời gốc, hồ sơ lắp ráp và thử bổ sung tên. Thư mục recordings được Git bỏ qua. [summary.json](summary.json) chứa số tổng hợp và hash; [per-eval.csv](per-eval.csv) chứa thời gian, chi phí và lỗi theo mã hồ sơ. Rerun trong cùng thư mục sẽ tái sử dụng `result.json`; dùng thư mục mới để đo lại.
