# Kiểm tra dữ liệu cho classifier Sales

Ngày kiểm tra: 03/10/2026. Nguồn: `D:/NCKH-AI/recordings/`.

## Kết luận

Kho có thể làm dữ liệu khởi đầu cho thử nghiệm classifier phạm vi nhỏ sau khi sửa transcript và gán nhãn lại. Chưa đủ để huấn luyện và đánh giá tin cậy toàn bộ hành vi của hội thoại Sales. Các con số về nhãn dưới đây là nhãn hệ thống đã lưu, chưa phải nhãn được con người xác nhận.

## Thống kê kho

| Thành phần | Số lượng | Ý nghĩa |
|---|---:|---|
| JSON toàn bộ cây thư mục | 313 | Bao gồm phiên, kết quả, bản nháp, trạng thái đồng bộ và replay |
| WAV toàn bộ cây thư mục | 226 | Bao gồm Sales, luật sư và một bản ghi khác |
| Phiên Sales quay lại | 60 | 43 phiên có lượt nói, 17 phiên rỗng |
| Lượt nói Sales quay lại | 150 | 150 turn ID khác nhau, tất cả có transcript và WAV tương ứng |
| Transcript lượt nói khác nhau sau chuẩn hóa chữ hoa/thường và khoảng trắng | 149 | Một câu xuất hiện hai lần; ngữ cảnh có thể khác |
| Audio gắn với 150 lượt | 38,94 phút | Dữ liệu classifier được tính theo lượt nói, không theo số phút |
| WAV theo tên phiên Sales | 152 | Có hai WAV không thuộc 150 lượt đã lưu |
| JSON thuyết phục bán hàng phần 1 | 55 | 50 có transcript không rỗng, 49 transcript khác nhau |
| Transcript TXT phần 1 | 1 | Có thể bổ sung một bản ghi mà transcript JSON đang null |
| WAV thuyết phục phần 1 | 55 | Khác nhiệm vụ hội thoại khách quay lại |
| JSON luật sư | 17 | Không tính là dữ liệu trực tiếp cho classifier Sales |
| WAV luật sư | 18 | Có thêm một transcript TXT |

Trong `simulation-results`, 28 JSON kết quả Sales cuối chứa 103 lượt, đều đã có trong 150 lượt của các phiên. Không cộng thêm các lượt này khi tính kích thước dataset. Bản nháp, cache `completedTurns` và replay cũng phải được liên kết theo turn ID để tránh đếm trùng.

Có 13 giá trị tên người tham gia khác nhau trong các phiên có nội dung. Đây không phải bằng chứng xác nhận 13 người nói độc lập. 40/43 phiên có nội dung lưu tên, ba phiên không có tên. Cần xác minh người nói trước khi chia dữ liệu theo người.

## Phân bố mục tiêu tại thời điểm người chơi nói

| Mục tiêu | Lượt |
|---|---:|
| 1 | 121 |
| 2 | 26 |
| 3 | 3 |
| 4 | 0 |

Đây là `objectiveActiveDuringTurn`. Không phải phân bố của từng hành vi: một câu ở mục tiêu 1 vẫn có thể đề xuất giải pháp. Tuy nhiên, kho không có ví dụ về phản ứng trong ngữ cảnh mục tiêu 4.

## Độ phủ nhãn tự động hiện có

| Nhãn | Có | Không | Thiếu trường |
|---|---:|---:|---:|
| emotionalAcknowledgment | 77 | 73 | 0 |
| openQuestion | 39 | 111 | 0 |
| useOrDurationQuestion | 14 | 136 | 0 |
| fitConditionOrPreferenceQuestion | 20 | 130 | 0 |
| causeStatement | 13 | 137 | 0 |
| policyExchange | 24 | 126 | 0 |
| lightweightForWalking | 20 | 130 | 0 |
| fitOrWalkTrial | 16 | 134 | 0 |
| originalSaleResponsibility | 9 | 141 | 0 |
| routineMatchExplanation | 12 | 138 | 0 |
| verificationStep | 15 | 135 | 0 |
| unauthorizedPromise | 15 | 135 | 0 |
| maintainsUnauthorizedPromise | 13 | 137 | 0 |
| managerEscalation | 0 | 150 | 0 |
| abuse | 8 | 142 | 0 |
| polite | 94 | 33 | 23 |
| condescending | 3 | 124 | 23 |
| apology | 47 | 80 | 23 |
| remedy | 41 | 86 | 23 |
| explanation | 18 | 109 | 23 |
| correctiveAdvice | 17 | 110 | 23 |
| reasonableReturnPolicy | 18 | 109 | 23 |
| beggingWithoutExplanation | 17 | 110 | 23 |
| apologyOnly | 35 | 92 | 23 |
| profanityOrInsult | 9 | 118 | 23 |
| repeatedQuestion | 0 | 127 | 23 |

127 lượt có schema gồm 26 boolean và danh sách vi phạm. 23 lượt dùng schema cũ gồm 15 boolean. Không được thay nhãn thiếu bằng `false`.

`playerResponseRating`: 95 bad, 37 good, 18 lượt không có trường này. Đây là kết quả hệ thống, không phải đánh giá độc lập của người gán nhãn.

Tám lượt mang nhãn `abuse=true` nằm trong sáu phiên nhưng chỉ một nhóm tên người tham gia. Ba lượt `condescending=true` cũng chỉ thuộc một nhóm tên. Chia ngẫu nhiên theo lượt có thể làm đánh giá quá lạc quan.

## Chất lượng và nguồn nhãn

Metadata STT của 150 lượt ghi `sherpa-onnx`. Metadata model ghi 83 lượt `Qwen3-4B-Hybrid`, 56 lượt `qwen3-4b`, 11 lượt không ghi model. Nhãn đã qua logic backend nên không phải đầu ra model nguyên trạng. Không tìm thấy trường xác nhận người gán nhãn hoặc duyệt nhãn trong các JSON đã kiểm tra.

Ví dụ cần rà soát: trong `sales-session-12041310fd3240ab8528dc29f05ba3f0.json`, lượt `7b1411a0d2f44d5a908244a3263c1bd4` có transcript `Ồ DẠ` nhưng `policyExchange=true`. Trong `sales-session-fbcb5478d5f94c53a05c9ca413e0a8ad.json`, transcript ba từ được gán đồng thời nhiều hành vi giải quyết. Cần nghe audio và xem lịch sử để phân biệt lỗi STT, lỗi model và cách định nghĩa nhãn.

Kiểm tra toàn bộ WAV đọc được header PCM; 225 file mono 16 kHz PCM16, một file stereo 48 kHz PCM16. Đã đối chiếu SHA-256 dữ liệu PCM; không có bản trùng PCM trong từng nhóm tên file. Chưa nghe toàn bộ audio, chưa đo độ đúng transcript, chưa xác nhận danh tính người nói và chưa gán nhãn thủ công.

## Mức dữ liệu đề xuất

Các mức sau là kế hoạch cho dự án, không phải ngưỡng bảo đảm chất lượng của model:

| Phạm vi | Dữ liệu khởi đầu nên chuẩn bị |
|---|---|
| Thử nghiệm vài hành vi bằng SetFit | Kho hiện tại có thể dùng sau khi rà soát, chọn nhãn đủ ví dụ dương và âm |
| Bản thử 6–8 hành vi | Khoảng 300–500 lượt có ngữ cảnh và nhãn đã duyệt, bao gồm phần kiểm tra tách riêng |
| Classifier đầy đủ nhiều hành vi Sales | Khoảng 1.000–3.000 lượt đa dạng làm mốc thu thập ban đầu; cần tăng theo kết quả từng nhãn |

Với bản thử, nhắm tối thiểu khoảng 30–50 ví dụ dương và các ví dụ âm dễ nhầm cho mỗi hành vi ưu tiên. Những nhãn tác động đến phạt hoặc kết thúc thất bại cần bộ kiểm tra lớn hơn. Một lượt có thể mang nhiều nhãn nên không cộng cơ học số ví dụ theo nhãn. Không phải chỉ có nhiều lượt là đủ: cần đủ phiên, người nói, giai đoạn và tình huống.

Ưu tiên bổ sung mục tiêu 3 và 4, xác nhận thông tin, câu trung tính, sửa lời đã nói, phủ định và trích dẫn cam kết, hỏi lặp so với hỏi làm rõ, đề nghị chuyển quản lý, và các cách nói lịch sự khác nhau. Dùng nhãn hệ thống làm bản nháp để người duyệt sửa; không coi chúng là nhãn chuẩn.

Chia train/validation/test theo người nói hoặc phiên, giữ cả bản transcript STT và bản chỉnh đúng của cùng lượt trong cùng tập. Nhóm bản sao và biến thể sinh từ một ví dụ cũng phải ở cùng tập. Đánh giá theo từng nhãn, phạt nhầm và trường hợp model không đủ chắc chắn; không chỉ dùng accuracy tổng.

## Tài liệu phương pháp

- SetFit: https://huggingface.co/docs/setfit/quickstart
- Phân loại nhiều nhãn: https://huggingface.co/docs/setfit/how_to/multilabel

Ví dụ SetFit dùng tám mẫu mỗi lớp trong bài toán cảm xúc hai lớp không chứng minh tám mẫu đủ cho hành vi Sales phụ thuộc ngữ cảnh.
