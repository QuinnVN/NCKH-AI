Status: implemented; headset-validation-pending

Implementation notes: [verification](../../docs/verification/sales-customer-state-v3-2026-10-05.md), [ADR 0004](../../docs/adr/0004-ledger-scoring-and-customer-concerns.md).

# Lan phản ứng theo trạng thái khách hàng, chấm điểm từ evidence ledger

Ngày tổng hợp: 05/10/2026. Phạm vi gồm backend NCKH-AI cho returning-customer conversation chạy pipeline OpenRouter. Spec này tổng hợp năm hướng đã thảo luận sau khi đọc hai session gần nhất (`1c6d65843d5f4b2c867c079dbd7479ab`, `6de080f41334460dace500d0e8161ea0`) và code hiện tại. Các ngưỡng, số bậc gợi ý và tên trạng thái dưới đây là đề xuất thiết kế, chưa được kiểm chứng trên người chơi thật.

## Problem Statement

Người chơi bị kẹt trong returning-customer conversation dù đang làm những việc hợp lý. Ở hai session gần nhất, cả 10 lượt đều nằm ở mục tiêu 1. Người chơi đề nghị đổi giày đúng chính sách, hỏi chỗ đau, đề nghị thử giày, nhưng chưa xin lỗi hay thể hiện sự thấu hiểu. Lan không nói ra điều còn thiếu. Lan liên tục hỏi "Em định kiểm tra đôi giày này thế nào?", nên người chơi tiếp tục kiểm tra, mà việc đó không được tính khi chưa qua mục tiêu 1.

Lan không trả lời câu hỏi của người chơi. Người chơi hỏi "chị đau ở đâu" bốn lần và đã được nhận diện đúng, nhưng Lan chỉ được tiết lộ dữ kiện từ mục tiêu 2. Lan hỏi ngược lại người chơi, khiến cuộc trò chuyện giống thẩm vấn một chiều.

Lan lặp lại câu cũ. Một câu fallback xuất hiện bốn lần trong mười lượt. Guard của writer đã chặn câu trả lời vì lặp, nhưng câu được dùng thay lại không qua kiểm tra lặp. Guard cũng đòi những từ khóa tiếng Việt cố định, nên nhiều câu trả lời hợp lý của Qwen bị loại.

Kết quả cuối không phản ánh những gì người chơi đã làm. Người chơi nêu đúng điều kiện đổi trong 7 ngày với giày còn nguyên vẹn vẫn nhận 0/100, `trustState=lost`, và Lan đòi gặp quản lý sau một câu hỏi lịch sự. Bằng chứng bị khóa theo mục tiêu đang hoạt động: lời đề nghị đổi ở mục tiêu 1 không được tính lại ở mục tiêu 3. Khi bế tắc, cách kết thúc duy nhất là hết lượt.

Với người nghiên cứu và người bảo trì, thứ tự chấm điểm đang quyết định cả thứ tự hội thoại. Mỗi lần sửa thêm nhánh, regex và câu mẫu mới, nhưng lỗi chỉ chuyển sang chỗ khác. Muốn biết một thay đổi rubric ảnh hưởng thế nào thì phải replay từng run bằng tay.

## Solution

Lan phản ứng theo trạng thái khách hàng thay vì theo mục tiêu chấm điểm đang hoạt động. Trạng thái gồm bốn customer concern có thể được giải quyết theo bất kỳ thứ tự nào, các dữ kiện Lan đã nói và hint level của concern Lan đang nêu. Mỗi lượt, Lan xử lý theo thứ tự: phản ứng với hành vi xấu nếu có, trả lời câu hỏi về dữ kiện, rồi nêu concern quan trọng nhất còn mở.

Dữ kiện của kịch bản là sự thật. Khi người chơi hỏi đúng dữ kiện, Lan trả lời ngay ở bất kỳ thời điểm nào. Điểm được cộng cho việc hỏi đúng, còn việc Lan kể ra thông tin không phải là phần thưởng.

Điểm cuối được tính bằng một hàm chấm điểm duy nhất trên evidence ledger của toàn cuộc hội thoại. Mỗi sales rubric component được cộng khi điều kiện ngữ cảnh của nó đúng, không phụ thuộc mục tiêu đang hoạt động. Cùng hàm đó tính rating từng lượt, tính kết quả khi complete và chấm lại recording offline mà không gọi model.

Writer chỉ bị chặn bởi các ràng buộc cứng như bịa chính sách, hứa sai, lộ dữ kiện chưa được phép, xưng hô sai, quá dài hoặc lặp lại. Mọi câu Lan nói ra, kể cả fallback, phải qua cùng một lần kiểm tra.

Khi concern Lan đang nêu không có tiến triển, Lan nói rõ hơn một bậc nhưng vẫn giữ vai khách hàng. Nếu đã ở bậc rõ nhất mà vẫn không có tiến triển, Lan kết thúc vì bế tắc. Tám lượt chỉ còn là giới hạn an toàn.

## User Stories

1. Là người chơi, tôi muốn Lan trả lời ngay khi tôi hỏi chị đau ở đâu, để tôi điều tra được nguyên nhân.
2. Là người chơi, tôi muốn Lan trả lời câu hỏi về dữ kiện ở bất kỳ lúc nào, để tôi không bị phạt vì hỏi sớm.
3. Là người chơi, tôi muốn Lan trả lời câu hỏi trước rồi mới nêu điều chị còn lo, để cuộc trò chuyện tự nhiên.
4. Là người chơi, tôi muốn Lan cho tôi biết điều chị thật sự chưa hài lòng, để tôi không đoán mò.
5. Là người chơi, tôi muốn Lan nêu đúng điều còn thiếu, chẳng hạn cảm giác không được lắng nghe, thay vì đòi tôi kiểm tra giày lần nữa.
6. Là người chơi, tôi muốn Lan nói rõ dần khi tôi vẫn chưa hiểu ý chị, để tôi có cơ hội sửa trước khi hết lượt.
7. Là người chơi, tôi muốn lời gợi ý của Lan vẫn là lời khách hàng, để mô phỏng không biến thành bài hướng dẫn.
8. Là người chơi, tôi muốn Lan không lặp lại một câu trong ba lượt gần nhất, để tôi biết chị đang nghe.
9. Là người chơi, tôi muốn Lan không nói "chị đã trả lời rồi" khi chị chưa trả lời, để không bị đổ lỗi sai.
10. Là người chơi, tôi muốn lời đề nghị đổi giày đúng chính sách được ghi nhận dù tôi nói nó sớm, để không phải nhắc lại cùng một ý.
11. Là người chơi, tôi muốn Lan nhớ phương án đổi tôi đã đưa ra, để chị không hỏi "tiệm có phương án nào không" sau khi tôi đã nói.
12. Là người chơi, tôi muốn lời xin lỗi ở bất kỳ lượt nào cũng được tính, để không phải đúng thứ tự mới được điểm.
13. Là người chơi, tôi muốn câu hỏi về nhu cầu đi bộ và độ vừa được tính khi tôi hỏi trước lúc xin lỗi, để mọi kỹ năng thật sự thể hiện đều có điểm.
14. Là người chơi, tôi muốn phần giải thích nguyên nhân chỉ được tính khi tôi đã có đủ dữ kiện, để điểm phản ánh việc suy luận có căn cứ.
15. Là người chơi, tôi muốn đề xuất giày nhẹ và đề nghị thử chỉ được tính sau khi đã tìm hiểu nhu cầu, để đề xuất phải dựa trên hiểu biết về khách.
16. Là người chơi, tôi muốn điểm không giảm khi tôi làm thêm một việc tốt, để hệ thống chấm công bằng.
17. Là người chơi, tôi muốn Lan dịu lại khi tôi giải quyết đúng điều chị lo, để thấy hành động của mình có tác dụng.
18. Là người chơi, tôi muốn Lan cứng rắn hơn khi tôi từ chối đổi, đổ lỗi hoặc thiếu tôn trọng, để hậu quả rõ ràng.
19. Là người chơi, tôi muốn Lan chấp nhận phương án đổi đúng chính sách khi chị đã được lắng nghe và đã kể vấn đề, để cuộc trò chuyện có thể kết thúc hợp lý.
20. Là người chơi, tôi muốn được đi tiếp tới câu chất vấn lấy lại niềm tin khi tôi đã tìm ra nguyên nhân và đề xuất giải pháp phù hợp, để thể hiện kỹ năng đầy đủ.
21. Là người chơi, tôi muốn cuộc trò chuyện kết thúc với lý do rõ ràng khi tôi bế tắc, để không phải nói thêm nhiều lượt vô ích.
22. Là người chơi, tôi muốn lời kết của Lan khớp với kết quả, để tôi hiểu mình đã làm tốt hay chưa.
23. Là người chơi, tôi muốn Lan không đòi gặp quản lý khi tôi lịch sự nhưng chưa giải quyết xong, để kết thúc phản ánh đúng thái độ của tôi.
24. Là người chơi, tôi muốn các hứa hẹn trái chính sách vẫn bị phạt như hiện tại, để mô phỏng giữ chuẩn của cửa hàng.
25. Là người chơi, tôi muốn việc rút lại một cam kết sai vẫn giải quyết outstanding promise như hiện tại, để tôi có cơ hội sửa.
26. Là người chơi, tôi muốn lượt bị nhận diện không chắc chắn vẫn được hỏi làm rõ, để không bị phạt vì STT nghe sai.
27. Là người chơi, tôi muốn tiến trình mục tiêu trong VR tăng dần như hiện tại, để giao diện không báo lỗi.
28. Là người chơi, tôi muốn vẫn có tối đa tám lượt, để thời lượng mô phỏng không thay đổi.
29. Là người nghiên cứu, tôi muốn kết quả cuối được tính từ evidence ledger, để mọi điểm số truy vết được tới từng lượt.
30. Là người nghiên cứu, tôi muốn chấm lại recording offline với rubric mới mà không gọi model, để so sánh phiên bản nhanh và rẻ.
31. Là người nghiên cứu, tôi muốn việc chấm lại không sửa kết quả đã finalize, để dữ liệu nghiên cứu cũ giữ nguyên.
32. Là người nghiên cứu, tôi muốn báo cáo chấm lại ghi rõ phiên bản rubric và session nào bị bỏ qua, để biết phạm vi so sánh.
33. Là người nghiên cứu, tôi muốn kết quả khi complete trùng với kết quả chấm lại từ cùng ledger, để chắc chắn chỉ có một cách tính điểm.
34. Là người nghiên cứu, tôi muốn biết lượt nào bị xem là không có tiến triển và hint level khi đó, để phân tích chỗ người chơi bị kẹt.
35. Là người nghiên cứu, tôi muốn session ghi lại endingReason `stalemate` khi kết thúc vì bế tắc, để phân biệt với hết lượt.
36. Là người nghiên cứu, tôi muốn session cũ giữ nguyên phiên bản và kết quả, để so sánh nghiên cứu không bị trộn luật.
37. Là người vận hành, tôi muốn thay đổi này không cần APK mới, để triển khai bằng việc khởi động lại backend.
38. Là người vận hành, tôi muốn số lần fallback và lý do guard từ chối vẫn được lưu, để theo dõi chất lượng writer.
39. Là người vận hành, tôi muốn số lần gọi Jev, Luna và Qwen mỗi lượt không tăng, để chi phí và độ trễ không xấu đi.
40. Là người bảo trì, tôi muốn trạng thái khách hàng nằm trong một module thuần, để kiểm thử mà không cần model.
41. Là người bảo trì, tôi muốn chỉ có một hàm kiểm tra câu Lan nói, để writer adapter và pipeline không lệch nhau.
42. Là người bảo trì, tôi muốn câu fallback được ghép từ trạng thái hiện tại, để không phải thêm câu mẫu mỗi khi gặp lỗi lặp.
43. Là người bảo trì, tôi muốn guard chỉ chặn điều cấm, để Qwen không bị loại vì diễn đạt khác từ khóa.
44. Là người bảo trì, tôi muốn các bất biến của hội thoại được kiểm thử trên nhiều chuỗi lượt, để lỗi kẹt hoặc lặp bị phát hiện trước khi chạy trên headset.
45. Là người bảo trì, tôi muốn hai session lỗi gần nhất trở thành fixture hồi quy, để lỗi đã gặp không quay lại.
46. Là agent triển khai, tôi muốn glossary và ADR được cập nhật cùng thay đổi, để thuật ngữ trong code và tài liệu thống nhất.

## Implementation Decisions

### Phiên bản và phạm vi

- Session mới dùng `pipelineVersion=sales-openrouter-v3`, `rubricVersion=sales-rubric-v3`, `promptVersion=lan-writer-v3`. `scenarioVersion`, `questionSetVersion` và `thresholdVersion` giữ nguyên, nên không cần hiệu chỉnh lại nhãn Jev.
- Session cũ giữ nguyên phiên bản và kết quả. Như hiện tại, session đang mở ở phiên bản không còn được hỗ trợ không tự chuyển sang luật mới.
- Số lần gọi model mỗi lượt giữ nguyên: một lần Jev, Luna theo luật chọn nhãn hiện có, và tối đa hai lần Qwen.

### Module trạng thái khách hàng

- Thêm một module thuần và tất định. Đầu vào là trạng thái khách hàng trước lượt, nhãn đã được chấp nhận của lượt và các dữ kiện đã biết. Đầu ra là trạng thái mới cùng các đầu vào cho planner. Module không gọi mạng và không đọc file.
- Dialogue state gồm: trạng thái mở hoặc đã giải quyết của bốn customer concern, dữ kiện Lan đã nói, concern Lan đang nêu, hint level của từng concern, đã đưa ra câu chất vấn niềm tin hay chưa, và outstanding promise.
- Bốn customer concern, đánh số trùng mục tiêu cũ để giữ tương thích:

| # | Concern | Được giải quyết khi |
| --- | --- | --- |
| 1 | Chưa được lắng nghe | Ledger có `acknowledgment` trong một lượt không `abuse` và có `openQuestion`, theo bất kỳ thứ tự nào |
| 2 | Chưa rõ vì sao đau | Đã biết `walking_routine` cùng ít nhất một trong `fit_condition`/`lighter_preference`, và có `causeStatement` ở một lượt sau khi đã biết các dữ kiện đó |
| 3 | Chưa có phương án phù hợp | Ledger có `exchangeOffer` và `exchangeConditions` an toàn, cùng `lightweightForWalking` và `fitOrWalkTrial` ở các lượt sau khi concern 2 đã đủ dữ kiện |
| 4 | Sợ lần sau lại vậy | Chỉ mở khi Lan đã đưa câu chất vấn niềm tin. Được giải quyết khi có `originalSaleResponsibility`, `routineMatchExplanation` và `fitOrWalkTrial` sau câu chất vấn, trong lượt tôn trọng |

- Concern được nêu là concern có số nhỏ nhất còn mở. Nếu concern đó gồm nhiều phần, planner nêu phần còn thiếu, ví dụ thiếu sự thấu hiểu hay thiếu câu hỏi về vấn đề.
- Câu chất vấn niềm tin được đưa ra khi concern 1–3 đã được giải quyết và còn ít nhất một lượt. Luật này giữ nguyên như hiện tại.
- `customerDisposition` vẫn có ba giá trị: `irritated` khi lượt bị chấm xấu hoặc concern đang nêu ở hint level 2; `receptive` khi lượt giải quyết một concern hoặc Lan trả lời một câu hỏi dữ kiện; `guarded` trong các trường hợp còn lại.

### Thứ tự lập kế hoạch câu trả lời

1. Kết thúc: `objectives_completed`, `exchange_accepted`, `stalemate`, `turn_limit` và các kết thúc bắt buộc hiện có.
2. Phản ứng với hành vi: từ chối xử lý, thiếu tôn trọng hoặc lăng mạ, ép mang tiếp, outstanding promise, và hỏi làm rõ khi nhãn không chắc chắn. Giữ nguyên ngữ nghĩa hiện tại. Lượt bị chấm xấu không tiết lộ dữ kiện.
3. Trả lời câu hỏi dữ kiện của lượt này, tối đa hai dữ kiện như hiện tại.
4. Nêu concern còn mở theo hint level hiện tại.

Bước 3 và 4 có thể ghép trong cùng một câu trả lời ngắn.

### Tiết lộ dữ kiện

- `pain_location`, `late_discomfort`, `walking_routine`, `fit_condition`, `lighter_preference` và `appearance` được tiết lộ ở mọi thời điểm khi nhãn câu hỏi tương ứng là true.
- `original_missed_question` vẫn chỉ xuất hiện trong phần chất vấn niềm tin.
- Backend tự xác định một câu hỏi có lặp không: câu hỏi chỉ bị xem là lặp khi mọi dữ kiện nó nhắm tới đã có trong danh sách đã biết trước lượt. Nhãn `repeatedQuestion` của Jev vẫn được lưu vào ledger nhưng luật v3 không dùng nhãn này để trả lời hay chấm điểm. Lan chỉ nói "chị đã trả lời rồi" khi backend xác định câu hỏi là lặp.

### Chấm điểm từ evidence ledger

- Một hàm chấm điểm thuần nhận ledger cùng dữ kiện kịch bản và trả về các component, sự kiện vi phạm, outstanding promise, cờ hoàn thành của bốn concern và outcome. Mỗi entry trong ledger lưu nhãn đã chấp nhận, dữ kiện đã biết trước lượt, outstanding promise trước lượt, câu chất vấn đã hiện chưa và dữ kiện được tiết lộ ở lượt đó.
- Hàm này được dùng ở ba nơi: tính rating từng lượt trên phần ledger tính đến lượt đó, tính kết quả khi complete, và chấm lại offline.
- Bằng chứng không còn bị khóa theo mục tiêu đang hoạt động. Điều kiện cộng từng component, mỗi component 10 điểm như hiện tại:

| Component | Điều kiện |
| --- | --- |
| acknowledgment | `acknowledgment` ở lượt không `abuse` |
| policy | `exchangeOffer` và `exchangeConditions` ở lượt an toàn (cùng lượt hoặc lượt sau còn giữ đề nghị) |
| lightweight, trial, explanation | Nhãn tương ứng ở lượt an toàn, sau khi concern 2 đã đủ dữ kiện |
| use_question | `walkingQuestion` khi `walking_routine` chưa được biết |
| fit_question | `fitQuestion` hoặc `preferenceQuestion` khi dữ kiện tương ứng chưa được biết |
| cause | `causeStatement` sau khi concern 2 đã đủ dữ kiện |
| responsibility | `originalSaleResponsibility` ở lượt an toàn sau câu chất vấn niềm tin |
| challenge_reply | Sau câu chất vấn, ledger có `fitOrWalkTrial` và `routineMatchExplanation`, lượt tôn trọng |

- "Lượt an toàn" giữ định nghĩa hiện tại: không có vi phạm, không có nhãn hành vi xấu và không còn outstanding promise.
- Vi phạm, mức phạt, outstanding promise, rút lại cam kết và cách xử lý nhãn không chắc chắn giữ nguyên luật hiện tại.
- Rating lượt: `bad` khi có vi phạm hoặc nhãn hành vi xấu; `good` khi lượt được cộng component mới hoặc giải quyết một concern; `uncertain` theo luật hiện tại; còn lại là `neutral`.
- `emotionalHandling`, `causeIdentification`, `solutionSuitability`, `trustRebuilding` lần lượt bằng trạng thái đã giải quyết của concern 1–4.
- `trustState` giữ công thức hiện tại nhưng đọc từ concern thay cho `completedObjectives`. Kết thúc `stalemate` không phải kết thúc bắt buộc, nên vẫn có thể đạt `partially_restored` nếu đủ điều kiện.
- Hàm chấm điểm có tính đơn điệu: thêm một nhãn tốt vào một lượt không bao giờ làm giảm điểm cuối.

### Chấp nhận phương án đổi

- Lan chấp nhận và kết thúc với `exchange_accepted` khi: concern 1 đã giải quyết, đã biết ít nhất một dữ kiện về vấn đề trước lượt này, concern 2 còn mở, và lượt hiện tại an toàn với cả `exchangeOffer` lẫn `exchangeConditions` đều true. Luật này giống v2.5 nhưng không còn phụ thuộc mục tiêu đang hoạt động.
- Nếu concern 2 đã được giải quyết, lời đề nghị đổi không kết thúc cuộc trò chuyện. Lan nêu concern 3 để người chơi giải thích vì sao phương án phù hợp và đề nghị thử.

### Hint level và bế tắc

- Mỗi concern có hint level 0, 1 hoặc 2:
  - Bậc 0: Lan nói concern một cách chung chung, đúng vai khách hàng.
  - Bậc 1: Lan phàn nàn cụ thể về phần còn thiếu.
  - Bậc 2: Lan nói thẳng điều mình cần, vẫn bằng lời khách hàng. Lan không đưa câu trả lời mẫu và không nhắc tới rubric, điểm hay mục tiêu.
- Một lượt là không có tiến triển khi nhãn đã chắc chắn, lượt không bị chấm xấu, không có component mới, không có dữ kiện mới được tiết lộ và concern đang nêu vẫn mở. Lượt như vậy làm hint level của concern đang nêu tăng một bậc. Lượt bị chấm xấu hoặc không chắc chắn giữ nguyên hint level. Hint level của một concern không giảm cho đến khi concern đó được giải quyết.
- Nếu một lượt không có tiến triển xảy ra khi concern đang nêu đã ở bậc 2, cuộc trò chuyện kết thúc với `endingReason=stalemate`. Lan nói lời kết theo `trustState` của outcome. Tám lượt vẫn là giới hạn trên; `turn_limit` vẫn áp dụng.
- Ledger lưu concern được nêu, hint level và cờ không có tiến triển của từng lượt để phục vụ phân tích.

### Writer, guard và fallback

- Plan gửi cho Qwen gồm hành động hội thoại, concern được nêu cùng phần còn thiếu, hint level, các dữ kiện phải trả lời, dữ kiện đã biết, disposition và các câu Lan đã nói gần đây. Qwen vẫn không nhận nhãn, điểm hay nguyên nhân thật.
- Các guard cứng được giữ: xưng hô sai, quá dài hoặc quá hai câu, sai định dạng, từ ngữ nội bộ hoặc chỉ dẫn, bịa chính sách hoặc thời hạn, hứa hoàn tiền/giảm giá/bồi thường/bảo đảm tuyệt đối, dữ kiện chưa được phép hoặc bị nói ngược nghĩa, thiếu dữ kiện bắt buộc phải trả lời, kết thúc ngoài kế hoạch, giả vờ không nghe rõ, lặp lời người chơi, giọng hành chính, và lặp lại một trong ba câu gần nhất của Lan.
- Kiểm tra nội dung bắt buộc bằng từ khóa chỉ còn dùng cho các hành động mà sai nghĩa sẽ đổi kết quả: các lời kết, cảnh báo thiếu tôn trọng, chất vấn từ chối, hỏi làm rõ việc từ chối, chất vấn ép mang tiếp, chất vấn lời hứa sai và câu chất vấn niềm tin. Bỏ kiểm tra này cho việc nêu concern, ghi nhận phương án đổi, mời điều tra và trả lời lời than phiền mở đầu.
- Chỉ có một hàm chọn fallback, dùng chung cho writer adapter và pipeline. Hàm thử lần lượt các mẫu theo hành động, concern, phần còn thiếu và hint level (ít nhất ba mẫu mỗi tổ hợp, ghép với câu trả lời dữ kiện nếu có). Sau đó hàm thử một nhóm ít nhất bốn câu chung đúng vai, không có câu hỏi hay mệnh đề từ sáu từ trở lên trùng nhau. Mỗi ứng viên đều qua đúng bộ kiểm tra dành cho câu của Qwen. Vì cửa sổ chống lặp là ba câu, luôn có ít nhất một câu hợp lệ.
- Pipeline không còn đưa vào một chuỗi fallback chưa kiểm tra. Mọi câu được commit đều đã qua kiểm tra. Metadata vẫn lưu `fallbackUsed`, mã lỗi và lý do bị từ chối của từng lần thử.

### Tương thích API và Unity

- Hình dạng response của lượt và của complete không đổi. Không thêm trường bắt buộc, nên không cần APK mới.
- Unity báo lỗi `invalid_phase_progression` nếu `activeObjective` giảm, tăng hơn một bậc, hoặc tăng mà không có `objectiveCompleted`. Vì vậy, `activeObjective` hiển thị bằng giá trị nhỏ hơn giữa concern có số nhỏ nhất còn mở và giá trị lượt trước cộng một. `objectiveCompleted` là true khi và chỉ khi giá trị hiển thị tăng ở lượt này. Khi các concern được giải quyết không theo thứ tự, giá trị hiển thị đuổi kịp từng bậc qua các lượt sau. Kết quả cuối luôn đọc từ ledger.
- `stalemate` là giá trị mới của `endingReason`, nằm trong giới hạn 32 ký tự của completion reason. Unity chuyển `endingReason` thành completion reason, còn contract kết quả của Web nhận chuỗi tùy ý. Spec chỉ yêu cầu kiểm tra lại hai điểm này, không sửa Unity hay Web.

### Chấm lại offline

- Thêm một lệnh chỉ đọc. Lệnh nhận thư mục recordings cùng phiên bản rubric, chấm lại mọi session có ledger bằng hàm chấm điểm và ghi báo cáo vào thư mục đánh giá mới trong recordings.
- Báo cáo ghi phiên bản rubric dùng để chấm, outcome đã lưu, outcome tính lại và chênh lệch. Session có phiên bản cũ được đánh dấu là phép thử giả định. Session thiếu trường ledger cần thiết được liệt kê là bị bỏ qua kèm lý do.
- Lệnh không sửa session, run draft, kết quả đã finalize hay MongoDB, và không gọi model.

### Tài liệu miền

- Thêm "Customer concern" và "Hint level" vào glossary. Định nghĩa "Dialogue state" chuyển từ mục tiêu đang hoạt động sang concern.
- Thêm một ADR mới thay câu "Each rubric component is earned once in its objective context" trong ADR 0003 bằng "được cộng một lần khi điều kiện ngữ cảnh của nó đúng trên evidence ledger". Các quyết định còn lại của ADR 0003 giữ nguyên: backend sở hữu tiến trình và điểm, lời của Qwen không tạo ra điểm hay kết thúc.
- Cập nhật tài liệu Sales Part 2 và sửa câu cũ trong README nói rằng `trustState` dựa trên số lượt tốt/xấu.

## Testing Decisions

- Một test tốt chỉ kiểm tra hành vi quan sát được trong kết quả mà turn pipeline trả về: `customerText`, `disclosedFactIds`, `playerResponseRating`, `activeObjective`, `objectiveCompleted`, `endingReason`, cùng điểm và `trustState` khi complete. Test không đọc trạng thái nội bộ của module concern hay planner, và không khẳng định từng câu chữ, trừ dữ kiện bắt buộc và các lời kết.
- Theo xác nhận của người dùng, seam duy nhất là turn pipeline ở mức hàm: gọi trực tiếp hàm xử lý lượt và hàm complete của pipeline OpenRouter trên một session store tạm, không qua HTTP. STT, Jev, Luna và Qwen được thay bằng bản giả có kịch bản, truyền qua các tham số inject mà hàm xử lý lượt đã có.
- Prior art: bộ test HTTP của pipeline Sales v2 đã có bản giả classifier (`classification(...)` dựng nhãn true/false), writer giả trả `fallbackText`, arbitrator giả và cách tạo store trong thư mục tạm. Test mới dùng lại các helper này nhưng gọi thẳng turn pipeline. Hiện chưa có test nào gọi turn pipeline trực tiếp, nên helper dựng session OpenRouter v3 trong store tạm là phần mới duy nhất của harness.
- Lệnh chấm lại offline được kiểm tra bằng cách chạy trên store tạm do các test turn pipeline tạo ra, rồi so outcome với kết quả của hàm complete. Lệnh chỉ là lớp vỏ mỏng gọi hàm chấm điểm dùng chung, nên không cần seam riêng.
- Fixture hồi quy từ dữ liệu thật: nhãn đã lưu của `1c6d65843d5f4b2c867c079dbd7479ab`, `6de080f41334460dace500d0e8161ea0` và `25bf01d8661448bdb8c8ce70fcd687f8` được phát lại qua turn pipeline với bản giả trả đúng nhãn đó. Kết quả mong đợi:
  - Lượt đầu tiên có `painLocationQuestion` trả về `pain_location`.
  - Không câu Lan nào lặp trong ba lượt gần nhất.
  - Lan nêu concern thiếu sự thấu hiểu và hint level tăng tới bậc 2 trước khi hết lượt.
  - Run `25bf…` vẫn kết thúc `exchange_accepted` ở lượt 4.
- Các bất biến được kiểm tra trên nhiều chuỗi nhãn sinh tất định theo seed, không thêm dependency mới:
  - Không câu Lan nào được commit nếu lặp một trong ba câu gần nhất.
  - Một câu hỏi dữ kiện chưa biết trong lượt không xấu luôn được trả lời ngay lượt đó.
  - Thêm một nhãn tốt không làm giảm điểm cuối.
  - Sau ba lượt liên tiếp không có tiến triển trên cùng concern, cuộc trò chuyện đã kết thúc.
  - `activeObjective` luôn thỏa luật tiến trình của Unity.
  - Outcome khi complete bằng outcome chấm lại từ cùng ledger.
- Các test guard hiện có cho chính sách, dữ kiện, bảo đảm tuyệt đối và lời kết tiếp tục áp dụng. Test HTTP đang khẳng định hành vi khóa theo mục tiêu (ví dụ không tiết lộ dữ kiện ở mục tiêu 1, bằng chứng tách theo mục tiêu) được chuyển sang ngữ nghĩa v3. Các test HTTP còn lại vẫn giữ để bảo vệ phần contract route, retry, checkpoint và xóa diagnostic; spec này không thêm test HTTP mới.

## Out of Scope

- Bộ người chơi mô phỏng bằng LLM và các chỉ số tỉ lệ kẹt, lặp, fallback trên hàng trăm cuộc hội thoại. Phần này nên là spec riêng, dùng spec này làm nền.
- Giảm độ trễ: thay đổi luật gọi Luna, câu đệm hay âm thanh chờ trong Unity.
- Thay đổi câu hỏi Jev, ngưỡng nhận định hoặc model.
- Thay đổi giao diện Unity, Web hoặc contract của các repo đó.
- Chấm điểm Phần 1, gồm cả lỗi lời bài hát được 75 điểm, và cách run draft ghi lỗi STT Phần 1 thành điểm 0.
- Pipeline legacy và shadow.
- Chấm lại hoặc ghi đè kết quả lịch sử.

## Further Notes

- Thay đổi này cố ý đổi ý nghĩa của điểm: lời xin lỗi hay đề nghị đổi nói sớm giờ được tính. Kết quả v3 không so sánh trực tiếp được với v2.x. Báo cáo chấm lại offline giúp đo chênh lệch trên cùng recording.
- Lời gợi ý ở hint level 2 dễ trượt thành hướng dẫn. Nên có người duyệt các mẫu fallback bậc 2 và một vài câu Qwen viết trước khi chạy với người tham gia thật.
- Khi concern được giải quyết không theo thứ tự, tiến trình hiển thị trong VR có thể chậm hơn tiến trình thật vài lượt. Cách này chấp nhận được vì Unity chỉ hiển thị trạng thái; kết quả cuối luôn đọc từ ledger.
- Bằng chứng gốc nằm ở hai session đã nêu cùng chẩn đoán stall ngày 05/10/2026 trong thư mục đánh giá của recordings.
