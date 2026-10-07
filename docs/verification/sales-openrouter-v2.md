# Kiểm chứng hội thoại Sales v2

Pipeline v2 là Sherpa local, Jev qua OpenRouter, Luna phân xử chọn lọc qua OpenRouter, bộ luật backend, Qwen 3.7 Flash qua OpenRouter và Supertonic local. Bản triển khai giữ `SALES_PIPELINE_MODE=legacy` làm mặc định. Không coi kiểm thử với dịch vụ thay thế là kết quả đo model hoặc headset.

## Vận hành

Cấp `OPENROUTER_API_KEY` bằng environment của tiến trình NCKH-AI hoặc secret injection. Không sao chép key vào Unity, repository hoặc câu lệnh ghi log. Backend không tự đọc cấu hình NCKH-Web. `SALES_PIPELINE_MODE=shadow` chỉ chạy đối chiếu, còn `openrouter` tạo phiên mới theo v2. Phiên đang chạy và dữ liệu cũ giữ phiên bản đã lưu.

Nếu phiên đang chạy dùng tuple phiên bản mà backend hiện tại không hỗ trợ, backend báo lỗi kỹ thuật rõ ràng và giữ bằng chứng/checkpoint. Không dùng schema hoặc rubric mới dưới provenance cũ. Cần tiếp tục bằng backend tương thích hoặc tạo phiên mới; kết quả lịch sử đã hoàn tất vẫn giữ nguyên.

Mặc định tối đa tám lượt đã phân loại, kể cả uncertain. Tám là giới hạn trên; đủ mục tiêu hoặc có kết thúc bắt buộc thì dừng sớm. Deadline Jev ba giây, Luna sáu giây và writer bốn giây. Điều chỉnh bằng `SALES_MAX_TURNS`, `SALES_JEV_TIMEOUT_SECONDS`, `SALES_LUNA_TIMEOUT_SECONDS`, `SALES_WRITER_TIMEOUT_SECONDS` giữa các đợt thử; thay đổi rubric, câu hỏi hoặc ngưỡng phải tăng phiên bản tương ứng. Chạy một backend worker với file-backed store. Supertonic giữ deadline và cơ chế fallback bằng chữ hiện có.

Jev lỗi giữ checkpoint và cho retry cùng turn ID/audio. Writer lỗi dùng câu backend an toàn, không phạt người chơi. Điểm của rubric và ending chỉ commit một lần. Lời Lan không quyết định kết thúc. Sau ba lần bất định về cùng nhãn, đánh giá cần duyệt nhưng hội thoại tiếp tục trong giới hạn lượt. Không thêm lượt vượt upper limit để đáp challenge.

## Phân xử Jev và Luna

`sales-jev-v3` có 30 nhãn thay cho 44. Gộp apology/emotionalAcknowledgment thành acknowledgment; profanityOrInsult vào abuse; condescending vào disrespect; useOrDurationQuestion vào walkingQuestion; nhóm fit vào fitQuestion/preferenceQuestion; verificationStep vào fitOrWalkTrial. Bỏ nhãn mô tả không dùng trong quyết định và retraction chung. Bốn loại rút cam kết giữ riêng; một lời sửa chung chỉ xác định được loại nếu có đúng một cam kết đang mở. CauseStatement vẫn tách routineMatchExplanation vì một nhãn giải thích đôi cũ và một nhãn giải thích phương án mới.

`SALES_LUNA_ARBITRATION_ENABLED=true` bật cho phiên openrouter mới. Jev đánh giá trước. Selector lấy tối đa tám nhãn có ảnh hưởng rating, tiến trình, cam kết hoặc thông tin cần hỏi: nhãn quan trọng uncertain, đề xuất đổi đồng thời từ chối đổi, cam kết đồng thời rút lại, và disrespect=true dễ nhầm với phàn nàn NPC. Không gọi Luna cho appearanceQuestion uncertain hoặc bằng chứng mục tiêu đã có. repeatedQuestion uncertain cũng phải phân xử nếu có thể làm cộng sai điểm cho câu hỏi điều tra.

Luna `openai/gpt-6-luna` qua provider OpenAI trả JSON strict cho các nhãn được chọn. Không gửi audio, tên thật, run ID hoặc rating. Mỗi nhãn true cần trích đoạn khớp lời người chơi hiện tại; nhãn false có thể để bằng chứng rỗng khi hành vi vắng mặt. Nhãn sai cấu trúc hoặc quote không khớp bị loại riêng; extra keys, model sai, output chưa hoàn tất và lỗi dịch vụ không ghi đè Jev. Luna uncertain cũng không ghi đè Jev. Backend quyết định điểm và ending theo rubric v2.4.

Checkpoint giữ Jev trước khi gọi Luna và giữ classification đã phân xử trước khi tính candidate. Retry không gọi lại stage đã lưu và không tính điểm hai lần. `turnAssessment` giữ noul gốc, jevStatus, source=luna và arbitration evidence; `classifierMetadata.arbitration` ghi nhãn đã chọn/được nhận/bị loại, model, thời gian, usage và lỗi. Lỗi Luna giữ nguyên bằng chứng Jev, có thể cần duyệt nếu vẫn còn nhãn quan trọng uncertain.

Thay đổi này chỉ áp dụng phiên mới sau khi backend restart. Phiên cũ đã hoàn tất vẫn đọc được; không diễn giải session đang chạy với nhãn mới dưới provenance cũ. STT không thay đổi.

`playerResponseRating` trả trực tiếp good/bad/neutral/uncertain khi có bằng chứng phân loại dùng được. Bất định ngữ nghĩa là uncertain; null dành cho không có kết quả phân loại dùng được hoặc chưa phân loại, như lượt silent hay lỗi dịch vụ. Luna lỗi không xóa bất định Jev đã trả hợp lệ. Nếu có hành vi bad chắc chắn cùng nhãn khác uncertain, rating vẫn bad và assessmentUncertain vẫn true. Unity giữ nguyên chuỗi rating, không tự đổi neutral/uncertain thành null. Web chỉ kiểm tra customerRating của kết quả cuối, không dùng enum playerResponseRating để quyết định trust.

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

Xác minh quyền và thiết bị micro, PCM không toàn 0, pause/resume, mất tracking, ranh giới từng đoạn thu, tiếng Lan lọt micro, dấu tiếng Việt, thao tác A ngắt playback và frame rate. Controller mặc định VAD tắt; scene Store hiện bật VAD với silence 5 giây, ngưỡng bắt đầu RMS 0,015 và tiếp tục 0,0075. Khi VAD bật, A không nộp audio trong lúc Recording. Hiệu chỉnh mức âm trên headset thực. Khi VAD tắt, không dùng transcript rỗng làm bằng chứng người chơi im lặng. Chỉ capture đã xác nhận hợp lệ và không có speech mới có thể tạo lượt im lặng thật.

Lỗi bản ghi có thể thu lại trả retryRecording=true và giải phóng checkpoint mà không xóa diagnostics lượt lỗi. Unity mở bản ghi mới trong cùng session, không phát incomplete hoặc lỗi toàn phase. Xem sales-capture-personality-2026-10-04.md để biết các mã lỗi được xử lý và kết quả kiểm chứng.

Không có headset hoặc nhãn người duyệt trong phiên triển khai này. Chưa công bố precision thực, chất lượng lời Qwen, chi phí phiên, p50/p95 toàn pipeline hay chống vọng.
