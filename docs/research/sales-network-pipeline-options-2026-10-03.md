# Phương án pipeline Sales dùng mạng

> Cập nhật theo lựa chọn của người dùng: ưu tiên cùng tài khoản OpenRouter, giữ Supertonic và bỏ TTS cloud khỏi phương án triển khai. Các phương án cloud voice và chi phí bên dưới là khảo sát trước khi chốt phạm vi. Đề xuất hiện tại nằm trong [pipeline OpenRouter + Supertonic](sales-openrouter-pipeline-options-2026-10-03.md).

Khảo sát ngày 03/10/2026. Đây là đề xuất kiến trúc và phép tính theo giá công bố; chưa chạy thử API thoại hoặc model sinh lời mới. Không thay đổi pipeline đang hoạt động.

## Kết luận đề xuất

Ưu tiên thử pipeline gồm nhận dạng giọng nói, Jev phân loại, backend cập nhật trạng thái và quyết định nội dung, Qwen cloud hoặc GPT-6 Luna viết lời Lan, rồi Gemini 3.8 Flash-Lite TTS phát giọng. Cách này cho phép kiểm tra lời thoại trước khi phát và thay từng thành phần riêng.

Nếu trải nghiệm nói xen và phản hồi liên tục là ưu tiên cao nhất, thử thêm Gemini 3.8 Live và GPT-Live 1, cùng giữ backend làm nơi quyết định điểm, dữ kiện và kết quả game.

## Cơ sở từ hệ thống hiện tại

- Qwen hiện phân loại lượt nói và chọn ý định, chưa viết lời thoại tự nhiên. Backend ghép câu mẫu với dữ kiện.
- `app/llm_service.py` gọi Chat Completions với `stream=False`, chưa gửi thông tin xác thực của nhà cung cấp cloud. Không thể chỉ đổi tên model hoặc URL để có toàn bộ pipeline mới.
- Backend đã có luật mục tiêu, chính sách và chấm điểm trong `app/sales_returning_customer.py`. Cần adapter cho nhãn Jev, đồng thời rà lại các phép ghi đè bằng từ khóa trong `_ground_turn_draft` để chúng không đảo ngược nhãn mới sai cách.
- Thử Jev trước đó có 40 lượt, 320 nhãn; khớp nhãn assistant 315/320, trung vị khoảng 402 ms. Tổng chi phí API trả về 0.002753772 USD. Nhãn không phải bộ chuẩn do người chấm độc lập; tám nhãn đã thử chưa phủ toàn bộ schema game.
- Dữ liệu nhận dạng giọng nói có nhiều câu mơ hồ. Nâng classifier không tự sửa được nội dung đã nghe sai.

## Model văn bản đáng thử

Giá Standard, USD trên một triệu token, không tính cache hoặc khuyến mại riêng của tài khoản.

| Thành phần | Input | Output | Vai trò |
| --- | ---: | ---: | --- |
| Jev 1.13 qua OpenRouter | 0.042 | 0 | Phân loại hành vi và ý định |
| Qwen3.7 Flash, Singapore International, input không quá 32K | 0.030 | 0.130 | Viết lời Lan từ kế hoạch đã duyệt |
| Qwen3.8 Flash, Singapore International | 0.150 | 0.470 | Ứng viên Qwen mới hơn để đối chiếu chất lượng |
| GPT-6 Luna | 0.100 | 0.500 | Viết lời Lan hoặc kiểm tra lại nhãn khó |

Nguồn: [Jev](https://openrouter.ai/typesafe/jev-1.13), [Qwen3.7 Flash](https://www.alibabacloud.com/help/en/model-studio/qwen3-7-flash), [Qwen3.8 Flash](https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash), [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).

Giá trực tiếp Alibaba và OpenAI không phải báo giá OpenRouter cho cùng model. Với model sinh lời, dùng chế độ không suy luận khi được hỗ trợ, giới hạn câu trả lời ngắn và đo chất lượng tiếng Việt thực tế. Tên phiên bản mới hơn không chứng minh hiệu quả hơn cho game này.

## Các phương án thoại

| Phương án | Cấu trúc | Lợi ích | Điều cần kiểm tra |
| --- | --- | --- | --- |
| Pipeline từng bước | STT → Jev → backend → model văn bản → TTS | Kiểm soát câu nói, điểm và dữ kiện; nâng dần từ code hiện tại | Các bước nối tiếp tạo độ trễ; phải stream audio và xử lý ngắt lời |
| Gemini 3.8 Live + Jev/backend | Model nhận/phát audio, gọi backend để lấy trạng thái đã xác nhận | Native audio; function calling hỗ trợ blocking và asynchronous | Đừng phát dữ kiện hoặc kết quả chưa được backend xác nhận; transcript và lịch sử có thêm phí |
| GPT-Realtime-2.1 Mini + Jev/backend | Realtime session kết hợp công cụ backend | Model thoại giá thấp hơn bản đầy đủ, có function calling | Không hỗ trợ Structured Outputs; transcript đầu vào tính phí riêng, vẫn cần nhãn riêng |
| GPT-Live 1 + client delegation | GPT-Live xử lý hội thoại; backend chạy Jev và luật game | Nghe và nói đồng thời; kết nối workflow hiện tại | Tính tiền toàn bộ thời gian session, cả im lặng; kiểm tra tiếng Việt và lời nói trước khi có kết quả |

Chưa xác minh được model `gpt-live-2` trong tài liệu chính thức. Tài liệu đang công bố `gpt-live-1`, GA ngày 10/09/2026; dòng Realtime có `gpt-realtime-2.1` và Mini.

Gemini 3.8 Live GA ngày 15/09/2026. Hai model TTS 3.8 GA ngày 22/09/2026. Flash-Lite TTS hỗ trợ tiếng Việt, streaming audio, điều khiển cách nói bằng metadata và đọc văn bản làm transcript. Nên thử giọng Lan khó chịu nhưng lịch sự, nói ngắn và bớt căng khi người chơi giải quyết đúng.

Nguồn: [OpenAI changelog](https://developers.openai.com/api/docs/changelog), [GPT-Live](https://developers.openai.com/api/docs/guides/live), [client delegation](https://developers.openai.com/api/docs/guides/live-delegation), [Realtime Mini](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini), [Gemini changelog](https://ai.google.dev/gemini-api/docs/changelog), [Gemini Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live), [Gemini TTS](https://ai.google.dev/gemini-api/docs/speech-generation).

## Ước tính chi phí

Giả định một phiên 10 lượt, mỗi lượt người chơi nói 20 giây, Lan nói 12 giây. Tổng audio mới là 200 giây input và 120 giây output. Phiên GPT-Live duy trì kết nối 8 phút. Model viết lời nhận 800 token và trả 100 token mỗi lượt, không dùng reasoning hoặc cache. Chi phí Jev lấy trung bình của 40 lượt đã thử, khoảng 0.000688 USD cho 10 lượt với tám câu hỏi. Schema đầy đủ hơn có thể tăng số token.

| Cấu hình | Chi phí ước tính USD/phiên | Phạm vi phép tính |
| --- | ---: | --- |
| Jev + Qwen3.7, giữ STT/TTS local | 0.0011 | API văn bản; chưa tính điện và tài nguyên máy |
| Jev + Qwen3.7 + GPT Transcribe theo lượt + Gemini Flash-Lite TTS | 0.0346 | Input STT 0.0045 USD/phút; output TTS hiện khoảng 0.0015 USD/10 giây, thêm 1000 token input TTS |
| Jev + Qwen3.7 + Gemini Live Transcribe + Gemini Flash-Lite TTS | 0.0496 | STT streaming khoảng 0.009 USD/phút theo ước tính của Google; TTS như trên |
| Gemini 3.8 Live | 0.0527 phần audio mới | 0.005 USD/phút input + 0.018 USD/phút output; chưa tính văn bản, transcript, Jev hoặc lịch sử |
| GPT-Realtime-2.1 Mini | 0.068 phần audio mới | Input 10 token/giây, output 20 token/giây; 10/20 USD trên một triệu audio token; chưa tính transcript, văn bản, Jev hoặc lịch sử |
| GPT-Live 1 | 0.40 phần voice session | 8 phút × 0.05 USD/phút; backend và công cụ tính riêng |

Các con số native audio chỉ là phần audio mới, không phải tổng hóa đơn hoặc dự báo ngân sách. Gemini tính lại audio lịch sử trong context mỗi lượt, và thêm phí token văn bản khi bật transcript. OpenAI Realtime cũng tính context theo từng response, với cache có thể giảm phí. Phải đo `usage` của cả phiên nhiều lượt trước khi so tổng chi phí.

Giá Gemini 3.8 Flash-Lite TTS hiện là khuyến mại đến hết 31/12/2026; input/output tăng gấp đôi từ 01/01/2027. Ước tính không tính thuế, phí nạp tiền của gateway, retry, hosting hoặc các dịch vụ bổ sung. Không dùng Batch/Flex để lập ngân sách phản hồi trực tiếp vì chúng không có cùng cam kết độ trễ.

Nguồn giá: [OpenAI](https://developers.openai.com/api/docs/pricing), [Google](https://ai.google.dev/gemini-api/docs/pricing). Nguồn cách tính audio và lịch sử: [OpenAI voice costs](https://developers.openai.com/api/docs/guides/voice-latency-cost?voice-api=realtime), [Gemini best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices).

## Thay đổi cần làm trước

1. Tách classifier, luật game, kế hoạch câu trả lời, model viết lời và bộ phát audio thành các adapter riêng. Backend xuất ý định, dữ kiện được phép nói, trạng thái cảm xúc và yêu cầu hỏi tiếp. Model viết lời không tự cập nhật điểm hoặc mở dữ kiện.
2. Jev gom các câu hỏi cần thiết vào một request mỗi lượt. Hiệu chỉnh ngưỡng theo từng nhãn trên dữ liệu tiếng Việt được người chấm. Nhãn mơ hồ có thể gọi model thứ hai hoặc hỏi lại; không phạt nặng dựa trên một xác suất chưa hiệu chỉnh.
3. Thêm VAD tự kết thúc câu, phụ đề tạm thời, transcript cuối để chấm, streaming TTS, bộ đệm phát audio và xử lý nói xen. Khi người chơi ngắt, dừng audio và hủy hoặc bỏ kết quả cũ bằng turn ID.
4. Kiểm tra lời sinh ra trước khi phát. Với câu ngắn, duyệt cả câu rồi stream phần TTS; nếu duyệt theo từng câu thì giữ nguyên thông tin đã duyệt và không phát token chưa kiểm tra.
5. Ghi độ trễ từ cuối câu người chơi đến audio đầu của Lan, chi phí cả phiên, số lần hỏi lại, phạt nhầm và câu nói vượt dữ kiện. Mục tiêu thử nghiệm dưới 1.5–2 giây đến audio đầu chỉ là mục tiêu, chưa đo được ở pipeline đề xuất.

Vòng so sánh đầu nên dùng cùng audio và trạng thái game cho pipeline từng bước, Gemini 3.8 Live và GPT-Live 1. Chọn theo mức tự nhiên của tiếng Việt, độ chính xác chấm game, độ trễ đến audio đầu và chi phí thực tế trên mỗi phiên hoàn tất.
