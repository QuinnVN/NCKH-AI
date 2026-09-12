import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.ai_servers import AIServerError, AIServerManager, parse_ai_command


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.killed = False

    def send_signal(self, sent_signal: int) -> None:
        self.signals.append(sent_signal)
        self.returncode = 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


class AICommandParserTests(unittest.TestCase):
    def test_defaults_target_to_all(self):
        parsed = parse_ai_command("AI START")

        self.assertEqual(parsed.action, "start")
        self.assertEqual(parsed.target, "all")

    def test_accepts_each_action_and_target(self):
        for action in ("start", "status", "stop", "restart"):
            for target in ("all", "llama", "whisper"):
                with self.subTest(action=action, target=target):
                    parsed = parse_ai_command(f"ai {action} {target}")
                    self.assertEqual((parsed.action, parsed.target), (action, target))

    def test_rejects_invalid_syntax(self):
        for command in ("ai", "ai launch", "ai start both", "ai stop all now"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_ai_command(command)


class AIServerManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.llama = self.root / "tools" / "llama.exe"
        self.whisper = self.root / "whisper" / "whisper-server.exe"
        self.model = self.root / "models" / "phowhisper.bin"
        for path in (self.llama, self.whisper, self.model):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.environment = {
            "LLAMA_SERVER_BIN": str(self.llama),
            "WHISPER_SERVER_BIN": str(self.whisper),
            "WHISPER_SERVER_MODEL": str(self.model),
        }
        self.manager = AIServerManager(root=self.root, environment=self.environment)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_llama_executable_comes_from_environment_variable(self):
        llama_spec = self.manager._specs()["llama"]

        self.assertEqual(llama_spec.executable, self.llama)
        self.assertEqual(llama_spec.arguments[0], "serve")

    def test_preflight_reports_all_missing_dependencies(self):
        manager = AIServerManager(
            root=self.root,
            environment={
                "LLAMA_SERVER_BIN": str(self.root / "missing-llama.exe"),
                "WHISPER_SERVER_BIN": str(self.root / "missing-whisper.exe"),
                "WHISPER_SERVER_MODEL": str(self.root / "missing-model.bin"),
            },
        )

        with patch.object(manager, "_port_is_available", return_value=True):
            with self.assertRaises(AIServerError) as raised:
                manager._preflight(("llama", "whisper"))

        message = str(raised.exception)
        self.assertIn("llama: executable not found", message)
        self.assertIn("whisper: executable not found", message)
        self.assertIn("whisper: required model not found", message)

    async def test_start_all_uses_two_owned_processes_and_console_flags(self):
        llama_process = FakeProcess(101)
        whisper_process = FakeProcess(102)
        process_factory = AsyncMock(side_effect=(llama_process, whisper_process))

        with (
            patch.object(self.manager, "_port_is_available", return_value=True),
            patch.object(self.manager, "_wait_until_ready", new=AsyncMock()),
            patch(
                "app.ai_servers.asyncio.create_subprocess_exec",
                process_factory,
            ),
        ):
            started = await self.manager.start("all")

        self.assertEqual(started, ("llama", "whisper"))
        self.assertEqual(process_factory.await_count, 2)
        llama_call = process_factory.await_args_list[0]
        self.assertEqual(llama_call.args[:2], (str(self.llama), "serve"))
        if os.name == "nt":
            flags = llama_call.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NEW_CONSOLE)
            self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)

    async def test_stop_only_signals_owned_processes_and_awaits_them(self):
        llama_process = FakeProcess(201)
        whisper_process = FakeProcess(202)
        self.manager._processes = {
            "llama": llama_process,
            "whisper": whisper_process,
        }

        stopped = await self.manager.stop("all")

        self.assertEqual(stopped, ("llama", "whisper"))
        self.assertEqual(self.manager._processes, {})
        if os.name == "nt":
            self.assertEqual(llama_process.signals, [signal.CTRL_BREAK_EVENT])
            self.assertEqual(whisper_process.signals, [signal.CTRL_BREAK_EVENT])

    async def test_partial_launch_failure_rolls_back_new_process(self):
        llama_process = FakeProcess(301)
        process_factory = AsyncMock(
            side_effect=(llama_process, OSError("cannot create whisper process"))
        )

        with (
            patch.object(self.manager, "_port_is_available", return_value=True),
            patch(
                "app.ai_servers.asyncio.create_subprocess_exec",
                process_factory,
            ),
        ):
            with self.assertRaises(OSError):
                await self.manager.start("all")

        self.assertEqual(self.manager._processes, {})
        self.assertEqual(llama_process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
