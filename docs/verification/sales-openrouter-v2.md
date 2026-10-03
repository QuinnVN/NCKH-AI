# Kiểm chứng hội thoại Sales v2

Pipeline v2 là Sherpa local, Jev qua OpenRouter, bộ luật backend, Qwen 3.7 Flash qua OpenRouter và Supertonic local. Bản triển khai giữ `SALES_PIPELINE_MODE=legacy` làm mặc định. Không coi kiểm thử với dịch vụ thay thế là kết quả đo model hoặc headset.

## Vận hành

Cấp `OPENROUTER_API_KEY` bằng environment của tiến trình NCKH-AI hoặc secret injection. Không sao chép key vào Unity, repository hoặc câu lệnh ghi log. Backend không tự đọc cấu hình NCKH-Web. `SALES_PIPELINE_MODE=shadow` chỉ chạy đối chiếu, còn `openrouter` tạo phiên mới theo v2. Phiên đang chạy và dữ liệu cũ giữ phiên bản đã lưu.

Nếu phiên đang chạy dùng tuple phiên bản mà backend hiện tại không hỗ trợ, backend báo lỗi kỹ thuật rõ ràng và giữ bằng chứng/checkpoint. Không dùng schema hoặc rubric mới dưới provenance cũ. Cần tiếp tục bằng backend tương thích hoặc tạo phiên mới; kết quả lịch sử đã hoàn tất vẫn giữ nguyên.

Mặc định thử nghiệm là tám lượt có thể đánh giá, deadline Jev ba giây và writer bốn giây. Điều chỉnh bằng `SALES_MAX_TURNS`, `SALES_JEV_TIMEOUT_SECONDS`, `SALES_WRITER_TIMEOUT_SECONDS` giữa các đợt thử; thay đổi rubric, câu hỏi hoặc ngưỡng phải tăng phiên bản tương ứng. Chạy một backend worker với file-backed store. Supertonic giữ deadline và cơ chế fallback bằng chữ hiện có.

Jev lỗi giữ checkpoint và cho retry cùng turn ID/audio. Writer lỗi dùng câu backend an toàn, không phạt người chơi. Điểm của rubric và ending chỉ commit một lần. Lời Lan không quyết định kết thúc. Lượt uncertain không dùng ngân sách đánh giá; sau hai lần làm rõ chưa giải quyết, kết quả cần duyệt. Một challenge ở lượt cuối được dành thêm một lượt đáp.

## Bộ kiểm thử tự động

Chạy `python -m unittest discover -s app/tests`. Các bộ `test_sales_openrouter`, `test_sales_pipeline_api`, `test_sales_evaluation` và `test_run_results` kiểm tra adapter, luật, API theo hình dạng Unity, retry, quyền terminal result và công cụ khảo sát. Giữ kiểm thử legacy để xác nhận rollback không đổi luật phiên cũ.

Trong NCKH-Web, chạy Vitest cho `src/lib/server/sales-result-contract.test.ts` và kiểm tra TypeScript. Trong NCKH-VR, dùng `Build.ps1 -Target Validate`, `EditModeTests`, `PlayModeTests` và `Android`. Báo compile, test và build riêng; log và số test thực tế được ghi trong báo cáo giao bản.

## Duyệt dữ liệu và đo chất lượng

`scripts/evaluate_sales_jev.py prepare --recordings <directory> --output <new-review.jsonl>` chuẩn bị hồ sơ duyệt. Nó tách transcript STT khỏi `transcriptCorrected`, để trống nhãn tham chiếu và thông tin người duyệt, không dùng nhãn hệ thống cũ làm gold. Đây là bản xuất diagnostics; giữ cùng chính sách retention và xóa bản xuất khi xóa diagnostics nguồn.

Người duyệt nghe audio, xác nhận nhóm người nói và phiên, gán nhãn độc lập, ghi reviewer/time và phân dữ liệu thành development, calibration, holdout. Các biến thể cùng phiên hoặc người nói phải cùng split. Tám câu hỏi trên 40 mẫu đã khảo sát chỉ là development. Cần khoảng 300–500 lượt được duyệt, thêm mục tiêu 3/4 và ít nhất 30–50 ví dụ dương cùng âm dễ nhầm cho mỗi nhãn ưu tiên.

`scripts/evaluate_sales_jev.py predict --records <review.jsonl> --transcript-field transcriptStt --limit 100 --output <new-predictions.jsonl>` gọi Jev bằng key backend. Lệnh này phát sinh chi phí mạng, giới hạn số request rõ ràng và không gửi nhãn gold hoặc thông tin người duyệt vào model. Chạy riêng với `transcriptCorrected` để tách lỗi STT khỏi lỗi nhận định. Lỗi dịch vụ được lưu với nhãn rỗng, không thành dự đoán âm. Không dùng lệnh này để thay đổi trạng thái game.

`scripts/evaluate_sales_jev.py score --references <review.jsonl> --predictions <predictions.jsonl> --split holdout --observations <observations.jsonl> --output <new-report.json>` báo precision/recall/F1 theo nhãn, exact match, tỷ lệ uncertain/được quyết định, phạt nhầm và ending sai có tham chiếu. JSON dự đoán gồm `caseId`, `questionSetVersion`, `thresholdVersion`, `labels` với Noul nguyên trạng. Thiếu dự đoán được báo uncertain. Công cụ từ chối split rò rỉ, nhãn chưa duyệt và trộn phiên bản.

Observations gồm thời gian riêng `sttSeconds`, `jevSeconds`, `writerSeconds`, `wavSeconds`, `endOfSpeechToPlaybackSeconds`, `usageCostUsd`, `fallbackUsed`, `retried`, `errorCode`. Duyệt writer bằng `writerReview` có `independent`, `reviewerId`, `passed`. Chi phí thiếu dữ liệu không được thay bằng 0.

`scripts/evaluate_sales_jev.py calibrate --references <review.jsonl> --predictions <predictions.jsonl> --output <new-curves.json>` chỉ dùng tập calibration để so các ngưỡng chấp nhận nhãn dương. Nó không đổi cấu hình production. Người duyệt chọn cả ngưỡng nhận và loại từng nhãn, tăng threshold version, rồi đo lại trên holdout chưa dùng để chọn ngưỡng. Evaluator từ chối phiên bản ngưỡng chưa biết thay vì âm thầm áp ngưỡng hiện tại.

Trước khi bật chấm chính thức, kiểm tra precision nhãn phạt ít nhất 98% trên phần được quyết định, báo số mẫu và khoảng bất định; không có ending sai trong tình huống bắt buộc. Người duyệt đọc ít nhất 50 lời Qwen, mục tiêu ít nhất 90% đạt về nội dung, vai và tiếng Việt. Đo ít nhất 100 lượt thật với p50 đến playback không quá 2,5 giây, p95 không quá 5 giây trên cấu hình đích. Đây là tiêu chí đề xuất, không phải thành tích đã đo.

## Kiểm tra headset còn cần thực hiện

Xác minh quyền và thiết bị micro, PCM không toàn 0, pause/resume, mất tracking, ranh giới từng đoạn thu, tiếng Lan lọt micro, dấu tiếng Việt, thao tác A ngắt playback và frame rate. VAD mặc định tắt; hiệu chỉnh mức âm và 800 ms im lặng trên giọng tiếng Việt/headset trước khi bật. Khi VAD tắt, không dùng transcript rỗng làm bằng chứng người chơi im lặng. Chỉ capture đã xác nhận hợp lệ và không có speech mới có thể tạo lượt im lặng thật.

Không có headset hoặc nhãn người duyệt trong phiên triển khai này. Chưa công bố precision thực, chất lượng lời Qwen, chi phí phiên, p50/p95 toàn pipeline hay chống vọng.
