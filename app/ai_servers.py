"""Lifecycle management for the local llama.cpp and whisper.cpp servers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
from typing import Literal, Mapping

from app.config import BACKEND_ROOT


AIServerName = Literal["llama", "whisper"]
AIServerTarget = Literal["all", "llama", "whisper"]
AICommandAction = Literal["start", "status", "stop", "restart"]

SERVER_NAMES: tuple[AIServerName, ...] = ("llama", "whisper")
VALID_ACTIONS = frozenset({"start", "status", "stop", "restart"})
VALID_TARGETS = frozenset({"all", *SERVER_NAMES})
AI_COMMAND_USAGE = "Usage: ai <start|status|stop|restart> [all|llama|whisper]"


class AIServerError(RuntimeError):
    """Report a model-server lifecycle failure that is safe to show in the CLI."""


@dataclass(frozen=True)
class AICommand:
    action: AICommandAction
    target: AIServerTarget


@dataclass(frozen=True)
class AIServerSpec:
    name: AIServerName
    executable: Path
    arguments: tuple[str, ...]
    host: str
    port: int
    required_files: tuple[Path, ...] = ()

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)


@dataclass(frozen=True)
class AIServerStatus:
    name: AIServerName
    running: bool
    host: str
    port: int
    pid: int | None = None
    return_code: int | None = None


def parse_ai_command(command: str) -> AICommand:
    """Parse one operator-console AI lifecycle command."""

    parts = command.strip().lower().split()
    if len(parts) not in (2, 3) or parts[0] != "ai":
        raise ValueError(AI_COMMAND_USAGE)

    action = parts[1]
    target = parts[2] if len(parts) == 3 else "all"
    if action not in VALID_ACTIONS or target not in VALID_TARGETS:
        raise ValueError(AI_COMMAND_USAGE)
    return AICommand(action=action, target=target)  # type: ignore[arg-type]


def _configured_path(value: str, root: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if expanded.is_absolute():
        return expanded
    resolved_command = shutil.which(value)
    if resolved_command:
        return Path(resolved_command)
    return root / expanded


def _default_llama_executable(environment: Mapping[str, str]) -> str:
    local_app_data = environment.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return str(Path(local_app_data) / "Microsoft" / "WindowsApps" / "llama.exe")
    return "llama.exe"


def _positive_float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw_value = environment.get(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


class AIServerManager:
    """Own and supervise model-server processes started by this Python process."""

    def __init__(
        self,
        *,
        root: Path = BACKEND_ROOT,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._root = root.resolve()
        self._environment = dict(os.environ if environment is None else environment)
        self._processes: dict[AIServerName, asyncio.subprocess.Process] = {}
        self._startup_timeout = _positive_float(
            self._environment, "AI_SERVER_START_TIMEOUT_SECONDS", 180.0
        )
        self._shutdown_timeout = _positive_float(
            self._environment, "AI_SERVER_SHUTDOWN_TIMEOUT_SECONDS", 15.0
        )

    def _specs(self) -> dict[AIServerName, AIServerSpec]:
        llama_value = self._environment.get("LLAMA_SERVER_BIN", "").strip()
        if not llama_value:
            llama_value = _default_llama_executable(self._environment)
        llama_executable = _configured_path(llama_value, self._root)
        llama_prefix = ("serve",) if llama_executable.stem.lower() == "llama" else ()

        whisper_value = self._environment.get(
            "WHISPER_SERVER_BIN",
            "whisper.cpp/build/bin/Release/whisper-server.exe",
        ).strip()
        whisper_executable = _configured_path(whisper_value, self._root)
        whisper_model_value = self._environment.get(
            "WHISPER_SERVER_MODEL",
            self._environment.get("PHOWHISPER_MODEL", "ggml-phowhisper-small.bin"),
        ).strip()
        whisper_model = _configured_path(whisper_model_value, self._root)

        return {
            "llama": AIServerSpec(
                name="llama",
                executable=llama_executable,
                arguments=(
                    *llama_prefix,
                    "-hf",
                    "Qwen/Qwen3-4B-GGUF:Q4_K_M",
                    "--alias",
                    "qwen3-4b",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8080",
                    "--ctx-size",
                    "16384",
                    "--jinja",
                    "--reasoning",
                    "auto",
                    "--reasoning-format",
                    "deepseek",
                    "--reasoning-budget",
                    "1024",
                    "--no-context-shift",
                    "-ngl",
                    "99",
                ),
                host="127.0.0.1",
                port=8080,
            ),
            "whisper": AIServerSpec(
                name="whisper",
                executable=whisper_executable,
                arguments=(
                    "-m",
                    str(whisper_model),
                    "-l",
                    "vi",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8081",
                ),
                host="127.0.0.1",
                port=8081,
                required_files=(whisper_model,),
            ),
        }

    @staticmethod
    def _names(target: AIServerTarget) -> tuple[AIServerName, ...]:
        if target == "all":
            return SERVER_NAMES
        return (target,)

    def _is_running(self, name: AIServerName) -> bool:
        process = self._processes.get(name)
        return process is not None and process.returncode is None

    @staticmethod
    def _port_is_available(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, port))
            except OSError:
                return False
        return True

    def _preflight(self, names: tuple[AIServerName, ...]) -> dict[AIServerName, AIServerSpec]:
        specs = self._specs()
        errors: list[str] = []
        for name in names:
            if self._is_running(name):
                continue
            spec = specs[name]
            if not spec.executable.is_file():
                variable = "LLAMA_SERVER_BIN" if name == "llama" else "WHISPER_SERVER_BIN"
                errors.append(
                    f"{name}: executable not found at '{spec.executable}'. Set {variable} to its path."
                )
            for required_file in spec.required_files:
                if not required_file.is_file():
                    errors.append(f"{name}: required model not found at '{required_file}'.")
            if not self._port_is_available(spec.host, spec.port):
                errors.append(
                    f"{name}: {spec.host}:{spec.port} is already in use by an unmanaged process."
                )
        if errors:
            raise AIServerError("Preflight failed:\n" + "\n".join(errors))
        return specs

    @staticmethod
    def _creation_flags() -> int:
        if os.name != "nt":
            return 0
        return subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP

    async def _wait_until_ready(
        self,
        name: AIServerName,
        spec: AIServerSpec,
        process: asyncio.subprocess.Process,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._startup_timeout
        while loop.time() < deadline:
            if process.returncode is not None:
                raise AIServerError(
                    f"{name} exited during startup with code {process.returncode}."
                )
            try:
                _, writer = await asyncio.open_connection(spec.host, spec.port)
            except OSError:
                await asyncio.sleep(0.25)
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        raise AIServerError(
            f"{name} did not listen on {spec.host}:{spec.port} within "
            f"{self._startup_timeout:g} seconds."
        )

    async def start(self, target: AIServerTarget = "all") -> tuple[AIServerName, ...]:
        names = self._names(target)
        specs = self._preflight(names)
        to_start = tuple(name for name in names if not self._is_running(name))
        if not to_start:
            return ()

        started: list[AIServerName] = []
        try:
            for name in to_start:
                spec = specs[name]
                process = await asyncio.create_subprocess_exec(
                    *spec.command,
                    cwd=str(self._root),
                    creationflags=self._creation_flags(),
                )
                self._processes[name] = process
                started.append(name)

            await asyncio.gather(
                *(
                    self._wait_until_ready(name, specs[name], self._processes[name])
                    for name in started
                )
            )
        except BaseException:
            await self._stop_names(tuple(started))
            raise
        return tuple(started)

    async def _request_stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
        except (OSError, ProcessLookupError, ValueError):
            if process.returncode is None:
                try:
                    process.terminate()
                except (OSError, ProcessLookupError):
                    pass

        try:
            await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
        except asyncio.TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
            await process.wait()

    async def _stop_names(
        self, names: tuple[AIServerName, ...]
    ) -> tuple[AIServerName, ...]:
        owned = tuple(
            (name, self._processes[name])
            for name in names
            if name in self._processes
        )
        if not owned:
            return ()

        results = await asyncio.gather(
            *(self._request_stop(process) for _, process in owned),
            return_exceptions=True,
        )
        for name, _ in owned:
            self._processes.pop(name, None)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise AIServerError(
                "Failed to stop one or more managed AI servers: "
                + "; ".join(str(failure) for failure in failures)
            )
        return tuple(name for name, _ in owned)

    async def stop(self, target: AIServerTarget = "all") -> tuple[AIServerName, ...]:
        return await self._stop_names(self._names(target))

    async def restart(self, target: AIServerTarget = "all") -> tuple[AIServerName, ...]:
        await self.stop(target)
        return await self.start(target)

    async def status(self, target: AIServerTarget = "all") -> tuple[AIServerStatus, ...]:
        await asyncio.sleep(0)
        specs = self._specs()
        statuses: list[AIServerStatus] = []
        for name in self._names(target):
            process = self._processes.get(name)
            if process is not None and process.returncode is not None:
                self._processes.pop(name, None)
            running = process is not None and process.returncode is None
            statuses.append(
                AIServerStatus(
                    name=name,
                    running=running,
                    host=specs[name].host,
                    port=specs[name].port,
                    pid=process.pid if running else None,
                    return_code=process.returncode if process is not None else None,
                )
            )
        return tuple(statuses)
