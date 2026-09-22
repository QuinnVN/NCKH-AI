# Dữ liệu đầu ra cho Final Assessment

Tài liệu này mô tả payload AI backend phải trả về để hiển thị trang Final Assessment. Tất cả nội dung người dùng đọc phải bằng tiếng Việt.

## Hợp đồng phản hồi khi hoàn tất

Khi phân tích thành công, API trả về một đối tượng `FinalAssessment` có dạng sau. Tên trường dùng `camelCase` để khớp trực tiếp với frontend.

```json
{
  "version": 1,
  "participantName": "Nguyễn Văn An",
  "participantEmail": "an.nguyen@example.com",
  "completedAt": "2026-09-22T10:30:00.000Z",
  "stageAssessments": {
    "D": "...",
    "E": "...",
    "S": "...",
    "M": "...",
    "A": "...",
    "P": "..."
  },
  "dimensionLevels": {
    "D1": "fairly-compatible",
    "D2": "well-compatible",
    "...": "...",
    "P6": "neutral"
  },
  "behaviourComparison": {
    "experienceName": "Bác sĩ cấp cứu",
    "findings": []
  },
  "careerSuggestions": [],
  "finalEvaluation": {
    "experienceName": "Bác sĩ cấp cứu",
    "headline": "...",
    "workStyle": "...",
    "benefit": "...",
    "challenge": "...",
    "improvement": "...",
    "strengthLabel": "...",
    "developmentLabel": "...",
    "evidence": "..."
  }
}
```

`participantName` là tên người tham gia. `participantEmail` là email đã được chuẩn hóa, viết thường và không có khoảng trắng thừa. Backend dùng cặp tên và email để liên kết kết quả với người tham gia. `completedAt` là thời điểm backend tạo kết quả, theo ISO 8601.

## Nhận định theo sáu nhóm DESMAP

`stageAssessments` chứa đủ sáu khóa `D`, `E`, `S`, `M`, `A`, `P`.

| Mã | Nhóm | Nội dung AI cần trả về |
| --- | --- | --- |
| `D` | Mong muốn | Điều người tham gia coi trọng trong công việc và môi trường phù hợp. |
| `E` | Chuyên môn | Các kỹ năng đang thể hiện và kỹ năng nên tiếp tục rèn. |
| `S` | Vai trò xã hội | Cách phối hợp, đóng góp hoặc làm việc cùng người khác. |
| `M` | Tư duy | Cách xử lý thông tin, phân tích và ra quyết định. |
| `A` | Khả năng thích ứng | Cách phản ứng khi thông tin, yêu cầu hoặc bối cảnh thay đổi. |
| `P` | Phản ứng với áp lực | Cách duy trì hiệu quả khi có áp lực thời gian hoặc nhiều việc cùng lúc. |

Mỗi giá trị là một đoạn ngắn, nêu điểm nổi bật, căn cứ quan sát hoặc điểm bảng hỏi, và một hướng phát triển nếu có. Với `D`, mức cao hoặc thấp chỉ nói về mức độ coi trọng, không gán là tốt hay xấu.

## Mức đánh giá 28 khía cạnh

`dimensionLevels` cần có đủ 28 mã sau:

| Nhóm | Mã khía cạnh |
| --- | --- |
| D | `D1`, `D2`, `D3`, `D4`, `D5`, `D6` |
| E | `E1`, `E2`, `E3`, `E4`, `E5`, `E6` |
| S | `S1`, `S2`, `S3` |
| M | `M1`, `M2`, `M3` |
| A | `A1`, `A2`, `A3`, `A4` |
| P | `P1`, `P2`, `P3`, `P4`, `P5`, `P6` |

Mỗi mã chỉ nhận một trong năm giá trị sau:

```ts
'not-compatible' | 'low-compatible' | 'neutral' | 'fairly-compatible' | 'well-compatible'
```

Frontend tự hiển thị nhãn tiếng Việt và phần diễn giải cho từng mức. Backend không cần trả lại nội dung diễn giải 28 khía cạnh.

## Đối chiếu tự đánh giá và hành vi VR

`behaviourComparison` mô tả phần "Bạn nghĩ gì, bạn đã thể hiện thế nào?".

```json
{
  "experienceName": "Bác sĩ cấp cứu",
  "findings": [
    {
      "id": "analytical-thinking",
      "kind": "confirmed",
      "title": "Tư duy phân tích",
      "questionnaireResult": "Bạn tự đánh giá cao khả năng phân tích thông tin trước khi hành động.",
      "vrEvidence": "Trong VR, bạn kiểm tra dữ kiện chính trước khi chọn thứ tự ưu tiên.",
      "summary": "Kết quả VR củng cố điểm mạnh đã thể hiện trong bảng câu hỏi."
    }
  ]
}
```

Mỗi phần tử trong `findings` cần có:

| Trường | Yêu cầu |
| --- | --- |
| `id` | Mã duy nhất, ổn định trong cùng một kết quả. |
| `kind` | `confirmed`, `emerging` hoặc `development`. |
| `title` | Tên ngắn của hành vi hoặc năng lực. |
| `questionnaireResult` | Kết quả tự đánh giá liên quan. |
| `vrEvidence` | Hành vi quan sát được trong VR. |
| `summary` | Kết luận từ việc đối chiếu hai nguồn. |

Ý nghĩa `kind`:

- `confirmed`: bảng hỏi và hành vi VR cùng cho thấy một điểm mạnh.
- `emerging`: hành vi VR cho thấy điểm mạnh chưa rõ trong tự đánh giá.
- `development`: hành vi VR cho thấy một điểm cần luyện thêm.

Chỉ trả về phát hiện có căn cứ. Không cần tạo đủ ba loại.

## Gợi ý nghề nghiệp

`careerSuggestions` có từ một đến ba nghề, đã sắp xếp từ phù hợp nhất đến thấp hơn.

```json
[
  {
    "id": "doctor",
    "name": "Bác sĩ",
    "compatibilityPercent": 86,
    "description": "Kết quả tự đánh giá và hành vi trong VR cho thấy bạn có xu hướng kiểm tra dữ kiện trước khi chọn ưu tiên. Đây là cách làm phù hợp với các nhiệm vụ cần đánh giá tình huống và quyết định có căn cứ."
  }
]
```

| Trường | Yêu cầu |
| --- | --- |
| `id` | Mã nghề duy nhất. |
| `name` | Tên nghề bằng tiếng Việt. |
| `compatibilityPercent` | Số nguyên từ 0 đến 100. Frontend hiện không hiển thị trường này, nhưng cần lưu để dùng cho các màn hình sau. |
| `description` | Lý do gợi ý, liên hệ kết quả DESMAP với bằng chứng VR và yêu cầu nghề. |

## Kết luận cuối cho người tham gia

`finalEvaluation` cấp nội dung cho khối "Đánh giá cuối cùng" ở đầu trang.

| Trường | Nội dung cần trả về |
| --- | --- |
| `experienceName` | Tên trải nghiệm VR đã hoàn thành. |
| `headline` | Kết luận ngắn, dễ hiểu, không gắn nhãn tính cách. |
| `workStyle` | Cách người tham gia đã xử lý nhiệm vụ. |
| `benefit` | Cách làm đó hỗ trợ công việc hoặc tình huống tương tự. |
| `challenge` | Điểm dễ gặp khó, chỉ nêu khi có căn cứ. |
| `improvement` | Một hành động thực hành cụ thể có thể làm tiếp. |
| `strengthLabel` | Tên ngắn của điểm mạnh nên phát huy. |
| `developmentLabel` | Tên ngắn của điểm cần luyện. |
| `evidence` | Tóm tắt ngắn bằng chứng hành vi trong VR. |

## Quy tắc kiểm tra phản hồi

- `version` phải là `1`.
- `participantName` không được rỗng.
- `participantEmail` phải là email hợp lệ sau khi chuẩn hóa.
- Không dùng `isPlaceholder` trong dữ liệu thật.
- Phải có đủ sáu nhận định DESMAP và 28 mã trong `dimensionLevels`.
- `findings[].id` và `careerSuggestions[].id` không được trùng nhau trong cùng danh sách.
- `compatibilityPercent` là số nguyên từ 0 đến 100.
- Nội dung chỉ mô tả kết quả bảng hỏi và hành vi quan sát được. Không suy đoán đặc điểm nhạy cảm, sức khỏe hoặc tính cách cố định.
- Nếu chưa đủ dữ liệu để tạo kết quả, API không trả payload `FinalAssessment` hoàn chỉnh. API cần trả trạng thái lỗi riêng để frontend tiếp tục hiển thị đánh giá ban đầu.
