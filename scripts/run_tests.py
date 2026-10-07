"""Run unit tests without loading local credentials or sending live HTTP requests."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import dotenv
import httpx


ROOT = Path(__file__).resolve().parents[1]
TEST_ENV = {
    "SALES_PIPELINE_MODE": "legacy",
    "OPENROUTER_API_KEY": "offline-test-key",
    "MONGODB_URI": "",
    "BACKEND_API_TOKEN": "",
    "LLM_USE_AMD_HYBRID": "false",
    "DISABLE_AI_SALE_PT2": "false",
}


async def reject_live_http(transport, request):
    raise AssertionError(
        f"Unit test attempted live HTTP: {request.method} {request.url.host}. "
        "Use httpx.MockTransport or mock the service boundary."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest module, class or method names")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    with (
        tempfile.TemporaryDirectory(prefix="nckh-tests-") as recordings,
        patch.dict(os.environ, {**TEST_ENV, "RECORDINGS_DIR": recordings}),
        patch.object(dotenv, "load_dotenv", return_value=False),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", reject_live_http),
        patch.object(httpx.HTTPTransport, "handle_request", side_effect=AssertionError(
            "Unit test attempted live HTTP. Use httpx.MockTransport."
        )),
    ):
        loader = unittest.defaultTestLoader
        suite = loader.loadTestsFromNames(args.tests) if args.tests else loader.discover(str(ROOT / "app/tests"))
        result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
