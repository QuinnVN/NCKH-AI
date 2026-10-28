"""pytest isolation matching scripts/run_tests.py: no local .env, no live HTTP."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import dotenv
import httpx

TEST_ENV = {
    "PIPELINE_MODE": "legacy",
    "OPENROUTER_API_KEY": "offline-test-key",
    "MONGODB_URI": "",
    "BACKEND_API_TOKEN": "",
    "LLM_USE_AMD_HYBRID": "false",
    "DISABLE_AI_SALE_PT2": "false",
}
_patches: list = []
_recordings: tempfile.TemporaryDirectory | None = None


async def _reject_live_http(transport, request):
    raise AssertionError(f"Unit test attempted live HTTP: {request.method} {request.url.host}. "
                         "Use httpx.MockTransport or mock the service boundary.")


def pytest_configure(config):
    global _recordings
    _recordings = tempfile.TemporaryDirectory(prefix="nckh-tests-")
    _patches.extend([
        patch.dict(os.environ, {**TEST_ENV, "RECORDINGS_DIR": _recordings.name}),
        patch.object(dotenv, "load_dotenv", return_value=False),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", _reject_live_http),
        patch.object(httpx.HTTPTransport, "handle_request",
                     side_effect=AssertionError("Unit test attempted live HTTP. Use httpx.MockTransport.")),
    ])
    for item in _patches:
        item.start()


def pytest_unconfigure(config):
    while _patches:
        _patches.pop().stop()
    if _recordings is not None:
        _recordings.cleanup()
