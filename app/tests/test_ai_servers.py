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

    def poll(self) -> int | None:
        return self.returncode


class AICommandParserTests(unittest.TestCase):
    def test_defaults_target_to_all(self):
        parsed = parse_ai_command("AI START")

        self.assertEqual(parsed.action, "start")
        self.assertEqual(parsed.target, "all")

    def test_accepts_each_action_and_target(self):
        for action in ("start", "status", "stop", "restart"):
            for target in ("all", "llama", "supertonic"):
                with self.subTest(action=action, target=target):
                    parsed = parse_ai_command(f"ai {action} {target}")
                    self.assertEqual((parsed.action, parsed.target), (action, target))

    def test_rejects_invalid_syntax(self):
        for command in ("ai", "ai launch", "ai start both", "ai stop all now", "ai setup llama"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                parse_ai_command(command)

    def test_setup_defaults_to_all_and_accepts_targeted_setup(self):
        parsed = parse_ai_command("AI SETUP")

        self.assertEqual(parsed.action, "setup")
        self.assertEqual(parsed.target, "all")
        self.assertEqual(parse_ai_command("ai setup stt").target, "stt")
        self.assertEqual(parse_ai_command("ai setup supertonic").target, "supertonic")


class AIServerManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.llama = self.root / "tools" / "llama.exe"
        self.llama.parent.mkdir(parents=True, exist_ok=True)
        self.llama.touch()
        self.supertonic = self.root / "tools" / "supertonic.exe"
        self.supertonic.touch()
        self.environment = {
            "LLAMA_SERVER_BIN": str(self.llama),
            "SUPERTONIC_SERVER_BIN": str(self.supertonic),
        }
        self.manager = AIServerManager(root=self.root, environment=self.environment)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_llama_executable_comes_from_environment_variable(self):
        llama_spec = self.manager._specs()["llama"]

        self.assertEqual(llama_spec.executable, self.llama)
        self.assertEqual(llama_spec.arguments[0], "serve")
        self.assertEqual(
            llama_spec.arguments[
                llama_spec.arguments.index("--parallel") + 1
            ],
            "1",
        )
        self.assertEqual(self.manager._specs()["supertonic"].executable, self.supertonic)

    def test_preflight_reports_all_missing_dependencies(self):
        manager = AIServerManager(
            root=self.root,
            environment={
                "LLAMA_SERVER_BIN": str(self.root / "missing-llama.exe"),
            },
        )

        with patch.object(manager, "_port_is_available", return_value=True):
            with self.assertRaises(AIServerError) as raised:
                manager._preflight(("llama",))

        message = str(raised.exception)
        self.assertIn("llama: executable not found", message)

    async def test_start_all_uses_owned_llama_process_and_console_flags(self):
        llama_process = FakeProcess(101)
        process_factory = AsyncMock(return_value=llama_process)

        with (
            patch.object(self.manager, "_port_is_available", return_value=True),
            patch.object(self.manager, "_wait_until_ready", new=AsyncMock()),
            patch(
                "app.ai_servers.asyncio.create_subprocess_exec",
                process_factory,
            ),
        ):
            started = await self.manager.start("all")

        self.assertEqual(started, ("llama", "supertonic"))
        self.assertEqual(process_factory.await_count, 2)
        llama_call = process_factory.await_args_list[0]
        self.assertEqual(llama_call.args[:2], (str(self.llama), "serve"))
        if os.name == "nt":
            flags = llama_call.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NEW_CONSOLE)
            self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)

    async def test_stop_only_signals_owned_processes_and_awaits_them(self):
        llama_process = FakeProcess(201)
        self.manager._processes = {"llama": llama_process}

        stopped = await self.manager.stop("all")

        self.assertEqual(stopped, ("llama",))
        self.assertEqual(self.manager._processes, {})
        if os.name == "nt":
            self.assertEqual(llama_process.signals, [signal.CTRL_BREAK_EVENT])

    async def test_detached_start_uses_popen_and_is_not_owned_by_event_loop(self):
        llama_process = FakeProcess(301)

        with (
            patch.object(
                self.manager,
                "_port_is_available",
                side_effect=[True, False],
            ),
            patch("app.ai_servers.subprocess.Popen", return_value=llama_process) as popen,
        ):
            started = await self.manager.start_detached("llama")

        self.assertEqual(started, ("llama",))
        self.assertEqual(self.manager._processes, {})
        call = popen.call_args
        self.assertEqual(call.args[0][:2], (str(self.llama), "serve"))
        if os.name == "nt":
            flags = call.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NEW_CONSOLE)
            self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)

    async def test_launch_failure_leaves_no_owned_process(self):
        process_factory = AsyncMock(side_effect=OSError("cannot create llama process"))

        with (
            patch.object(self.manager, "_port_is_available", return_value=True),
            patch(
                "app.ai_servers.asyncio.create_subprocess_exec",
                process_factory,
            ),
        ):
            with self.assertRaises(AIServerError):
                await self.manager.start("all")

        self.assertEqual(self.manager._processes, {})


if __name__ == "__main__":
    unittest.main()
