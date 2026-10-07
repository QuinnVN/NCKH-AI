# Pipeline Sales dùng OpenRouter và Supertonic

Khảo sát ngày 03/10/2026. Phạm vi người dùng đã chọn: chấp nhận mạng, ưu tiên một tài khoản OpenRouter, giữ Supertonic local. Đã chốt `qwen/qwen3.7-flash` viết lời Lan. Chưa sửa pipeline production hoặc gọi thử model sinh lời/STT mới. Đặc tả triển khai đã được lưu trong [tracker NCKH-VR](D:/NCKH-VR/.scratch/sales-dialogue-openrouter-upgrade/spec.md).

## Phương án ưu tiên

Sherpa local → Jev qua OpenRouter → luật game và kế hoạch trả lời ở backend → Qwen 3.7 Flash qua OpenRouter → Supertonic local → Unity.

Jev đánh giá hành động trong lời người chơi. Backend quyết định điểm, mục tiêu, dữ kiện được phép nói và trạng thái cảm xúc. Model viết lời nhận kế hoạch đó và viết một hoặc hai câu tiếng Việt tự nhiên. Supertonic phát giọng hiện có.

Một OpenRouter API key đủ cho Jev, các model văn bản và STT OpenRouter nếu bổ sung sau. Không cần BYOK hoặc tài khoản trực tiếp của từng nhà cung cấp cho các route này. Jev sử dụng Decisions endpoint riêng; model văn bản sử dụng Chat Completions. `typesafe/jev-router` trong catalog là sản phẩm routing khác, không thay cho `typesafe/jev-1.13` đã thử.

## Model văn bản

Đã đối chiếu Models API công khai và trang model OpenRouter. Giá USD trên một triệu token, chưa áp dụng cache và chưa tính token reasoning.

| Model slug | Input | Output | Đề xuất |
| --- | ---: | ---: | --- |
| `qwen/qwen3.7-flash` | 0.030 | 0.130 | Người dùng đã chọn; mức giá này cho input dưới 32K |
| `openai/gpt-6-luna` | 0.100 | 0.500 | Đối chiếu lời thoại tiếng Việt; có thể kiểm tra lại một số nhận định khó |
| `qwen/qwen3.8-flash` | 0.150 | 0.470 | Ứng viên Qwen mới hơn; chỉ chọn nếu chất lượng thực tế tốt hơn |

Nguồn: [Qwen3.7 Flash](https://openrouter.ai/qwen/qwen3.7-flash), [GPT-6 Luna](https://openrouter.ai/openai/gpt-6-luna), [Qwen3.8 Flash](https://openrouter.ai/qwen/qwen3.8-flash), [Models API](https://openrouter.ai/api/v1/models).

Giữ Jev 1.13, giá input 0.042 USD trên một triệu token, output miễn phí. Kết quả thử 40 lượt chỉ gồm tám câu hỏi, tham chiếu do assistant chấm transcript; chưa đủ xác nhận toàn bộ schema của game. Nguồn: [Jev](https://openrouter.ai/typesafe/jev-1.13).

## Chi phí tham khảo

Một phiên 10 lượt; model viết lời mỗi lượt nhận 800 token và trả 100 token, không có reasoning hoặc cache. Jev lấy chi phí trung bình từ 40 lượt đã thử, khoảng 0.000688 USD/10 lượt với tám câu hỏi. STT và Supertonic local không có phí API; chưa tính điện, hosting, retry, thuế hoặc phí nạp credit.

| Jev + model viết lời | USD/phiên 10 lượt | USD/1000 phiên |
| --- | ---: | ---: |
| Qwen3.7 Flash | 0.00106 | 1.06 |
| GPT-6 Luna | 0.00199 | 1.99 |
| Qwen3.8 Flash | 0.00236 | 2.36 |

Đây là ước tính từ số token giả định, không phải chi phí cả game đã đo. Prompt, lịch sử, thêm nhãn và reasoning có thể tăng tiền. Chưa có phép đo độ trễ của ba model viết lời trên máy và mạng hiện tại.

## STT nếu cần nâng tiếp

OpenRouter có `/api/v1/audio/transcriptions` với cùng key. Một lựa chọn thử giá thấp là `openai/whisper-large-v3-turbo`. Models API hiện công bố mức thấp nhất 0.00000333 USD/giây, khoảng 0.00020 USD/phút. Trang model có các provider với giá khác nhau; tiền thực tế tùy route và lấy từ `usage.cost`. Không mặc định đây là STT tốt hơn Sherpa cho giọng người chơi.

Đây là API nhận một đoạn audio rồi trả transcript. Không suy ra khả năng streaming STT liên tục hoặc phụ đề tạm thời chỉ từ tốc độ inference của model. Khi thử, dùng WAV trong recordings, cùng audio cho hai phía, chấm các lỗi làm thay đổi ý nghĩa như phủ định, tự sửa lời và xúc phạm.

Nguồn: [Whisper Large V3 Turbo](https://openrouter.ai/openai/whisper-large-v3-turbo), [OpenRouter transcription](https://openrouter.ai/blog/tutorials/transcription-on-openrouter/), [STT catalog](https://openrouter.ai/api/v1/models?output_modalities=transcription).

## Điều chỉnh để nói tự nhiên

- Kế hoạch backend gồm ý định, dữ kiện được phép tiết lộ, mức khó chịu, câu hỏi cần làm rõ và nội dung bắt buộc. Model viết lời không tự chấm điểm hoặc tạo thêm chính sách.
- Đưa lượt nói hiện tại và vài lượt gần nhất vào prompt để tránh lặp câu mẫu. Lan chỉ hỏi một điều mỗi lượt, dùng câu ngắn, đáp đúng nội dung người chơi vừa nói.
- Rà các phép ghi đè từ khóa hiện có trước khi gắn Jev; không để chúng đảo nhãn đã được kiểm tra. Những nhãn dẫn tới phạt nặng cần ngưỡng được hiệu chỉnh và cơ chế xử lý mơ hồ.
- Duyệt lời sinh ra trước khi đưa Supertonic. Phát text và lưu điểm theo cùng turn ID; bỏ kết quả cũ khi người chơi đã bắt đầu lượt khác.
- Có thể nâng VAD và khả năng dừng audio khi người chơi nói xen trong Unity. Những thay đổi này không yêu cầu TTS cloud nhưng vẫn cần sửa bộ điều khiển hội thoại.

Vòng thử tiếp dùng Qwen 3.7 Flash trên kế hoạch backend đã xác định. Đo thời gian sinh lời, độ tự nhiên tiếng Việt, lỗi vượt dữ kiện và tổng thời gian từ cuối câu người chơi đến khi Lan bắt đầu nói. Các model khác trong bảng chỉ còn là tham khảo từ khảo sát trước khi chốt lựa chọn.
