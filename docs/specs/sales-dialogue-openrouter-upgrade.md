Status: implemented; rollout-validation-pending

# Nâng cấp hội thoại Sales, pipeline OpenRouter và logic chấm điểm

Ngày tổng hợp: 03/10/2026. Phạm vi triển khai gồm backend NCKH-AI và hội thoại khách quay lại trong NCKH-VR. Đây là đặc tả triển khai, chưa phải mô tả chức năng đã được đưa vào production.

Người dùng đã chốt chấp nhận phụ thuộc mạng, ưu tiên một tài khoản OpenRouter, dùng `qwen/qwen3.7-flash` viết lời Lan, giữ Supertonic local và bỏ TTS cloud. Người dùng đã xác nhận ưu tiên kiểm thử qua API hội thoại hiện có. Trọng số rubric, ngưỡng nhận định, giới hạn lượt và các mặc định vận hành dưới đây là đề xuất thiết kế của đặc tả này, không phải những thông số đã được người dùng duyệt riêng hoặc đã được kiểm chứng thực nghiệm.

## Problem Statement

Người chơi cần cảm thấy Lan đang nghe và phản hồi đúng lời mình nói. Hiện tại model local phân loại hành vi, còn backend chọn câu từ các nhóm câu có sẵn. Lan dễ hỏi lại điều đã được giải thích, phản hồi thiếu liên hệ với lời vừa nghe, và gặp lỗi khi nhóm câu đã dùng hết. Chỉ thay model phân loại sẽ không giải quyết được chất lượng hội thoại.

Pipeline hiện tại còn chờ người chơi kết thúc thu âm, nhận toàn bộ audio, chạy STT, phân loại rồi mới phát customer speech. Không có final transcript trong khi người chơi đang nói. Unity khóa luồng thu âm khi Lan phát giọng. Backend và client có giới hạn bốn lượt, dù việc hỏi nguyên nhân thường cần nhiều hơn một lượt. Một đường đi hiện tại vừa chuyển sang mục tiêu khôi phục niềm tin, đưa ra câu chất vấn bắt buộc, đã chạm giới hạn lượt. Đường kết thúc sớm khác có thể bỏ qua mục tiêu này.

Chấm điểm và kết quả Lan hiện chưa dùng chung một căn cứ. Backend xác định `good` hoặc `bad` từ các cờ và các phép sửa bằng từ khóa. Một lượt chỉ xin lỗi hoặc xác nhận ngắn có thể bị coi là xấu. Phần phân tích cuối dùng LLM chấm hai tiêu chí 0–50, sau đó trừ 10 điểm cho mỗi vi phạm được lưu. Customer trust state lại chủ yếu phụ thuộc số lượt tốt/xấu. Vì vậy lời chào kết thúc, tiến trình mục tiêu, điểm số và mức tin tưởng có thể không nhất quán.

Kho khảo sát có 150 lượt Sales quay lại, lệch nhiều về mục tiêu 1 và không có lượt ở mục tiêu 4. Nhãn lưu sẵn do hệ thống tạo, có schema cũ và ví dụ sai. Khảo sát Jev trên 40 lượt với tám câu hỏi cho kết quả khả quan, nhưng chưa xác nhận toàn bộ hành vi, rubric hay độ chính xác trên audio thật. Cần nâng pipeline cùng với cách đánh giá, không lấy kết quả khảo sát nhỏ làm bằng chứng sẵn sàng chấm toàn bộ game.

## Solution

Giữ STT Sherpa và Supertonic trên máy. Dùng Jev qua OpenRouter nhận định hành vi trong final transcript. Backend quyết định conversation objective, dữ kiện được phép tiết lộ, mức cảm xúc, điểm và nội dung cần đáp. Qwen 3.7 Flash chỉ diễn đạt nội dung đó thành lời Lan tự nhiên. Customer text được kiểm tra và lưu trước khi Supertonic phát customer speech.

Chấm điểm bằng rubric có phiên bản trên các bằng chứng đã được chấp nhận. Dùng cùng bằng chứng đó để xác định kết quả Lan. Lời xác nhận, hỏi làm rõ và trường hợp chưa chắc chắn được xử lý riêng, không tự động biến thành vi phạm. Cho người chơi đủ lượt điều tra và trả lời câu chất vấn khôi phục niềm tin. Khi lỗi mạng, lỗi micro hoặc lỗi model xảy ra, game cho thử lại và giữ kết quả đã xác nhận.

## User Stories

1. Là người chơi, tôi muốn Lan đáp đúng nội dung tôi vừa nói để cảm thấy mình được lắng nghe.
2. Là người chơi, tôi muốn Lan nói tiếng Việt ngắn gọn và tự nhiên để hội thoại không giống đọc câu mẫu.
3. Là người chơi, tôi muốn Lan giữ cách xưng hô chị/em và vai khách hàng để nhân vật nhất quán.
4. Là người chơi, tôi muốn Lan nhớ điều đã nói trong phiên để không phải giải thích lại vô cớ.
5. Là người chơi, tôi muốn Lan nhận ra tôi đã đề xuất đổi giày để không hỏi lại kế hoạch đã có.
6. Là người chơi, tôi muốn Lan trả lời câu hỏi mới về vị trí đau hoặc thời điểm đau để điều tra nguyên nhân.
7. Là người chơi, tôi muốn lời xác nhận dữ kiện được phân biệt với hỏi lặp để không bị đánh giá sai.
8. Là người chơi, tôi muốn câu hỏi làm rõ một thông tin chưa đủ được chấp nhận để có thể hiểu vấn đề.
9. Là người chơi, tôi muốn lời xin lỗi đầu tiên được ghi nhớ để có thể đề xuất giải pháp ở lượt sau.
10. Là người chơi, tôi muốn câu "dạ" hoặc "vâng" không bị xem là xúc phạm hay vi phạm để hội thoại có nhịp tự nhiên.
11. Là người chơi, tôi muốn Lan yêu cầu làm rõ khi lời nói khó hiểu để tránh bị phạt do STT nghe sai.
12. Là người chơi, tôi muốn phủ định lời hứa hoàn tiền được phân biệt với hứa hoàn tiền để được đánh giá đúng.
13. Là người chơi, tôi muốn việc trích lời khách được phân biệt với cam kết của tôi để không nhận nhầm vi phạm.
14. Là người chơi, tôi muốn việc tự sửa lời ngay trong một lượt được xét theo ý cuối cùng để có thể sửa sai.
15. Là người chơi, tôi muốn rút lại một cam kết sai ở lượt sau để có cơ hội tiếp tục giải quyết khiếu nại.
16. Là người chơi, tôi muốn lời thô lỗ, lời lăng mạ và một câu STT mơ hồ được phân biệt để tránh mức phạt không phù hợp.
17. Là người chơi, tôi muốn được hỏi đủ thông tin trước khi kết luận nguyên nhân để thể hiện kỹ năng điều tra.
18. Là người chơi, tôi muốn có lượt trả lời sau câu chất vấn về niềm tin để thể hiện cách khắc phục lỗi tư vấn trước.
19. Là người chơi, tôi muốn điểm phản ánh các kỹ năng đã thể hiện để việc nói dài hoặc lặp câu không tự tăng điểm.
20. Là người chơi, tôi muốn lời kết thúc của Lan khớp với customer trust state để hiểu đúng kết quả.
21. Là người chơi, tôi muốn kết quả cuối trong VR tiếp tục chỉ hiển thị mức tin tưởng để giữ trải nghiệm hiện có.
22. Là người chơi, tôi muốn game nhận biết lúc tôi nói xong để giảm thao tác nộp lượt thủ công.
23. Là người chơi, tôi muốn dùng nút A để kết thúc lượt khi VAD tắt; khi VAD bật, A không nộp bản ghi trong lúc thu.
24. Là người chơi, tôi muốn có thể ngắt lời Lan bằng thao tác rõ ràng để sửa hoặc bổ sung lời mình.
25. Là người chơi, tôi muốn game không thu chính giọng Lan thành lời tôi để tránh bị chấm nhầm.
26. Là người chơi, tôi muốn lỗi micro được phân biệt với việc tôi im lặng để không bị mất niềm tin do thiết bị.
27. Là người chơi, tôi muốn được thử lại khi mạng lỗi để không phải chơi lại toàn bộ phiên.
28. Là người chơi, tôi muốn retry giữ nguyên lượt nói để không bị cộng lượt hoặc phạt hai lần.
29. Là người chơi, tôi muốn vẫn đọc được lời Lan khi Supertonic lỗi để có thể tiếp tục hội thoại.
30. Là người vận hành, tôi muốn dùng cùng một tài khoản OpenRouter để quản lý credentials và chi phí.
31. Là người vận hành, tôi muốn giữ giọng Supertonic hiện tại để không phải chuyển dịch vụ giọng nói.
32. Là người vận hành, tôi muốn biết thời gian STT, Jev, viết lời và phát giọng riêng để xác định phần gây chờ.
33. Là người vận hành, tôi muốn biết chi phí thực tế từng phiên để kiểm soát ngân sách khảo sát.
34. Là người vận hành, tôi muốn phiên cũ giữ nguyên kết quả và phiên bản rubric để có thể so sánh nghiên cứu đúng cách.
35. Là người vận hành, tôi muốn điểm có bằng chứng theo lượt để giải thích một trường hợp bị chấm sai.
36. Là người vận hành, tôi muốn kết quả chưa đủ bằng chứng được báo chưa hoàn tất để không nhận điểm giả.
37. Là người duyệt dữ liệu, tôi muốn xem transcript STT và bản nghe sửa tách biệt để biết nguồn sai lệch.
38. Là người duyệt dữ liệu, tôi muốn nhãn tham chiếu được duyệt độc lập với dự đoán Jev để đo chất lượng đáng tin cậy.
39. Là người duyệt dữ liệu, tôi muốn các mục tiêu 3 và 4 có dữ liệu để đánh giá giải pháp và khôi phục niềm tin.
40. Là người phát triển, tôi muốn Jev chỉ nhận định hành vi để model không tự điều khiển luật game.
41. Là người phát triển, tôi muốn Qwen chỉ viết customer text để giọng nhân vật không trở thành nguồn chấm điểm.
42. Là người phát triển, tôi muốn luật tiến trình và luật điểm dùng cùng bằng chứng để tránh kết quả mâu thuẫn.
43. Là người phát triển, tôi muốn lưu kết quả mỗi giai đoạn xử lý để retry không phải gọi lại phần đã thành công.
44. Là người phát triển, tôi muốn kết quả trả muộn bị loại khỏi giao diện đã đổi phiên để không phát nhầm lời Lan.
45. Là người phát triển, tôi muốn kiểm thử qua API hiện có để bảo vệ toàn bộ hành vi công khai của backend.
46. Là người phát triển, tôi muốn kiểm thử micro và ngắt lời riêng trong Unity để xác nhận hành vi theo frame và thiết bị.
47. Là người phát triển, tôi muốn có công tắc dùng pipeline cũ cho phiên mới để có thể rollback khi khảo sát chưa đạt.
48. Là người phát triển, tôi muốn simulation-run result giữ một terminal result có thẩm quyền để client không ghi đè điểm backend.

## Implementation Decisions

### Pipeline và ranh giới trách nhiệm

1. Pipeline được chọn là audio của người chơi → Sherpa local → Jev qua OpenRouter → bộ luật backend và kế hoạch trả lời → Qwen 3.7 Flash qua OpenRouter → kiểm tra customer text → lưu kết quả lượt → Supertonic local → Unity. Không cần tài khoản trực tiếp của Typesafe hoặc Alibaba cho các route này.
2. Jev dùng model `typesafe/jev-1.13` tại `POST https://openrouter.ai/api/alpha/decisions`. Qwen dùng `qwen/qwen3.7-flash` tại `POST https://openrouter.ai/api/v1/chat/completions`. Không dùng `typesafe/jev-router` thay classifier và không gộp Decisions của OpenRouter với sản phẩm Decisions của OpenAI.
3. Tách adapter Jev, bộ luật đánh giá, bộ lập kế hoạch phản hồi và bộ viết lời Lan khỏi trách nhiệm quản lý session. Giữ các điểm thay thế dịch vụ STT/responder đang có để kiểm thử ở mức API. Không đổi dịch vụ LLM chung của các simulation khác khi cấu hình Sales mới.
4. Jev nhận final transcript hiện tại cùng ngữ cảnh cần thiết để hiểu chủ thể, phủ định, thông tin đã hỏi và chính sách. Chỉ hành vi của người chơi trong lượt hiện tại tạo bằng chứng mới. Lời Lan và lịch sử chỉ cung cấp ngữ cảnh. Qwen không quyết định cờ, điểm, objective, fact ID hoặc kết thúc phiên.
5. Backend giữ store policy, returning customer profile và actual cause làm dữ liệu có cấu trúc, có phiên bản. Chuẩn hóa ánh xạ fact ID của backend và Unity. Hiện hai phía đang dùng tên fact khác nhau, không được coi các mảng này là đã tương thích. Mua ba ngày trước, đổi trong bảy ngày khi giày nguyên vẹn và các điều kiện đã được tác giả xác định phải nhất quán. Không tự thêm yêu cầu còn tem hoặc chưa sử dụng, vì Lan đã mang giày và những yêu cầu đó không có trong chính sách hiển thị hiện tại.

### Nhận định hành vi và bằng chứng

6. Mở rộng bộ câu hỏi Jev từ tám câu khảo sát thành danh mục có định nghĩa cho đầy đủ hành vi cần chấm và điều khiển game. Mỗi nhãn lưu giá trị `noul` nguyên trạng, trạng thái chấp nhận `true`, `false` hoặc `uncertain`, mã định nghĩa và phiên bản ngưỡng. Không thay trường thiếu bằng `false` và không gọi giá trị `noul` là độ tin cậy đã hiệu chỉnh.
7. Tách hành vi độc lập khỏi nhãn suy ra. Nhận định xin lỗi, công nhận cảm xúc, hỏi thông tin, đề xuất đổi, nêu điều kiện đổi, giải thích nguyên nhân, trách nhiệm lần trước, lời hứa tiền, xúc phạm và chuyển quản lý riêng. `apologyOnly`, tính phù hợp của giải pháp và duy trì lời hứa sau chất vấn được backend suy ra khi có đủ căn cứ. Trường `policyExchange` hiện có ý nghĩa không đồng nhất giữa prototype và backend, nên phải tách "đề xuất đổi" với "nêu đúng điều kiện đổi" trước khi adapter tương thích schema cũ.
8. Dùng câu hỏi Jev riêng cho từng vi phạm cần phân biệt. Không suy ra hoàn tiền, giảm giá hoặc bồi thường chỉ từ một nhãn `unauthorizedPromise` tổng. Phân biệt lời khách, trích dẫn, phủ định, đề nghị có điều kiện và lời tự sửa. Không yêu cầu Jev trả evidence span hoặc JSON ngoài hợp đồng đã kiểm chứng. Bằng chứng tối thiểu là turn ID, transcript tham chiếu, mã câu hỏi, kết quả và ngữ cảnh phiên bản hóa.
9. Đề xuất ngưỡng ban đầu để thử: chấp nhận hành vi thông thường khi `noul >= 0,80`, loại khi `noul <= 0,20`; nhãn dẫn tới phạt hoặc kết thúc thất bại dùng các mốc 0,90 và 0,10. Phần giữa là `uncertain`. Các mốc này là tham số thử nghiệm, phải hiệu chỉnh theo từng nhãn trên dữ liệu đã duyệt trước khi bật chấm chính thức. Không lấy ngưỡng 0,50 của khảo sát làm mặc định production.
10. Mơ hồ về hành vi quan trọng dẫn tới yêu cầu làm rõ, không trừ điểm hoặc kết thúc mất niềm tin. Lưu lượt và nguyên nhân mơ hồ để duyệt. Cho tối đa hai lượt làm rõ liên tiếp cho cùng vấn đề; nếu vẫn chưa quyết định được, đánh dấu assessment cần duyệt thay vì tự đặt điểm 0. Nhãn không liên quan quyết định hiện tại có thể để unknown và không cản trở hội thoại.
11. Rà và thay đường ghi đè từ khóa hiện tại. Giữ kiểm tra cấu trúc, fact allowlist và tính nhất quán luật. Một từ như "đổi", "quản lý" hoặc "đau" không đủ tạo bằng chứng ngữ nghĩa. Cụm "kệ bà" là trường hợp cần định nghĩa và duyệt về thiếu tôn trọng; không đồng nhất tự động với lăng mạ để áp cùng hình phạt. Unknown không được bị luật từ khóa chuyển thành vi phạm chắc chắn.

### Tiến trình hội thoại và lời Lan

12. Giữ bốn conversation objective theo thứ tự: xử lý cảm xúc; tìm actual cause; đề xuất giải pháp; khôi phục niềm tin. Backend tích lũy bằng chứng giữa các lượt cùng mục tiêu. Một lượt có thể chứa nhiều hành vi, nhưng chỉ chuyển tối đa một mục tiêu. Ý nói sớm cho mục tiêu sau được lưu làm ngữ cảnh, cần được xác nhận lại khi đã có dữ kiện phù hợp.
13. Mục tiêu 1 hoàn thành khi đã công nhận cảm xúc hoặc xin lỗi và có câu hỏi mở đúng vấn đề. Mục tiêu 2 cần Lan đã thực sự cung cấp thông tin về việc đi bộ và độ vừa hoặc sở thích giày nhẹ, sau đó người chơi xác định sự không phù hợp của giày nặng với nhu cầu. Không cho hoàn thành nhờ nhắc actual cause chưa được điều tra. Mục tiêu 3 cần đề xuất đổi đúng quyền hạn sang giày nhẹ phù hợp đi bộ và kiểm tra độ vừa hoặc đi thử. Mục tiêu 4 cần câu trả lời sau trust challenge thể hiện trách nhiệm lần trước, lý do phù hợp hơn và cách kiểm chứng.
14. Kết quả restored vẫn cần hoàn thành mục tiêu 4 sau trust challenge; không được kết thúc ngay lượt vừa phát câu chất vấn. Theo lựa chọn người dùng ngày 2026-10-05, Lan có thể chấp nhận đổi và kết thúc ở mục tiêu 2 khi đã hoàn thành xử lý cảm xúc, đã nghe ít nhất một dữ kiện về vấn đề, và lượt hiện tại xác nhận cả đề nghị đổi lẫn điều kiện đổi đúng chính sách. Không áp dụng khi có hành vi xấu, vi phạm, cam kết sai chưa rút lại hoặc nhãn critical chưa rõ. Nhánh này dùng endingReason=exchange_accepted, resolutionAccepted=true và trustState=partially_restored; không tự công nhận tìm đúng nguyên nhân, giải pháp phù hợp hay khôi phục hoàn toàn niềm tin. Không cố định kết thúc ở lượt 4. Nếu nguyên nhân đã được xác định và mục tiêu 2 hoàn thành, tiếp tục đường giải pháp và trust challenge.
15. Giới hạn mặc định tối đa tám lượt có thể đánh giá, thay bốn lượt. Theo yêu cầu cập nhật ngày 2026-10-04, tám lượt là upper limit, không phải số lượt bắt buộc; không dành thêm lượt thứ chín. Backend trả giới hạn và số lượt còn lại để Unity dùng cùng giá trị. Hoàn thành mục tiêu hoặc có ending rõ ràng thì kết thúc ngay dù còn lượt. Nếu giải pháp vừa hoàn thành ở lượt cuối, kết thúc theo rubric hiện tại thay vì đưa ra trust challenge không còn lượt để trả lời. Lượt lỗi kỹ thuật và lượt làm rõ do mơ hồ không dùng ngân sách đánh giá. Lời xác nhận trung tính hợp lệ vẫn dùng một lượt, nhưng không tự thành lượt xấu. Giữ tối đa 45 giây cho một lượt; chưa thêm giới hạn thời gian toàn phiên. Thời gian chờ mạng và customer speech không chạy đồng hồ lượt người chơi.
16. Backend lập kế hoạch trả lời gồm ý định, mức cảm xúc, dữ kiện bắt buộc, facts được phép nói mới, facts đã biết có thể nhắc lại, điều chưa giải quyết, câu hỏi cần hỏi và loại kết thúc nếu có. Chỉ tiết lộ dữ kiện được phép ở mục tiêu hiện tại và phù hợp câu hỏi thực tế. Dữ kiện đã biết có thể được nhắc lại để xác nhận mà không bị coi là tiết lộ mới.
17. Qwen nhận kế hoạch, final transcript hiện tại và tối đa ba cặp lượt gần nhất. State có cấu trúc giữ các thông tin quan trọng lâu hơn cửa sổ này. Qwen viết 1–2 câu, tối đa 55 từ và 600 ký tự, giữ chị/em, không nói điểm, rubric, tên model hoặc hướng dẫn người chơi đọc đáp án. Lan trả lời phần được hỏi trước và chỉ hỏi tối đa một điều còn thiếu. Trust challenge có thể đổi cách nói nhưng phải giữ ý nghi ngại và cơ hội chứng minh.
18. Giữ điều khiển cảm xúc bằng luật backend, không thêm model đo cảm xúc từ giọng. Lan có thể từ khó chịu chuyển sang thận trọng hoặc cởi mở theo bằng chứng đã xác nhận. Thiếu tôn trọng làm Lan cứng rắn hơn, không tự kết thúc chỉ vì Qwen viết câu giống một câu kết thúc. Loại bỏ mọi logic suy kết thúc từ so sánh chuỗi customer text.
19. Kiểm tra định dạng, độ dài, nội dung bắt buộc, xưng hô, echo transcript và dấu hiệu thêm dữ kiện hoặc chính sách trước khi đưa vào customer speech. Kiểm tra tự động không bảo đảm tuyệt đối tính đúng ngữ nghĩa của văn bản tự do, nên cần đánh giá người duyệt và bộ tình huống. Nếu lời sinh bị từ chối, cho một lần viết lại có giới hạn thời gian, sau đó dùng câu an toàn từ kế hoạch. Không phạt người chơi vì lỗi viết lời. Câu dự phòng phải luôn khả dụng, không lỗi khi dùng hết một nhóm biến thể.
20. Chưa phát customer speech từ token Qwen chưa được duyệt. Có thể dùng streaming nội bộ để đo time-to-first-token nếu route hỗ trợ, nhưng chỉ lưu và phát toàn bộ customer text đã hợp lệ. Những nâng cấp này là hội thoại phản hồi nhanh theo lượt, không phải speech-to-speech full duplex.

### Chấm điểm và customer trust state

21. Thay lời gọi LLM chấm điểm cuối của pipeline mới bằng bộ tính rubric xác định từ sổ bằng chứng. Qwen không sinh điểm hoặc feedback đánh giá. Jev cũng không sinh tổng điểm. Nếu có hành vi quan trọng chưa phân giải có thể thay đổi kết quả, giữ assessment pending hoặc needs-review; không công bố terminal result có điểm giả. Completion kiểm tra tối đa 64 tổ hợp của nhãn bất định bằng luật hiện tại, giữ cả vi phạm và context lịch sử. Nếu mọi khả năng cho cùng kết quả, ghi nhận outcome_invariant và hoàn tất mà không đổi nhãn gốc. Nếu còn ảnh hưởng, phân xử lại transcript/context gốc một pass Luna, tối đa tám nhãn và một deadline chung; retry complete không gọi lặp. Quote khác dấu câu được nhận chỉ khi chuỗi từ liên tiếp, dấu tiếng Việt và phủ định còn nguyên; nhãn sai quote được repair một lần trong deadline, lưu lý do từ chối. Khung hai tiêu chí 0–50 và `score` 0–100 được giữ để tương thích dữ liệu phân tích.
22. Đề xuất rubric v2 dưới đây. Mỗi hạng mục chỉ nhận 0 hoặc toàn bộ số điểm khi có bằng chứng được chấp nhận; không tự chấm nửa điểm. Bằng chứng được cộng một lần cho cả phiên, không nhân với số lần nhắc lại. Điểm và cờ mục tiêu được tính theo ngữ cảnh, không chỉ OR tất cả nhãn.

| Tiêu chí | Hành vi có bằng chứng | Điểm tối đa |
| --- | --- | ---: |
| `apologyAndPolicyRemedy` | Công nhận thất vọng hoặc xin lỗi phù hợp | 10 |
| `apologyAndPolicyRemedy` | Nhận trách nhiệm về thiếu sót tư vấn lần trước | 10 |
| `apologyAndPolicyRemedy` | Đề xuất đổi và nêu đúng điều kiện chính sách | 10 |
| `apologyAndPolicyRemedy` | Đề xuất mẫu nhẹ phù hợp nhu cầu đi bộ đã biết | 10 |
| `apologyAndPolicyRemedy` | Đề nghị kiểm tra độ vừa hoặc đi thử | 10 |
| `adaptabilityAndDeescalation` | Hỏi phù hợp về cách dùng hoặc thời gian dùng | 10 |
| `adaptabilityAndDeescalation` | Hỏi phù hợp về độ vừa, tình trạng hoặc sở thích liên quan | 10 |
| `adaptabilityAndDeescalation` | Xác định đúng actual cause sau điều tra | 10 |
| `adaptabilityAndDeescalation` | Dùng thông tin Lan đã nói để giải thích cách khắc phục | 10 |
| `adaptabilityAndDeescalation` | Đáp trust challenge bình tĩnh và nêu cách kiểm chứng | 10 |

23. `rawScore` là tổng hai tiêu chí. `policyViolationPenalty = min(rawScore, 10 × số sự kiện vi phạm được xác nhận)`. `score = rawScore - policyViolationPenalty`. Chỉ vi phạm quyền hạn hoặc lăng mạ đã được định nghĩa mới tạo sự kiện phạt. Xin lỗi thiếu giải pháp, câu trung tính, hỏi lặp, nhận định sai nguyên nhân hoặc đề xuất chưa đủ không tạo thêm khoản phạt 10 điểm. Chúng có thể chưa đạt mục tiêu hoặc chưa nhận điểm tương ứng.
24. Sự kiện vi phạm định danh bằng session ID, turn ID và mã vi phạm. Cùng một câu lăng mạ xuất hiện trong `abuse` và `profanityOrInsult` chỉ là một sự kiện `abusive_language`. Retry không tạo sự kiện mới. Một lời hứa cùng loại được nói lại rõ ràng ở lượt mới có thể là sự kiện mới; chỉ cờ "vẫn chưa rút lại" không tự sinh thêm khoản phạt. Bằng chứng tự sửa trong cùng lượt được xét theo vị trí cuối cùng. Rút lại ở lượt sau giải quyết trạng thái cam kết đang tồn tại nhưng không xóa sự kiện đã xảy ra.
25. Tách chất lượng lượt thành `good`, `bad`, `neutral`, `uncertain`. Xin lỗi đầu tiên có thể tạo tiến bộ cảm xúc mà chưa đủ giải pháp. Xác nhận ngắn là neutral. Hỏi làm rõ khác với hỏi lặp thông tin đã trả lời. Good/bad counts giữ lại cho phân tích lịch sử, không còn quyết định customer trust state. `playerResponseRating` trả trực tiếp good/bad/neutral/uncertain khi có kết quả phân loại dùng được. Bất định ngữ nghĩa trả uncertain; null dành cho lỗi hoặc thiếu toàn bộ kết quả phân loại, hay lượt chưa phân loại. Luna lỗi không xóa bất định Jev đã trả hợp lệ. Một hành vi bad chắc chắn vẫn trả bad dù nhãn khác uncertain, với assessmentUncertain=true. Unity giữ nguyên chuỗi rating. Không cộng good/bad counts cho neutral hoặc uncertain, và không sửa bản ghi lịch sử.
26. Quy tắc kết quả v2: `restored` khi hoàn thành cả bốn mục tiêu, đã trả lời trust challenge, `score >= 70`, không còn cam kết trái quyền hạn chưa rút lại và không có ending mất niềm tin. `partially_restored` khi đã công nhận vấn đề, hoàn thành mục tiêu giải pháp, `score >= 40` và không có ending bắt buộc lost; hoặc Lan chấp nhận đổi theo điều kiện ở mục 14. Nhánh chấp nhận đổi cộng điểm policy đúng một lần, có thể đạt partially_restored dưới 40 điểm vì khách chấp nhận cách xử lý dù người chơi chưa chứng minh đủ kỹ năng. Các trường hợp đánh giá hoàn chỉnh còn lại là `lost`. Ánh xạ `customerRating` tương ứng thành good, considering, bad. Lỗi hạ tầng và nhãn critical chưa rõ không tự đi vào ba nhánh này.
27. Giữ quy tắc chuyển quản lý thật sự của người chơi hoặc duy trì lời hứa ngoài quyền hạn sau khi Lan chất vấn là ending lost. Lần đầu hứa sai tạo vi phạm và câu chất vấn, cho cơ hội rút lại. Lăng mạ rõ ràng tạo phạt và cảnh báo, chưa tự kết thúc. Hai lượt im lặng hợp lệ do người chơi không nói kết thúc lost; PCM toàn số 0, micro hỏng, STT lỗi hoặc upload lỗi là lỗi kỹ thuật. Ending được backend quyết định trước khi Qwen viết lời.
28. Điểm cuối, `trustState`, `customerRating`, các cờ emotionalHandling/causeIdentification/solutionSuitability/trustRebuilding và kế hoạch câu kết thúc phải cùng xuất phát từ rubric v2 và bằng chứng. Một ending lost không xóa các kỹ năng đã đạt, vì score và trust đo các khía cạnh khác nhau; ghi lý do ending để giải thích. Không cho lần hoàn tất sau ghi đè ending hoặc kết quả đã cố định. `natural` completion không được dùng để tuyên bố thành công sau một lượt chưa xong mục tiêu; nếu client yêu cầu dừng sớm, chốt kết quả chưa hoàn thành theo rubric và lý do rõ ràng.

### Thu âm, ngắt lời và lỗi dịch vụ

29. Dùng lại hướng microphone session đã có trong tracker, tránh tạo chủ sở hữu micro thứ hai. Xác nhận PCM thực sự có dữ liệu trước khi đưa thu âm vào pipeline. Thu liên tục local và cắt đoạn theo lượt không đồng nghĩa truyền audio liên tục lên cloud. Khi bắt đầu mục tiêu hoặc resume, giữ kiểm tra quyền, thiết bị, pause và bộ đệm.
30. Bổ sung phát hiện kết thúc lời local bằng VAD với thời gian im lặng có thể cấu hình. Chờ 5 giây im lặng sau khi đã có lời, dùng pre-roll ngắn để không mất đầu câu. Giữ giới hạn 45 giây; nút A chỉ nộp khi VAD tắt. Ngưỡng RMS tiếp tục lời thấp hơn ngưỡng bắt đầu để giữ đoạn nói nhỏ. Lỗi bản ghi có thể thu lại không làm hủy session; backend giải phóng checkpoint và Unity mở lượt thu mới mà không phát incomplete. Không tự nộp ngay một khoảng lặng trước khi người chơi bắt đầu nói. Phải đo trên giọng tiếng Việt và headset trước khi bật mặc định.
31. Bước ngắt lời đầu dùng thao tác rõ ràng: khi Lan đang phát giọng, người chơi nhấn nút để dừng audio rồi bắt đầu lượt mới. Không chạy VAD thu lời người chơi trên audio đang phát từ loa khi chưa kiểm chứng chống vọng. Ngắt lời tự động bằng giọng và khử vọng đầy đủ là phần nâng tiếp, không phải điều kiện giao bản đầu.
32. Ngắt lời customer speech không rollback lượt người chơi đã accepted hoặc chấm lại điểm. Hủy tải/phát audio cũ và bỏ phản hồi giao diện trả muộn bằng session generation, turn ID và speech ID. Khóa tiếp nhận lượt mới trong khi backend chưa commit lượt hiện tại; hiển thị trạng thái chờ, không chạy hai quyết định state đồng thời. Nếu text đã hiển thị, facts đó được coi là đã trình bày cho người chơi dù audio bị ngắt; Unity cần giữ text để người chơi tham chiếu.
33. Mỗi lượt có các mốc nhận audio, có transcript, nhận định, lập kế hoạch, viết lời, commit và phát giọng. Lưu checkpoint hợp lệ để retry dùng cùng turn ID và audio hash. Backend chỉ commit trạng thái, điểm, lời Lan và sự kiện phạt một lần. Đề xuất deadline riêng 3 giây cho Jev, 4 giây cho viết lời, giữ deadline Supertonic hiện có; cấu hình và hiệu chỉnh sau phép đo. Retry mạng có giới hạn trong deadline, không lồng nhiều vòng retry làm thời gian chờ tăng vô hạn.
34. Jev lỗi thì giữ audio/checkpoint, báo thử lại và không cập nhật điểm hay objective. Qwen lỗi thì dùng câu backend an toàn để hoàn tất lượt đã nhận định. Supertonic lỗi thì hiển thị customer text và tiếp tục bằng chữ. Không tự dùng model local chấm thay Jev giữa một phiên đang khảo sát. Các tình huống này có mã lỗi và trạng thái riêng, không xuất hiện như lỗi kỹ năng trong assessment.

### Cấu hình, dữ liệu và triển khai

35. Backend sở hữu OpenRouter key, dùng chung key cho Jev và Qwen. Credentials hiện nằm trong cấu hình backend NCKH-Web; lúc triển khai phải cấp cho tiến trình NCKH-AI qua environment hoặc secret injection rõ ràng. Không đọc key từ máy client, không hard-code đường dẫn workspace bên cạnh, không đóng gói trong Unity và không đưa vào log. Chỉ gửi ngữ cảnh hội thoại cần thiết, bỏ tên thật, run identifier không cần thiết và audio khỏi request phân loại/viết lời.
36. Đóng băng `pipelineVersion`, `rubricVersion`, `scenarioVersion`, `questionSetVersion`, `thresholdVersion` và prompt version từ lúc tạo phiên. Lưu model yêu cầu và model thực tế trả về, provider, request ID, usage và mốc thời gian khi có. Không ghi phiên bản không được API xác nhận thành phiên bản giả. Phiên đã bắt đầu hoặc kết thúc theo pipeline cũ tiếp tục dùng luật cũ; không tự chấm lại dữ liệu nghiên cứu lịch sử.
37. Giữ routes tạo/resume session, gửi turn, speech lookup và complete hiện có. Bổ sung trường phiên bản, chất lượng lượt, trạng thái đánh giá, giới hạn lượt và lý do ending theo hợp đồng client/backend thống nhất. `customerText` đã commit là nguồn cho phụ đề và speech ID. Cùng turn ID với audio khác vẫn bị từ chối; cùng completion ID hoặc completion retry trả kết quả đã lưu.
38. Giữ một simulation-run result cho mỗi run theo ADR hiện tại. Backend Part 2 assessment là terminal result có thẩm quyền; fragment Unity không ghi đè score bằng giá trị mặc định. Kết quả pending không được đồng bộ như finalized. Việc lưu phiên bản đánh giá là bổ sung provenance có chủ đích, cần cập nhật hợp đồng aggregate cùng backend NCKH-Web trước khi bật. Không gộp điểm Part 1 với Part 2 hoặc đổi tác vụ chấm Part 1 trong đợt này.
39. Giữ retention, xóa diagnostics, tombstone và phân tách token gameplay/diagnostic đang có. Sổ bằng chứng sau khi xóa diagnostics chỉ giữ nguồn tham chiếu và các dữ kiện tổng hợp được chính sách cho phép, không giữ lại transcript dưới tên trường mới. Tiếp tục chạy một worker khi còn file-backed store; nhiều worker cần khóa liên tiến trình hoặc transactional store trước khi mở rộng.
40. Sửa mô tả kiến trúc ADR về phân tích cuối: pipeline mới dùng bộ luật trên nhận định Jev thay LLM chấm cuối, vẫn giữ phân tách nhân vật và đánh giá. Giữ HTTP theo lượt cho bản đầu. Ghi rõ thao tác dừng playback local chưa biến giao thức thành full duplex; bất kỳ thay đổi truyền audio hai chiều liên tục phải có quyết định kiến trúc riêng.
41. Triển khai theo thứ tự: chuẩn hóa chính sách, nhãn và gold set; tích hợp Jev cùng luật điểm ở chế độ so sánh không điều khiển phiên; thêm Qwen writer và fallback; hoàn thiện rubric, mục tiêu 4 và hợp đồng Unity; nâng thu âm/VAD/ngắt playback; chạy khảo sát thực trước khi bật pipeline mới. Cho phép bật/tắt pipeline cho phiên mới, không đổi model/rubric giữa phiên.

## Testing Decisions

Người dùng đã xác nhận ưu tiên API hội thoại làm điểm kiểm thử chính. Một test tốt quan sát session, transcript đã lưu, customer text, objective, điểm và terminal result qua API; không khóa test vào lời gọi helper hoặc cấu trúc lớp. Mỗi biến thể câu nói phải kiểm tra tác động bên ngoài thay vì chỉ xác nhận cờ nội bộ.

1. Mở rộng các HTTP test hiện có cho tạo phiên, gửi Unity-shaped audio, đọc session, speech lookup và complete. Thay STT, Jev, writer và Supertonic ở ranh giới dịch vụ bằng dữ liệu xác định. Giữ prior art về idempotency, xóa diagnostics trong lúc xử lý, auth, fact gating và projection vào simulation-run result.
2. Kiểm thử một phiên restored có nhiều lượt điều tra, câu trust challenge và câu đáp sau challenge; restored không được bỏ qua mục tiêu 4. Kiểm tra giới hạn tám lượt không phát challenge thiếu lượt trả lời. Replay run ngày 2026-10-05 phải chấp nhận đổi ở lượt 4, còn bốn lượt nhưng kết thúc với partially_restored; thiếu điều kiện đổi, hành vi xấu hoặc nhãn cần thiết chưa rõ không được tự chấp nhận.
3. Kiểm thử xin lỗi tách khỏi giải pháp, câu trung tính, hỏi mới, hỏi lặp, nhắc lại để xác nhận, phủ định, trích lời khách, tự sửa trong cùng lượt, rút lại ở lượt sau và câu hỏi về quản lý khác với thật sự chuyển quản lý.
4. Kiểm thử nhãn uncertain, field thiếu, response malformed, xác suất ngoài miền và schema cũ. Không phạt từ lỗi này. Kiểm tra giới hạn hai lần làm rõ và trạng thái assessment cần duyệt.
5. Kiểm thử rubric bằng các phiên có kết quả mong đợi, mỗi thành phần chỉ cộng một lần. Cùng transcript/bằng chứng và phiên bản cho cùng điểm bất kể Qwen viết lời khác. Lặp lại lời tốt không tăng rawScore; retry không tăng accepted count, penalty hoặc usage cho checkpoint đã cache.
6. Kiểm thử trùng abuse/profanity chỉ phạt một lần, nhiều loại vi phạm độc lập được tách, ending lost ưu tiên ngưỡng và finalCustomerText không thể tự quyết định kết thúc. Điểm số, customerRating và trustState phải khớp quy tắc v2, kể cả dừng sớm và im lặng thật.
7. Kiểm thử Qwen viết quá dài, echo lời người chơi, thêm chính sách sai, tiết lộ facts chưa cho phép, trả hướng dẫn chấm điểm và lỗi dịch vụ. Backend dùng phản hồi dự phòng hợp lệ, không sửa assessment của người chơi. Test không yêu cầu khớp nguyên văn lời Lan khi ý có thể diễn đạt khác nhau.
8. Kiểm thử timeout Jev, Qwen và Supertonic riêng; mất mạng sau từng checkpoint; restart backend trước/sau commit; response trả muộn sau đổi phiên; completion retry sau ending. Xác nhận không có terminal result giả do lỗi hạ tầng.
9. Unity Edit Mode kiểm tra hợp đồng và giới hạn lượt. Play Mode kiểm tra trạng thái thu âm/phát giọng, thao tác A, dừng audio, bỏ response cũ, pause/resume và đồng hồ ngừng khi chờ. Dùng các điểm thay micro/request đã có. Nối với spec microphone session hiện hữu thay vì tạo thêm hệ kiểm thử micro trùng lặp.
10. Chạy khảo sát thật riêng trên mạng và headset đích. Tách audio → transcript, transcript đúng → Jev, kế hoạch cố định → Qwen và toàn bộ lượt → customer speech. Không lấy latency Jev hoặc provider P50 làm độ trễ game. Không coi test tự động là đã kiểm chứng headset, chống vọng hoặc độ tự nhiên.

### Tiêu chí nhận bản thử và bật chấm chính thức

- Bản thử phải vượt tất cả kịch bản API bắt buộc, có điểm/rubric truy vết được và không còn đường restored bỏ qua trust challenge. Chấp nhận đổi sớm chỉ là partially_restored, giữ riêng các kỹ năng chưa chứng minh.
- Chuẩn bị khoảng 300–500 lượt có ngữ cảnh đã được người duyệt gán nhãn, bổ sung mục tiêu 3/4 và các trường hợp dễ phạt nhầm. Cố gắng có ít nhất 30–50 ví dụ dương cùng âm dễ nhầm cho mỗi nhãn ưu tiên; đây là kế hoạch thu thập, không phải bảo đảm chất lượng. Nếu thiếu độ phủ, giữ chế độ so sánh cho nhãn đó.
- Tách calibration và holdout theo phiên hoặc người nói đã xác nhận; giữ biến thể và transcript sửa của một nguồn cùng tập. 40 lượt Jev đã khảo sát là dữ liệu phát triển, không làm holdout độc lập. Không dùng nhãn hệ thống cũ như gold.
- Báo precision/recall/F1 theo nhãn, exact match theo lượt, tỷ lệ uncertain, tỷ lệ được quyết định, phạt nhầm và kết thúc nhầm. Mục tiêu thử cho nhãn phạt là precision ít nhất 98% trên phần được quyết định và không có ending sai trong bộ tình huống bắt buộc. Báo cả số mẫu và độ bất định; zero lỗi trên ít mẫu không chứng minh đạt 98%. Nếu chưa đủ bằng chứng thì tiếp tục so sánh, chưa tự bật phạt nhãn đó.
- Người duyệt đọc ít nhất 50 lời Qwen ở ngữ cảnh đa dạng. Mục tiêu ít nhất 90% đạt rubric về đáp đúng lời vừa nghe, không hỏi lặp vô cớ, giữ vai Lan và câu nói tự nhiên. Mọi trường hợp bịa chính sách hoặc làm lộ đáp án được sửa và chạy lại bộ kiểm tra trước khi phát hành. Đây là tiêu chí đề xuất, chưa có kết quả đo.
- Đo ít nhất 100 lượt cho p50/p95 từ lúc người chơi kết thúc câu đến khi Lan bắt đầu nói. Mục tiêu trải nghiệm ban đầu p50 không quá 2,5 giây và p95 không quá 5 giây trên cấu hình đích; báo riêng STT, Jev, Qwen, lấy WAV và playback. Nếu không đạt, xác định nút thắt và thử lại; không tuyên bố đã đạt từ benchmark văn bản.
- Ghi token, usage.cost và tổng chi phí thực trên cùng các phiên thử. Báo tỷ lệ fallback, retry và lỗi. Không đặt budget production từ tám câu hỏi Jev khi chưa đo schema đầy đủ.
- Kiểm tra trực tiếp headset cho quyền micro, PCM hợp lệ, mất tracking, pause/resume, tiếng Việt, độ trễ, tiếng Lan lọt micro, thao tác ngắt lời và frame rate. Bản đầu chưa yêu cầu tự ngắt lời bằng giọng.

## Out of Scope

- TTS cloud, đổi giọng Supertonic, native speech-to-speech hoặc thêm tài khoản nhà cung cấp trực tiếp.
- Thay STT mặc định sang cloud trong bản đầu. Có thể thử OpenRouter transcription ở một đặc tả tiếp theo sau khi so cùng audio với Sherpa.
- Fine-tune classifier local từ kho dữ liệu hiện có hoặc dùng nó làm fallback chấm điểm tự động trong phiên Jev.
- Tích hợp OpenAI Decisions preview chưa có hợp đồng và chi phí xác nhận cho dự án này.
- Audio streaming hai chiều liên tục, Lan ngắt lời người chơi, ngắt lời tự động bằng giọng và một hệ khử vọng đầy đủ.
- Đổi scoring Part 1, đổi tình huống giày, thêm customer profile mới, dashboard mới hoặc sửa scoring của các simulation khác.
- Công bố độ chính xác tổng quát từ 40 mẫu, tự biến nhãn cũ thành gold, hoặc chấm lại và ghi đè kết quả nghiên cứu lịch sử.
- Triển khai nhiều worker dùng chung file store hoặc migration MongoDB lớn ngoài phần provenance cần cho hợp đồng đánh giá.

## Further Notes

Khảo sát ngày 03/10/2026 đã gọi Jev cho 10 mẫu chọn có chủ đích và 30 mẫu bổ sung. Tổng 40/40 request thành công, khớp 315/320 nhận định với tham chiếu, 35/40 lượt khớp cả tám nhãn. Median gộp khoảng 402 ms. Riêng 30 mẫu bổ sung khớp 235/240 nhận định, 25/30 lượt; median khoảng 410 ms. Tham chiếu do assistant rà transcript trước request, chưa có người chấm độc lập. Các bất đồng chủ yếu thuộc đề nghị thử giày và ranh giới lăng mạ. Những số này không đo toàn bộ STT, writer, Supertonic hoặc pipeline mới.

Kiểm tra model local trước đó dùng 11 tình huống văn bản, median khoảng 4,54 giây. Hai khảo sát có đầu vào, schema và công việc backend khác nhau, nên không suy ra tỷ lệ tăng tốc công bằng từ hai median.

Theo trang OpenRouter kiểm tra cùng ngày, Qwen 3.7 Flash có mức giá ngắn-context tham khảo 0,03 USD/triệu token input và 0,13 USD/triệu token output; Jev 1.13 có input 0,042 USD/triệu token, output miễn phí. Với 10 lượt, writer 800 input và 100 output token mỗi lượt, cộng Jev theo trung bình khảo sát tám câu hỏi, ước tính khoảng 0,00106 USD/phiên. Schema đầy đủ, lịch sử, token reasoning, retries, provider và giá thay đổi có thể làm chi phí khác. Model Qwen có `response_format` nhưng không bảo đảm JSON-schema enforcement; backend vẫn phải kiểm tra kết quả. Nguồn: [Qwen 3.7 Flash](https://openrouter.ai/qwen/qwen3.7-flash), [Jev 1.13](https://openrouter.ai/typesafe/jev-1.13).

Tài liệu nền đã đối chiếu gồm glossary backend và Unity, đặc tả returning-customer conversation, ADR về HTTP theo lượt, ADR về một simulation-run result, cấu hình và code session hiện tại, test API hiện có, spec microphone session trong tracker, báo cáo dữ liệu classifier, kết quả Jev và khảo sát pipeline OpenRouter. Code đang có các thay đổi chưa commit của người dùng; đặc tả này không sửa chúng.

Các quyết định thiết kế cần kiểm chứng qua bản thử gồm rubric 10 hạng mục, ngưỡng Jev theo nhãn, ngưỡng trust 70/40, ngân sách tám lượt, VAD 5 giây và mục tiêu latency. Có thể hiệu chỉnh giữa các đợt triển khai, phải tăng phiên bản tương ứng và không thay luật giữa phiên.
