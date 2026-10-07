# Run có transcript nhưng kết quả cần xem xét

Run `1df91a8593bf4f5587ee14182c4c4c71`, session `10006f6840794f84ad8a83ce87146fa7`, cập nhật cuối lúc 15:15:11 ngày 04/10/2026 theo giờ Việt Nam. Nguồn là `recordings/sales-session-10006f6840794f84ad8a83ce87146fa7.json`. Có ba lượt, tất cả có transcript và customerText, rating lần lượt good, bad, bad. Prompt lan-writer-v2.7 đang được dùng.

## Nguyên nhân xác nhận

Lượt 2, `05bfd6754cc84d8d9ca9dfb0eea43a12`, có Jev noul=0.42 cho beggingWithoutExplanation, nên nhãn là uncertain. Luna được gọi cho absoluteGuarantee và beggingWithoutExplanation, trả về bình thường với finishReason=stop. Adapter nhận absoluteGuarantee=true nhưng từ chối beggingWithoutExplanation. Nhãn thứ hai vì thế vẫn uncertain.

Metadata chỉ lưu rejectedLabels, không lưu decision bị từ chối hay lý do cụ thể. Code có thể từ chối vì shape/status/evidence sai, quote không nằm trong transcript hoặc true nhưng thiếu quote. Không có dữ liệu để xác định chính xác trường hợp nào đã xảy ra trong lần gọi này. Đây không phải bằng chứng Luna không trả dữ liệu.

Lượt 2 vẫn được đánh bad vì đã xác nhận absoluteGuarantee, đồng thời assessmentUncertain=true vì beggingWithoutExplanation. Hai trường này phục vụ hai mục đích khác nhau: kết luận từng lượt và mức đầy đủ của bằng chứng cho tổng kết.

sales_rubric.evaluate thêm nhãn bất định vào assessmentReviewItems. Lượt 3 rõ ràng không xóa review item của lượt 2. sales_pipeline.complete chặn mọi session còn review item, chuyển assessmentStatus sang needs-review và giữ status=awaitingCompletion. Unity OnAnalysisResponse hiển thị ReviewRequired theo đúng phản hồi backend. Chưa có bước tự phân xử lại review item trong complete. Bấm kiểm tra lại không tự giải quyết bằng chứng thiếu.

## Vì sao hội thoại đã dừng

Lượt 2 có lời "chắc chắn là sẽ không bị đau nữa". Lượt 3 tiếp tục "chắc chắn là sẽ không bị lặp lại vấn đề này nữa". Luna xác nhận absoluteGuarantee và maintainsUnauthorizedPromise ở lượt 3. Backend kết thúc với maintained_unauthorized_promise. Đây là nhánh kết thúc sớm riêng, không phải do đủ tám lượt, lỗi thu âm hay rating null.

## Replay kiểm tra ảnh hưởng

Replay offline dùng các nhãn đã lưu qua evaluate và complete thật, với store chỉ ở RAM. Không gọi model, không sửa session nguồn hoặc kết quả gameplay.

| Nhãn beggingWithoutExplanation ở lượt 2 | Tổng kết | Kết thúc | Kết quả tính theo rubric |
| --- | --- | --- | --- |
| uncertain như dữ liệu gốc | needs-review | maintained_unauthorized_promise | 0 điểm, lost, bad |
| false giả định để kiểm tra | completed | maintained_unauthorized_promise | 0 điểm, lost, bad |
| true giả định để kiểm tra | completed | maintained_unauthorized_promise | 0 điểm, lost, bad |

Cả hai giá trị xác định đều cho cùng điểm, trustState, customerRating và các cờ rubric. Đây là kiểm tra độ nhạy của kết quả, không phải tự phân loại lại nhãn. Bất định hiện có không làm thay đổi kết quả trong run này nhưng vẫn chặn tổng kết. Kết quả chi tiết lưu ở `recordings/evaluations/2026-10-04-needs-review/replay.json`.

## Hướng sửa đề xuất

- Chỉ chặn tổng kết khi các khả năng của nhãn bất định có thể thay đổi kết quả cần công bố. Kiểm tra trên replay trạng thái hội thoại, giữ nguyên các vi phạm đã xác nhận và không tự đổi uncertain thành false.
- Khi bất định còn ảnh hưởng kết quả, phân xử lại đúng nhãn và transcript gốc bằng Luna trước khi yêu cầu xem xét thủ công. Giới hạn số lần và thời gian xử lý.
- Lưu lý do từ chối theo nhãn và decision bị từ chối trong diagnostics, theo cùng retention của transcript, để phân biệt lỗi schema, quote không khớp và model chủ động uncertain.
- Với trường hợp có vi phạm đã rõ nhưng vẫn còn một nhãn phụ uncertain, reply cần bắt bẻ vi phạm đã xác nhận thay vì chỉ hỏi chung "Chị chưa rõ ý em".

Đợt này chỉ kiểm tra và lưu báo cáo/replay. Chưa sửa logic đánh giá, chưa ghi lại điểm cho run nguồn, không chạy test Unity hoặc gọi model thêm.

## Bản sửa sau chẩn đoán

Đã sửa adapter và completion theo yêu cầu tiếp theo. Adapter đối chiếu chuỗi từ liên tiếp, giữ dấu tiếng Việt và các từ phủ định, cho phép khác biệt dấu câu/Unicode NFC. Quote hợp lệ được thay bằng span gốc trong transcript. Không chấp nhận paraphrase, bỏ từ hoặc bỏ dấu. Các nhãn bị từ chối được sửa tối đa một lần, chỉ hỏi lại nhãn lỗi trong cùng deadline Luna, giữ những nhãn đã hợp lệ. Metadata có attempts, rejectionReasons và rejectedDecisions được giới hạn độ dài, che API key, cùng tổng cost các lần gọi.

Completion dùng sales-review-v1. Replay tối đa 64 tổ hợp true/false trên các review item, qua evaluate và plan_reply hiện tại. Kiểm tra cả điểm/trust/cờ rubric, thành phần được ghi điểm, vi phạm, cam kết tồn tại, mục tiêu, dữ kiện đã thực sự công bố và lý do kết thúc. Nếu replay đổi context của một lượt lịch sử hoặc thiếu dữ liệu, không tự hoàn tất. Không đổi uncertain thành false và không viết lại turns/completedTurns.

Nếu kết quả thống nhất, lưu assessmentReviewResolution, chuyển review item sang assessmentResolvedReviewItems và hoàn tất bằng các thành phần rubric thống nhất. Nếu còn ảnh hưởng kết quả, completion phân xử lại nhãn của transcript gốc với context trước lượt đó. Toàn session chỉ có một pass tự động, tối đa tám nhãn và một deadline chung. Retry complete không gọi model lặp lại. Nếu vẫn không đủ bằng chứng thì giữ needs-review. Kết quả đã hoàn tất vẫn idempotent và không regrade theo code mới.

Nhánh promise_challenge cũng được ưu tiên trước nhánh làm rõ bất định, để một cam kết sai đã xác nhận không bị che bởi nhãn phụ chưa rõ. Dữ liệu review có quote được ẩn khỏi public_session và được xóa cùng diagnostics. Run-result projection giữ summary assessmentReviewResolution để ghi nhận cách tổng kết được quyết định.

Sáu regression đầu đã fail trên code cũ, sau đó pass. Bộ kiểm tra cuối có 131 tests pass, gồm adapter, API, writer và run-result. Các ca giữ review bao gồm khả năng phát sinh vi phạm dù điểm đã về 0, replay đổi mục tiêu lịch sử, lỗi phân xử; cũng kiểm tra deadline, retry idempotent và xóa diagnostics trong khi phân xử.

Replay bản sao run lỗi qua complete thật đã đạt completed, score=0, trustState=lost, customerRating=bad. Hai biến thể của beggingWithoutExplanation cho cùng trạng thái rubric; không cần gọi model. Run draft với kết quả này đủ điều kiện finalized. Transcript, turns và completedTurns không đổi. Chi tiết ở `recordings/evaluations/2026-10-04-needs-review/fix-verification.json`, thời gian khoảng 84 ms trong lần đo này. Session và draft nguồn vẫn nguyên trạng; đây là kiểm chứng trên temporary store.

Backend đang chạy không bật autoreload. Cần restart backend để dùng bản sửa; không thay đổi Unity trong đợt này. Chưa chạy APK/headset hoặc gọi Luna thật thêm. Với run đang bị giữ, gọi lại complete sau restart sẽ áp dụng bước resolution mới; Unity đang ở màn ReviewRequired cần resume session để nhận trạng thái đó.
