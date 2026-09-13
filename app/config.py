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
    sales_retention_days: int
    sales_diagnostic_token: str | None
    whisper_cpp_bin: str
    phowhisper_model: str
    whisper_timeout_seconds: float


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
        sales_retention_days=_env_int("SALES_RETENTION_DAYS", 30, minimum=1, maximum=3650),
        sales_diagnostic_token=os.environ.get("SALES_DIAGNOSTIC_TOKEN", "").strip() or None,
        whisper_cpp_bin=os.environ.get("WHISPER_CPP_BIN", "whisper.cpp/build/bin/Release/whisper-cli.exe").strip(),
        phowhisper_model=os.environ.get("PHOWHISPER_MODEL", "ggml-phowhisper-small.bin").strip(),
        whisper_timeout_seconds=_env_float(
            "WHISPER_TIMEOUT_SECONDS", 90.0, minimum=1.0, maximum=600.0
        ),
    )
