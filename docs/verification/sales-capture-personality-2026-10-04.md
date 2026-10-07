# Thu âm VAD và tính cách khách hàng

## Run được kiểm tra

Run `bf0e0d9396454a56ae4e2e6fc88f4098`, session `3571746da4d643f79f817a10098471c0`, có bốn lượt được phân loại, một lượt speechDetected=false và một lượt transcription_failed. Backend vẫn active ở mục tiêu 2; lỗi không làm giảm ngân sách hội thoại nhưng để lại checkpoint chặn bản ghi mới. Unity đi vào nhánh error/incomplete dùng chung.

Audio lỗi dài 1,18 giây, có chín cửa sổ 20 ms trên ngưỡng RMS 0,015. Đoạn không có speech dài 2,6 giây, RMS cao nhất theo cửa sổ khoảng 0,0028. Scene lưu silence 0,8 giây. Ba lời NPC đầu lặp cấu trúc "Chị ghi nhận..." và câu hỏi tương tự; cả ba writerMetadata báo fallbackUsed=false. Model đang bám câu mẫu, không phải lỗi fallback kỹ thuật.

## Thay đổi

- Khi VAD bật, HandleAction(A) và SubmitEarly không nộp lúc đang Recording. A vẫn ngắt playback NPC hoặc xác nhận các màn hình khác. Khi VAD tắt, nộp thủ công giữ nguyên.
- Silence tăng lên 5 giây trong controller và scene Store. Ngưỡng bắt đầu lời vẫn 0,015; sau khi đã có lời dùng ngưỡng tiếp tục 0,0075 để giữ phần nói nhỏ. Không tự nộp chỉ vì chưa bắt đầu nói; giới hạn bản ghi 45 giây vẫn áp dụng.
- Backend trả retryRecording=true cho transcription_empty, transcription_failed, audio_read_failed và microphone_no_signal. Giữ diagnostics lượt lỗi, gỡ checkpoint của lượt đó, giữ session/mục tiêu/điểm/số lượt. Unity tạo turn ID mới và thu lại, phát sales.part2.capture_retry thay vì sales.part2.error hoặc sales.part2.incomplete. Lỗi cần retry cùng request như timeout/classifier lỗi vẫn giữ checkpoint và cơ chế retry cũ.
- Writer prompt lan-writer-v2.7 nhận customerDisposition do backend chọn. Lan dè chừng khi vấn đề chưa được giải quyết, bắt bẻ việc từ chối/đổ lỗi, và dịu hơn khi được hỏi đúng hoặc có bước xử lý phù hợp. Guard cấm "Chị ghi nhận/xác nhận", chặn lặp cùng câu hỏi dù đổi câu mở đầu, và yêu cầu trả lời lý do quay lại khi người chơi hỏi đúng. Fallback cũng đổi sang lời khách hàng tự nhiên; không hướng dẫn nhân viên phải đọc hay hỏi câu nào.

## Kiểm chứng

Ba Play Mode regression mới đã fail trên code trước sửa: A nộp trong VAD, transcription failure thành Incomplete, và silence mặc định 0,8 giây. Sau sửa: 8/8 Play Mode, 6/6 Edit Mode về thu âm/contract, 97/97 backend adapter/API pass. Console Unity không có lỗi sau kiểm tra. Bộ kiểm tra cũng giữ thao tác A ngắt NPC, speech giữa các render frame, stereo và flush cửa sổ cuối.

Writer replay cuối gồm bốn transcript của run và hai ca kiểm soát tổng hợp: hỏi nhu cầu đi bộ, từ chối đổi. Sáu câu đều qua guard, không fallback. Trung vị riêng writer khoảng 0,917 giây, tổng cost provider báo 0,00018936 USD cho đợt cuối. Dữ liệu ở recordings/evaluations/2026-10-04-capture-personality/writer-v2.7-final; không sửa recording nguồn hoặc điểm cũ. Đây là probe development, chưa phải đánh giá tính cách độc lập hay thử headset.

Ví dụ reply thực tế: "Chị mang đôi này bị đau nên mới quay lại. Em xem giúp chị vì sao lại như vậy nhé." Với câu hỏi đúng về đi bộ, Lan trả lời thông tin cần thiết thay vì tiếp tục bắt bẻ. Khi bị từ chối, Lan hỏi "Em từ chối đổi thì định xử lý vấn đề của chị thế nào?"

Restart backend, rebuild APK và bắt đầu run mới để dùng prompt/thu âm mới. Không chạy Android build hoặc retest headset trong đợt này. Các bản xuất replay là diagnostics, áp dụng cùng retention và quy trình xóa với nguồn. Cần xác nhận mức 0,0075 và khoảng lặng 5 giây trên mic/headset thực, đặc biệt khi phòng có tiếng ồn hoặc tiếng NPC lọt mic.
