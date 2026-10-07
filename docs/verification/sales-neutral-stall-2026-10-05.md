# Lặp fallback và hội thoại kéo dài

Nguồn là session `25bf01d8661448bdb8c8ce70fcd687f8`, run `a6e8991af208430c887f705eff767867`, cập nhật cuối lúc 15:23:54 ngày 05/10/2026 theo giờ Việt Nam. Run đã finished/completed, score=20, trustState=lost, endingReason=turn_limit. Không có lỗi needs-review hay kết quả Luna bị từ chối trong run này.

Phần chẩn đoán dưới đây mô tả luật v2.4/v2.7 của run nguồn. Phần cuối ghi thay đổi đã triển khai theo lựa chọn người dùng và kết quả kiểm tra phiên bản mới.

## Các lượt liên quan

| Lượt | Rating | Mục tiêu sau lượt | Phản hồi và đường xử lý |
| --- | --- | --- | --- |
| 1 | bad | 1 | Cảnh báo lời nói thiếu tôn trọng, writer hợp lệ |
| 2 | good | 1 | Xin lỗi, đề nghị đổi và nêu chính sách đúng; chưa có câu hỏi mở |
| 3 | good | 2 | Hỏi vị trí đau; Lan công bố pain_location |
| 4 | neutral | 2 | Đề nghị cùng kiểu giày, tăng size; fallback investigate_walking |
| 5 | neutral | 2 | Tiếp tục đề nghị đổi dòng giày; fallback investigate_walking |
| 6 | good | 2 | Hỏi nhu cầu đi bộ; Lan công bố walking_routine |
| 7 | neutral | 2 | Đề nghị tăng size; fallback investigate_fit |
| 8 | neutral | 2 | Transcript câu hỏi size mơ hồ; fallback ending_lost vì hết giới hạn |

Lượt 4, 5, 7 cùng mở đầu "Đổi cho chị thì được, nhưng chị chưa yên tâm." Cả bốn lượt neutral, gồm lượt kết thúc, đều fallbackUsed=true. Lượt 4 và 7 có missing_required_content ở cả hai lần viết. Lượt 5 có unallowed_fact và missing_required_content. Lượt 8 có missing_required_content. Không có raw candidate của Qwen để kết luận chính xác câu nào bị loại vì cụm từ nào.

## Nguyên nhân lặp đã xác nhận

Rating neutral không trực tiếp được gửi cho Qwen. Backend suy ra intent/disposition rồi gửi plan. Lượt 4, 5, 7 đều đi vào investigation_followup. Vì exchangeOffer đã tồn tại ở objectiveEvidence mục tiêu 2, plan_reply tiếp tục thêm requiredContent=acknowledge_exchange và cùng prefix, kể cả lượt 7 không có exchangeOffer mới được xác nhận.

Guard acknowledge_exchange yêu cầu từ "đổi" và một danh sách anchor như "được", "em định", "phương án". Nó chưa nhận "đồng ý đổi" hoặc "chấp nhận đổi". investigate_fit chưa nhận trực tiếp các cách hỏi "chật", "rộng", "hỏng". Guard ending_lost cũng chưa nhận cách từ chối tự nhiên chỉ nói "không chấp nhận cách tư vấn" và dừng trao đổi.

Probe offline với câu tổng hợp đúng ý đã tái hiện missing_required_content ở cả ba tình huống. Đây là bằng chứng guard có false rejection, không phải bằng chứng các candidate thật của Qwen dùng đúng các câu probe đó.

Guard lặp hiện chỉ so nguyên văn câu/câu hỏi sau normalize với lời NPC gần nhất. Nó không chặn cùng mệnh đề mở đầu hoặc lặp ý qua nhiều lượt. Vì vậy hai câu hỏi đi bộ đổi cách nói vẫn qua được, và khi lượt 6 trả lời một fact, lượt 7 có thể dùng lại prefix của lượt 5.

## Vì sao không dừng ở lượt 4

Lượt 4 mới xác nhận exchangeOffer và exchangeConditions. Không có causeStatement đúng, lightweightForWalking, câu hỏi về fit hoặc giải thích vì sao giày mới hợp nhu cầu đi bộ. Trước lượt này, facts đã công bố chỉ có pain_location. Sau lượt 6 mới có walking_routine; cuối run vẫn thiếu fit_condition/lighter_preference và causeStatement. Mục tiêu 2 vì vậy không hoàn thành; mục tiêu 3/4 chưa được thực hiện.

Classifier hiện không có tag phân biệt đề nghị sai nguyên nhân hoặc phương án thay thế không phù hợp với chỉ đơn giản chưa đủ giải pháp. Với bộ nhãn này, đề nghị tăng size bị xếp neutral vì không tạo điểm mới và không có hành vi bad đã được nhận dạng. Backend không có ending vì kết luận sai/giải pháp không thuyết phục hoặc bế tắc. Các ending tự động hiện có là hoàn tất bốn mục tiêu, chuyển quản lý, duy trì cam kết sai, hai lượt im lặng và hết giới hạn lượt. Vì vậy giới hạn 8 đang trở thành điểm dừng duy nhất trong run này.

Từ góc độ rubric hiện tại, lượt 4 chưa đủ điều kiện kết thúc thành công. Kết thúc sớm ở lượt này phải có quy tắc mất niềm tin/phương án không phù hợp được xác nhận, hoặc thay đổi yêu cầu đánh giá nếu muốn chấp nhận đề nghị đổi.

## Hướng sửa

- Mở rộng guard để kiểm tra ý bắt buộc mà không ép một số anchor hẹp, vẫn giữ ràng buộc chính sách/facts.
- Chỉ xác nhận đề nghị đổi khi có đề nghị mới hoặc cần phản hồi nó; không bắt đầu mọi followup bằng cùng prefix. Chống lặp cả mệnh đề và ý câu hỏi trong nhiều lượt gần nhất.
- Thêm bằng chứng cho phương án không phù hợp hoặc kết luận thiếu căn cứ nếu muốn chấm bad và dừng sớm vì lý do đó. Dùng Luna xác nhận các trường hợp dễ nhầm, không suy bad từ neutral hoặc từ việc thiếu tag.
- Quy tắc dừng phải dựa vào hành vi và phản ứng của khách, không tự dừng mọi cuộc trò chuyện ở lượt 4 hay sau một số lượt neutral cố định. Câu hỏi có ích vẫn được tiếp tục; 8 lượt vẫn là upper limit.

Replay offline qua evaluate và plan_reply đã khớp rating/conversationComplete của cả tám lượt. Kết quả và probe ở `recordings/evaluations/2026-10-05-dialogue-stall/diagnosis.json`. Không gọi model, không chạy thêm test suite, không thay đổi code gameplay hoặc dữ liệu session nguồn trong đợt chẩn đoán này.

## Thay đổi đã triển khai theo lựa chọn người dùng

Người dùng chọn "Chấp nhận phương án đổi và kết thúc" cho lượt 4. Rubric mới `sales-rubric-v2.5` cho phép ending `exchange_accepted` ở mục tiêu 2 sau khi đã hoàn thành mục tiêu cảm xúc, Lan đã công bố ít nhất một dữ kiện về vấn đề, và lượt hiện tại xác nhận cả exchangeOffer lẫn exchangeConditions. Hành vi xấu, vi phạm, cam kết sai chưa rút lại hoặc critical uncertainty không được tự kích hoạt chấp nhận. Không có luật buộc mọi phiên dừng ở lượt 4. Đề nghị ở lượt 2 của run này chưa đủ vì chưa có bước tìm hiểu vấn đề.

Nhánh này trả resolutionAccepted=true, conversationComplete=true, cộng policy đúng một lần và chốt partially_restored/considering. Việc khách chấp nhận cách xử lý không tự chứng minh causeIdentification, solutionSuitability hoặc trustRebuilding. Các cờ đó vẫn cần objective tương ứng. Đường hoàn thành bốn mục tiêu vẫn cần đáp trust challenge để đạt restored.

Planner chỉ xác nhận đề nghị đổi mới trong mục tiêu điều tra, bỏ prefix chung ở các followup tiếp theo. Guard nhận thêm cách nói "đồng ý đổi", "chấp nhận đổi" và câu hỏi về chật/rộng/hỏng/size. Chống lặp so toàn câu, câu hỏi và mệnh đề từ sáu từ trở lên với ba lời NPC gần nhất; đây chưa phải bộ đo tương đồng ý nghĩa. Prompt mới `lan-writer-v2.8` yêu cầu chấp nhận và kết thúc ở nhánh này, không hỏi thêm hoặc tuyên bố hoàn toàn hài lòng.

## Kết quả kiểm tra

- 136 tests thuộc sales_pipeline_api, sales_openrouter, sales_arbitration và run_results pass trong 50,425 giây. Bao gồm kết thúc ở lượt 4, chặn lượt thứ 5 sau kết thúc, thiếu chính sách, bất định chính sách, từ chối xử lý, hứa sai, guard cách nói tự nhiên, chống lặp và giữ đường đầy đủ qua trust challenge.
- Replay nhãn lưu của run nguồn dưới phiên bản mới kết thúc ở lượt 4, còn bốn lượt. Score=20, trustState=partially_restored, resolutionAccepted=true. causeIdentification/solutionSuitability/trustRebuilding=false. Đây là replay luật mới, không phải chấm lại kết quả lịch sử hay chạy lại STT/classifier.
- Một request Qwen thực qua OpenRouter trả "Được, chị đồng ý đổi theo chính sách đó. Mình xử lý như vậy nhé." Không fallback, không rejection; thời gian writer 2,231509 giây, chi phí được provider báo 0,00004609 USD. Đây là một mẫu writer, không phải số đo toàn pipeline hoặc p95. Sau probe chỉ dọn một đoạn hướng dẫn ending_review bị viết trùng; các yêu cầu chấp nhận đổi giữ nguyên.
- Dữ liệu replay và metadata writer lưu ở `recordings/evaluations/2026-10-05-dialogue-stall/fix-verification.json`. SHA-256 file session nguồn được đối chiếu, nội dung không đổi.

Khởi động lại backend và tạo phiên mới để dùng v2.5/v2.8. Session cũ giữ version/result cũ; phiên đang mở với version không còn được hỗ trợ không tự chuyển sang luật mới. Không chỉnh runtime Unity trong thay đổi này. Chưa kiểm tra lại trực tiếp trên headset.
