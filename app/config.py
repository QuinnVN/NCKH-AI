"""Environment-backed settings for the VR backend.

The service is intentionally usable with no ``.env`` file.  Defaults are
local-only so a developer does not accidentally expose a fresh server on the
network.  Values are read when :func:`get_settings` is called, which also
makes configuration changes straightforward to exercise in tests.
"""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_LLM_MODEL = "qwen3-4b"

# Keep local configuration out of source control while allowing deployment
# environments to override it through their own environment variables.
load_dotenv(BACKEND_ROOT / ".env", override=False)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    recordings_dir: str
    api_token: str | None
    max_recording_bytes: int
    max_telemetry_bytes: int
    max_transcript_chars: int
    max_conversation_messages: int
    max_message_chars: int
    command_timeout_seconds: float
    llm_base_url: str
    llm_model: str
    llm_connect_timeout_seconds: float
    llm_read_timeout_seconds: float
    llm_write_timeout_seconds: float
    llm_pool_timeout_seconds: float
    llm_max_tokens: int
    llm_career_max_tokens: int
    max_career_prompt_chars: int
    max_sales_prompt_chars: int
    max_lawyer_prompt_chars: int
    sales_retention_days: int
    sales_diagnostic_token: str | None
    lawyer_diagnostic_token: str | None
    sherpa_model_dir: str
    sherpa_num_threads: int
    sherpa_timeout_seconds: float
    supertonic_base_url: str | None
    tts_timeout_seconds: float
    disable_ai_sale_pt2: bool
    ai_thinking_sale_pt2: bool
    mongodb_uri: str | None
    mongodb_database: str
    mongodb_results_collection: str


def get_settings() -> Settings:
    """Load settings from environment variables using bounded safe defaults."""

    host = os.environ.get("BACKEND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # llama-server exposes its OpenAI-compatible API on port 8080 by default.
    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL).strip()
    if not base_url:
        base_url = DEFAULT_LLM_BASE_URL

    api_token = os.environ.get("BACKEND_API_TOKEN", "").strip() or None
    recordings_dir = os.environ.get("RECORDINGS_DIR", "recordings").strip() or "recordings"

    return Settings(
        host=host,
        port=_env_int("BACKEND_PORT", 8000, minimum=1, maximum=65535),
        recordings_dir=recordings_dir,
        api_token=api_token,
        max_recording_bytes=_env_int(
            "MAX_RECORDING_BYTES", 8 * 1024 * 1024, minimum=1024, maximum=64 * 1024 * 1024
        ),
        max_telemetry_bytes=_env_int(
            "MAX_TELEMETRY_BYTES", 12 * 1024 * 1024, minimum=16 * 1024, maximum=64 * 1024 * 1024
        ),
        max_transcript_chars=_env_int(
            "MAX_TRANSCRIPT_CHARS", 4000, minimum=1, maximum=32_000
        ),
        max_conversation_messages=_env_int(
            "MAX_CONVERSATION_MESSAGES", 20, minimum=0, maximum=100
        ),
        max_message_chars=_env_int("MAX_MESSAGE_CHARS", 4000, minimum=1, maximum=32_000),
        command_timeout_seconds=_env_float(
            "COMMAND_TIMEOUT_SECONDS", 5.0, minimum=0.1, maximum=60.0
        ),
        llm_base_url=base_url.rstrip("/"),
        # llama-server accepts the alias supplied with --alias; local-model is
        # deliberately generic so deployments can choose any GGUF model.
        llm_model=os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL).strip() or DEFAULT_LLM_MODEL,
        llm_connect_timeout_seconds=5.0,
        llm_read_timeout_seconds=_env_float(
            "LLM_READ_TIMEOUT_SECONDS", 120.0, minimum=1.0, maximum=600.0
        ),
        llm_write_timeout_seconds=10.0,
        llm_pool_timeout_seconds=5.0,
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 100, minimum=1, maximum=2048),
        llm_career_max_tokens=_env_int(
            "LLM_CAREER_MAX_TOKENS", 4096, minimum=256, maximum=8192
        ),
        max_career_prompt_chars=_env_int(
            "MAX_CAREER_PROMPT_CHARS", 32_000, minimum=4_000, maximum=64_000
        ),
        max_sales_prompt_chars=_env_int(
            "MAX_SALES_PROMPT_CHARS", 24_000, minimum=4_000, maximum=64_000
        ),
        max_lawyer_prompt_chars=_env_int(
            "MAX_LAWYER_PROMPT_CHARS", 32_000, minimum=4_000, maximum=64_000
        ),
        sales_retention_days=_env_int("SALES_RETENTION_DAYS", 30, minimum=1, maximum=3650),
        sales_diagnostic_token=os.environ.get("SALES_DIAGNOSTIC_TOKEN", "").strip() or None,
        lawyer_diagnostic_token=os.environ.get("LAWYER_DIAGNOSTIC_TOKEN", "").strip() or None,
        sherpa_model_dir=os.environ.get(
            "SHERPA_MODEL_DIR",
            "models/sherpa-onnx-zipformer-vi-30M-int8-2026-02-09",
        ).strip(),
        sherpa_num_threads=_env_int(
            "SHERPA_NUM_THREADS", 1, minimum=1, maximum=16
        ),
        sherpa_timeout_seconds=_env_float(
            "SHERPA_TIMEOUT_SECONDS", 20.0, minimum=1.0, maximum=600.0
        ),
        supertonic_base_url=(
            os.environ.get("SUPERTONIC_BASE_URL", "http://127.0.0.1:7788").strip().rstrip("/")
            or None
        ),
        tts_timeout_seconds=_env_float(
            "TTS_TIMEOUT_SECONDS", 8.0, minimum=1.0, maximum=30.0
        ),
        disable_ai_sale_pt2=_env_bool("DISABLE_AI_SALE_PT2"),
        ai_thinking_sale_pt2=_env_bool("AI_THINKING_SALE_PT2", True),
        mongodb_uri=os.environ.get("MONGODB_URI", "").strip() or None,
        mongodb_database=os.environ.get("MONGODB_DATABASE", "desmap").strip() or "desmap",
        mongodb_results_collection=os.environ.get("MONGODB_RESULTS_COLLECTION", "game_results").strip() or "game_results",
    )
