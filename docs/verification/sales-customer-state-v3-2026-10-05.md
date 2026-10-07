# Lan phản ứng theo customer concern, chấm điểm từ evidence ledger (v3)

Ngày 05/10/2026. Triển khai theo spec [.scratch/sales-customer-state-dialogue/spec.md](../../.scratch/sales-customer-state-dialogue/spec.md). Session mới dùng sales-openrouter-v3, sales-rubric-v3, lan-writer-v3; scenario, bộ câu hỏi Jev và ngưỡng giữ nguyên. Chưa kiểm tra trên headset.

## Kiểm thử tự động

161 test Sales pass: test HTTP của pipeline v2/v3, adapter OpenRouter, arbitration, run results, sales evaluation và file mới app/tests/test_sales_turn_pipeline.py. File mới gọi thẳng process_turn và complete trên store tạm, thay STT/Jev/Qwen bằng bản giả có kịch bản. Nó kiểm tra:

- Hành vi theo user story: trả lời câu hỏi dữ kiện ngay ở mục tiêu 1, hint level 1 → 2 rồi stalemate không đòi quản lý, kỹ năng nói sớm vẫn được tính, objective hiển thị đuổi kịp từng bậc, cause/giải pháp chỉ tính sau khi đủ dữ kiện, "chị đã trả lời rồi" chỉ khi mọi dữ kiện được hỏi đã biết, lượt xấu không tiết lộ dữ kiện.
- Fixture hồi quy từ nhãn đã lưu của ba session (chỉ trạng thái nhãn, không chứa transcript).
- Sáu bất biến trên 30 chuỗi nhãn sinh theo seed cố định, với writer lúc trả fallback, lúc lặp câu cũ, lúc lỗi: không câu nào lặp ba câu gần nhất; câu hỏi dữ kiện chưa biết trong lượt không xấu luôn được trả lời; activeObjective luôn thỏa luật của Unity; ba lượt liên tiếp không tiến triển trên cùng concern thì đã kết thúc; thêm nhãn tốt không làm giảm điểm của ledger đã phân giải; outcome khi complete bằng outcome chấm lại từ cùng ledger.
- Mọi câu backend viết sẵn đều qua guard, kể cả khi ghép sau câu trả lời hai dữ kiện; mỗi nhóm có kiểm tra từ khóa có ít nhất bốn câu.
- Lệnh chấm lại offline cho kết quả trùng complete, đánh dấu session phiên bản cũ là giả định, bỏ qua session legacy kèm lý do và không đổi byte nào của file session.

10 test HTTP và 6 test adapter/arbitration cũ khẳng định hành vi khóa theo mục tiêu đã được chuyển sang ngữ nghĩa v3 (ví dụ không còn đòi Lan nhắc lại lời đề nghị đổi bằng từ khóa).

Toàn bộ 404 test của backend còn lỗi ở test pipeline legacy, test_main (set_game) và test_llm_service. Các lỗi này đến từ .env trên máy (SALES_PIPELINE_MODE=openrouter, LLM_BASE_URL) và responder legacy trả responder_invalid; khi đặt SALES_PIPELINE_MODE=legacy, các lỗi do chế độ openrouter biến mất. Thay đổi này không chạm vào các vùng đó.

## Phát lại nhãn của ba session

| Session | v2 khi chạy thật | v3 khi phát lại qua turn pipeline |
| --- | --- | --- |
| 1c6d… | 8 lượt ở mục tiêu 1, Lan không trả lời câu hỏi chỗ đau, 0 điểm, lost | Lan nêu concern thiếu thấu hiểu ở bậc 1 rồi bậc 2, kết thúc stalemate ở lượt 5, còn 3 lượt; policy và fit_question được tính (20 điểm), lost |
| 6de0… | 2 lượt, không tiết lộ dữ kiện | Lượt 1 trả lời pain_location, lượt 2 trả lời fit_condition |
| 25bf… | exchange_accepted ở lượt 4 dưới v2.5 | Vẫn exchange_accepted ở lượt 4, còn 4 lượt; 20 điểm, partially_restored, causeIdentification=false |

Câu hỏi chỗ đau đầu tiên của 1c6d nằm ở lượt 8, sau điểm v3 đã kết thúc, nên kỳ vọng "lượt đầu có painLocationQuestion trả về pain_location" được kiểm ở 6de0 và 25bf.

## Chấm lại recordings hiện có

Lệnh python scripts/rescore_sales_sessions.py --rubric sales-rubric-v3 ghi báo cáo vào recordings/evaluations/rescore-sales-rubric-v3-20261005T160526662985Z/report.json. 7 session có ledger được chấm, tất cả là phép thử giả định vì sinh ra dưới v2.x; 61 session legacy và 4 session còn chờ review bị bỏ qua kèm lý do. Ledger v2 không lưu dữ kiện đã tiết lộ ở từng lượt nên báo cáo suy ra từ nhãn và ghi chú điều đó.

| Session | Rubric cũ | Kết quả đã lưu | v3 giả định |
| --- | --- | --- | --- |
| 1c6d… | v2.5 | 0, lost, turn_limit | 20, lost, stalemate ở lượt 6 |
| 25bf… | v2.4 | 20, lost, turn_limit | 20, partially_restored, exchange_accepted ở lượt 4 |
| 379f… | v2 | 0, lost, turn_limit | 20, lost, stalemate ở lượt 6 |
| 3571…, 4d3a…, 6de0…, e2e6… | v2–v2.5 | chưa hoàn tất | 0–20, lost |

1c6d dừng ở lượt 6 khi chấm lại nhưng ở lượt 5 khi phát lại qua pipeline, vì factsKnownBefore lưu trong ledger v2 khác với dữ kiện v3 sẽ tiết lộ. Đây là giới hạn của phép thử giả định, không phải sai lệch giữa complete và chấm lại.

## Writer Qwen thật

Hai lượt chạy, mỗi lượt 12 request qua OpenRouter với các kế hoạch v3 (nêu concern ở ba bậc, trả lời dữ kiện kèm concern, chất vấn niềm tin, stalemate, chấp nhận đổi). Tổng chi phí provider báo là 0,000377 và 0,000466 USD; thời gian writer 0,76–3,53 giây mỗi lượt.

Lượt chạy đầu có 10/12 câu dùng nguyên, nhưng một câu nói "Chị đã nói rồi" khi Lan chưa kể dữ kiện nào. Guard mới false_already_answered chặn kiểu câu này khi chưa có dữ kiện được kể và hành động không phải câu hỏi lặp, prompt cũng được bổ sung. Lượt chạy thứ hai có 7/12 câu dùng nguyên; 5 câu rơi về fallback đã kiểm tra vì lặp câu gần đây (2), nói sai đã trả lời (2) và thiếu nội dung kết thúc (1). Qwen thường chép gần nguyên fallbackText. Đây là mẫu nhỏ, chưa phải số đo tỉ lệ fallback.

## Unity và Web

Unity báo invalid_phase_progression khi activeObjective giảm, tăng hơn một bậc hoặc tăng mà objectiveCompleted=false; luật hiển thị v3 không vi phạm điều nào (bất biến 3 ở trên). Completion reason giới hạn 32 ký tự, "stalemate" có 9. Hợp đồng kết quả của Web nhận endingReason là chuỗi bất kỳ, và bộ kiểm tra nhất quán chỉ áp dụng cho sales-rubric-v2 đến v2.4. Không sửa code Unity hay Web.

## Còn lại

- Chưa chạy trên headset với người chơi thật.
- Người duyệt nên đọc các câu bậc 2 trong app/sales_customer_lines.py và vài câu Qwen viết trước khi dùng với người tham gia, vì bậc 2 dễ trượt thành hướng dẫn.
- Các ngưỡng hint (ba bậc) và cách hiển thị objective là đề xuất của spec, chưa được kiểm chứng với người chơi.
