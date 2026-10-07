# Jev và Luna phân xử chọn lọc

Triển khai ngày 04/10/2026. STT giữ nguyên. Pipeline mới dùng `sales-jev-v3` với 30 nhãn, rubric `sales-rubric-v2.4`, writer `lan-writer-v2.6` và arbitrator `sales-luna-v1`.

Luồng hiện tại: STT → Jev → chọn nhãn khó → Luna qua OpenRouter nếu cần → luật tính điểm/tiến trình → Qwen viết lời NPC → TTS. Luna chỉ trả hành vi và bằng chứng; backend vẫn quyết định rating, điểm và kết thúc.

## Phạm vi phân xử

- Nhãn quan trọng uncertain có thể thay đổi rating, tiến trình hoặc cam kết đang mở.
- Nhãn mâu thuẫn như đề xuất đổi đồng thời từ chối đổi, hoặc cam kết đồng thời rút cam kết.
- `disrespect=true` được kiểm tra lại vì dễ nhầm với phàn nàn NPC hỏi lặp.
- Câu hỏi lặp còn uncertain được phân xử khi có thể làm cộng sai điểm điều tra.

Mỗi request tối đa tám nhãn. Không hỏi lại bằng chứng mục tiêu đã có và không gọi Luna chỉ vì appearanceQuestion chưa rõ. Deadline Luna mặc định sáu giây. Lỗi, output thiếu, model khác, JSON sai hoặc quote không khớp không ghi đè Jev. Giữ Noul gốc và lưu source/evidence của Luna. Retry dùng checkpoint, không gọi lại stage đã lưu hoặc cộng điểm hai lần.

## Replay dữ liệu đã lưu

11 câu từ các lượt lỗi hôm nay được chạy với transcript STT nguyên trạng và trạng thái trước lượt đã tái dựng. Đây là tập development được chọn vì có lỗi; không có nhãn gold độc lập. Không suy ra accuracy toàn bộ game từ kết quả này.

| Chỉ số | Replay cuối v2 |
| --- | ---: |
| Lượt hoàn thành | 11/11 |
| Lượt cần Luna | 9/11 |
| Nhãn mỗi lần gọi Luna | 2–4 |
| Lượt có assessmentUncertain trước Luna | 9 |
| Lượt có assessmentUncertain sau Luna | 0 |
| Trung vị Jev + Luna | 3,175 giây |
| Trung vị riêng Luna trên 9 lượt được gọi | 2,979 giây |
| Tổng chi phí provider báo cho lượt replay cuối | 0,004587992 USD |
| Request thiếu thông tin cost | 0 |

Các lượt 4–7 có từ chối/đổ lỗi đều ra `bad`. Lượt 3 ghi nhận khách đau ra `good` nhưng chưa hoàn tất mục tiêu cảm xúc vì chưa có câu hỏi mở. Lượt 1, 2 và 8–11 ra `neutral`; các câu phàn nàn NPC lặp mà vẫn giữ phương án đổi không bị gán abuse/disrespect. Tại lúc replay, neutral còn có playerResponseRating=null. Theo thay đổi hợp đồng sau replay, phiên bản code hiện tại trả trực tiếp neutral/uncertain; null dành cho không có kết quả phân loại dùng được. Không sửa dữ liệu replay hoặc bản ghi lịch sử.

V1 và v2 lưu riêng trong `recordings/evaluations/2026-10-04-jev-luna-hybrid`. V2 có từng transcript, trạng thái trước lượt, Noul Jev, nhãn chọn, câu trả lời Luna, nhãn hợp nhất, quyết định, latency và cost. Manifest lưu câu hỏi, phiên bản, deadline và hash nguồn. V1 chạy trước bổ sung xử lý repeatedQuestion và làm rõ định nghĩa directed profanity. Tổng cost provider báo hai đợt là 0,009018092 USD.

Không sửa bản ghi hoặc điểm phiên nguồn. Các bản replay là diagnostics, áp dụng cùng chính sách retention/xóa như nguồn. Thời gian trên không gồm thu âm, STT, Qwen, TTS hoặc phát trên headset.

## Kiểm chứng và sử dụng

99 kiểm tra backend về adapter, luật, HTTP, retry và kết thúc sớm đã pass. Sau bổ sung kiểm tra model sai/truncated output và status sai kiểu, chạy lại 16 kiểm tra liên quan đều pass; tổng có 101 kiểm tra backend khác nhau đã pass trong đợt này. 19 kiểm tra Web contract pass với rubric v2.4. Không chạy lại build/headset hoặc toàn bộ bộ test không liên quan.

Khởi động lại backend rồi tạo cuộc hội thoại mới trong chế độ `SALES_PIPELINE_MODE=openrouter`. `SALES_LUNA_ARBITRATION_ENABLED=true` là mặc định; có thể tắt bằng false cho các phiên mới. Phiên đang chạy với bộ nhãn cũ không được trộn luật mới; kết quả lịch sử đã hoàn tất vẫn giữ nguyên. Tám lượt là upper limit, đủ mục tiêu hoặc có ending bắt buộc thì kết thúc sớm. Tham khảo chi tiết vận hành tại `sales-openrouter-v2.md`.
