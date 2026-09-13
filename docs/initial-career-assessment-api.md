# Initial career assessment API contract

`POST /api/ai/initial-career-assessment` accepts a categorized questionnaire profile and returns up to five careers for the participant to explore. The language model generates the careers from the profile. The website must not send a candidate career list.

This is a breaking replacement for the earlier candidate-scoring contract.

## Website migration

The website must make these changes:

1. Remove `careers` from the request.
2. Add `category` to every questionnaire dimension.
3. Read `suggestions` instead of `results` from the response.
4. Read only `career_name` and `match_percentage` from each suggestion. Career IDs are no longer returned.
5. Display suggestions in the order returned by the API.
6. Present `match_percentage` as an estimated fit, not a probability of career success.
7. Do not assume that a suggested career has a matching VR simulation.

The route path remains unchanged. The old and new contracts do not coexist.

## Request

Send JSON with `Content-Type: application/json`.

When `BACKEND_API_TOKEN` is configured, also send:

```http
Authorization: Bearer <token>
```

Example:

```json
{
  "assessment_id": "assessment-001",
  "dimensions": [
    {
      "id": "creative_work",
      "name": "Hứng thú sáng tạo",
      "description": "Mức độ yêu thích việc tạo ra ý tưởng và sản phẩm mới.",
      "category": "interest",
      "score": 88
    },
    {
      "id": "analytical_thinking",
      "name": "Tư duy phân tích",
      "description": "Khả năng phân tích dữ kiện và giải quyết vấn đề.",
      "category": "ability",
      "score": 82
    },
    {
      "id": "communication",
      "name": "Giao tiếp",
      "description": "Khả năng lắng nghe và diễn đạt rõ ràng.",
      "category": "trait",
      "score": 74
    }
  ]
}
```

### Dimension categories

| Value | Meaning |
| --- | --- |
| `interest` | An activity, subject, or work style the participant likes or wants to explore. |
| `ability` | A skill or capability measured by the questionnaire. |
| `trait` | A personal or behavioral characteristic. |
| `other` | A relevant dimension that does not fit the other categories. |

Interest dimensions are the main recommendation signal. The model uses the other categories as supporting information.

### Request validation

- `assessment_id` contains 1–64 letters, digits, underscores, or hyphens and cannot start with punctuation.
- `dimensions` contains 1–28 items.
- Dimension IDs are unique and follow the same format as `assessment_id`.
- A dimension name contains 1–100 characters.
- A dimension description contains 1–500 characters.
- `category` is one of `interest`, `ability`, `trait`, or `other`.
- `score` is an integer from 0 through 100.
- Unknown fields are rejected.

Request JSON Schema, Draft 2020-12:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["assessment_id", "dimensions"],
  "properties": {
    "assessment_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64,
      "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_-]*$"
    },
    "dimensions": {
      "type": "array",
      "minItems": 1,
      "maxItems": 28,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "name", "description", "category", "score"],
        "properties": {
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_-]*$"
          },
          "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "category": {
            "type": "string",
            "enum": ["interest", "ability", "trait", "other"]
          },
          "score": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100
          }
        }
      }
    }
  }
}
```

## Successful response

```json
{
  "assessment_id": "assessment-001",
  "suggestions": [
    {
      "career_name": "Nhà thiết kế trải nghiệm người dùng",
      "match_percentage": 91
    },
    {
      "career_name": "Chuyên viên nghiên cứu thị trường",
      "match_percentage": 84
    },
    {
      "career_name": "Chuyên viên truyền thông",
      "match_percentage": 78
    }
  ]
}
```

Response rules:

- `assessment_id` is copied unchanged from the request.
- `suggestions` contains 1–5 items.
- Suggestions have distinct career names. Comparison ignores capitalization and surrounding whitespace.
- Suggestions are sorted by `match_percentage` from highest to lowest. Equal percentages are allowed.
- Career names are in Vietnamese and favor occupations recognized in Vietnam. International occupations are allowed when the profile supports them.
- Each suggestion contains exactly `career_name` and `match_percentage`.
- `match_percentage` is an independent, uncalibrated estimate of fit from 0 through 100. It is not a probability of success, and percentages do not need to total 100.
- Results are provisional guidance. They are not diagnoses, guarantees, or career decisions.

Response JSON Schema, Draft 2020-12:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["assessment_id", "suggestions"],
  "properties": {
    "assessment_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64,
      "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_-]*$"
    },
    "suggestions": {
      "type": "array",
      "minItems": 1,
      "maxItems": 5,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["career_name", "match_percentage"],
        "properties": {
          "career_name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "match_percentage": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100
          }
        }
      }
    }
  }
}
```

JSON Schema cannot express the distinct-name and descending-order rules. The backend validates both after parsing the model response.

## Error responses

| Status | Meaning |
| --- | --- |
| `401` | The bearer token is missing or invalid when authentication is enabled. |
| `422` | The website request does not match the request contract. |
| `503` | The language model is not configured, unavailable, or timed out. |
| `502` | The model response is unusable after one automatic repair attempt. |

FastAPI validation errors use its standard `detail` array. Service errors use a `detail` string.

## Model behavior

The backend sends the validated profile to Qwen3-4B with thinking enabled. The prompt asks for open-ended career suggestions, prioritizes `interest` dimensions, and treats other categories as supporting signals. It does not limit suggestions to the VR scene catalog.

The model must return strict JSON. The backend removes hidden reasoning, validates the response, checks the request identity, verifies uniqueness and ordering, and makes one formatting-repair request when validation fails. Hidden reasoning is not returned to the website or written to logs.
