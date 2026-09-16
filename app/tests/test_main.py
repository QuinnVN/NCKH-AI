import asyncio
import base64
import io
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import wave

from fastapi import HTTPException

from app import main
from app import lawyer_assessment
from app import save_recording
from app.ai_servers import AIServerStatus
from app.config import get_settings


class ConsoleLoggingTests(unittest.IsolatedAsyncioTestCase):
    def test_log_emits_plain_text_without_json_wrapper(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        backend_logger = main.logger
        original_handlers = backend_logger.handlers[:]
        original_propagate = backend_logger.propagate
        original_level = backend_logger.level
        backend_logger.handlers = [handler]
        backend_logger.propagate = False
        backend_logger.setLevel(logging.INFO)
        try:
            main.log("[Server] Unity connected")
        finally:
            backend_logger.handlers = original_handlers
            backend_logger.propagate = original_propagate
            backend_logger.setLevel(original_level)

        self.assertEqual(output.getvalue(), "[Server] Unity connected\n")

    async def test_logging_is_configured_inside_prompt_safe_context(self):
        events: list[str] = []

        class RecordingContext:
            def __enter__(self):
                events.append("prompt-safe-enter")

            def __exit__(self, *args):
                events.append("prompt-safe-exit")

        with (
            patch.object(main, "patch_stdout", return_value=RecordingContext()),
            patch.object(
                main.logging,
                "basicConfig",
                side_effect=lambda **kwargs: events.append(
                    f"logging-configured:{kwargs['format']}"
                ),
            ),
            patch.object(
                main,
                "_run_backend",
                new=AsyncMock(side_effect=lambda: events.append("backend-run")),
                create=True,
            ),
        ):
            await main.main()

        self.assertEqual(
            events,
            [
                "prompt-safe-enter",
                "logging-configured:%(message)s",
                "backend-run",
                "prompt-safe-exit",
            ],
        )


class ApiRouteRegistrationTests(unittest.TestCase):
    def test_required_backend_routes_are_registered_on_served_app(self):
        registered_routes = {route.path for route in main.app.routes}

        self.assertIn("/api/health", registered_routes)
        self.assertIn("/api/telemetry", registered_routes)
        self.assertIn("/api/ai/respond", registered_routes)
        self.assertIn("/api/ai/initial-career-assessment", registered_routes)
        self.assertNotIn("/api/ai/career-assessment", registered_routes)
        self.assertIn("/ws/ctrl", registered_routes)

    def test_chat_request_rejects_unbounded_or_injected_fields(self):
        valid = main.ChatRequest(
            transcript="hello",
            game_id="lawyer",
            conversation=[{"role": "user", "content": "context"}],
        )
        self.assertEqual(valid.game_id, "lawyer")
        with self.assertRaises(ValueError):
            main.ChatRequest(transcript="", game_id="lawyer")
        with self.assertRaises(ValueError):
            main.ChatRequest(transcript="hello", game_id="lawyer", injected="bad")

    def test_optional_token_authentication(self):
        with patch.dict(os.environ, {"BACKEND_API_TOKEN": "test-token"}):
            with self.assertRaises(HTTPException) as raised:
                main._require_http_auth(None)
            self.assertEqual(raised.exception.status_code, 401)
            main._require_http_auth("Bearer test-token")

    def test_chat_request_rejects_unbounded_or_injected_fields(self):
        valid = main.ChatRequest(
            transcript="hello",
            game_id="lawyer",
            conversation=[{"role": "user", "content": "context"}],
        )
        self.assertEqual(valid.game_id, "lawyer")
        with self.assertRaises(ValueError):
            main.ChatRequest(transcript="", game_id="lawyer")
        with self.assertRaises(ValueError):
            main.ChatRequest(transcript="hello", game_id="lawyer", injected="bad")

    def test_optional_token_authentication(self):
        with patch.dict(os.environ, {"BACKEND_API_TOKEN": "test-token"}):
            with self.assertRaises(HTTPException) as raised:
                main._require_http_auth(None)
            self.assertEqual(raised.exception.status_code, 401)
            main._require_http_auth("Bearer test-token")


def build_wav(
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    sample_width: int = 2,
    frame_count: int = 160,
    fill_byte: int = 0,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(sample_width)
        recording.setframerate(sample_rate)
        recording.writeframes(
            bytes([fill_byte]) * frame_count * channels * sample_width
        )
    return output.getvalue()


def build_defense_recording_event(
    wav_bytes: bytes | None = None,
    *,
    round_id: str = "a" * 32,
) -> dict:
    recording = wav_bytes if wav_bytes is not None else build_wav()
    return {
        "schemaVersion": 1,
        "eventId": "b" * 32,
        "sessionId": "test-session",
        "occurredAtUtc": "2026-08-31T00:00:00Z",
        "sceneId": "lawyer-office",
        "phase": "running",
        "eventType": main.DEFENSE_RECORDING_EVENT_TYPE,
        "payload": {
            "roundId": round_id,
            "caseId": "placeholder-lawyer-case",
            "interviewRestartCount": 0,
            "assessmentContext": {
                "caseSummary": "Một vụ cháy xảy ra tại phòng CLB.",
                "investigationObjective": "Xây dựng lời bào chữa dựa trên chứng cứ.",
                "defenseConclusion": "Chưa đủ chứng cứ để kết luận Minh gây cháy.",
                "orderedEvidence": [
                    {"evidenceId": "clue-a", "title": "A", "description": "Dữ kiện A.", "strength": "strong"},
                    {"evidenceId": "clue-b", "title": "B", "description": "Dữ kiện B.", "strength": "strong"},
                    {"evidenceId": "clue-c", "title": "C", "description": "Dữ kiện C.", "strength": "strong"},
                    {"evidenceId": "clue-d", "title": "D", "description": "Dữ kiện D.", "strength": "weak"},
                ],
                "reasoningCards": [
                    {"reasoningId": "reasoning-a", "title": "Suy luận", "description": "Các dữ kiện hỗ trợ kết luận."}
                ],
                "sampleAnswers": ["Minh rời phòng trước khi có dấu hiệu cháy."],
            },
            "audio": {
                "fileName": "ignored-client-name.wav",
                "mimeType": "audio/wav",
                "encoding": "pcm_s16le",
                "sampleRateHz": 16000,
                "channels": 1,
                "durationSeconds": 0.01,
                "endedEarly": False,
                "dataBase64": base64.b64encode(recording).decode("ascii"),
            },
        },
    }

class FakeWebSocket:
    def __init__(self, send_error: Exception | None = None):
        self.sent_messages: list[dict] = []
        self.send_error = send_error

    async def send_json(self, data: dict):
        if self.send_error is not None:
            raise self.send_error
        self.sent_messages.append(data)


class SetGameCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.unity_ws = None
        main.pending_scene_commands.clear()
        main.next_command_sequence = 1
        self.original_participant_activity = main.participant_manager.activity
        self.messages: list[str] = []
        self.log_patch = patch.object(main, "log", self.messages.append)
        self.log_patch.start()

    def tearDown(self):
        main.pending_scene_commands.clear()
        main.unity_ws = None
        main.participant_manager.activity = self.original_participant_activity
        self.log_patch.stop()

    def test_parse_set_game_accepts_and_normalizes_catalog_ids(self):
        for scene_id in main.VALID_SCENE_IDS:
            with self.subTest(scene_id=scene_id):
                self.assertEqual(
                    main.parse_set_game_command(f"set_game {scene_id.upper()}"),
                    scene_id,
                )

    def test_parse_set_game_rejects_invalid_syntax_and_unknown_scene(self):
        invalid_commands = (
            "set_game",
            "set_game doctor extra",
            "load_game doctor",
            "set_game courtroom",
            "set_game tutorial",
        )
        for command in invalid_commands:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    main.parse_set_game_command(command)

    async def test_ready_or_autostarted_load_ack_starts_gameplay_scenes(self):
        for scene_id in ("clinic", "doctor", "lawyer"):
            for load_phase in ("ready", "running"):
                with self.subTest(scene_id=scene_id, load_phase=load_phase):
                    await self._assert_load_then_start(scene_id, load_phase)
        await self._assert_load_then_start("sale", "ready")

    async def _assert_load_then_start(self, scene_id: str, load_phase: str):
        socket = FakeWebSocket()
        main.unity_ws = socket
        main.next_command_sequence = 1

        command_task = asyncio.create_task(
            main.set_game(scene_id, timeout_seconds=0.2)
        )
        await asyncio.sleep(0)

        self.assertEqual(len(socket.sent_messages), 1)
        request = socket.sent_messages[0]
        self.assertEqual(request["sequence"], 1)
        self.assertEqual(request["type"], "load_scene")
        self.assertEqual(request["sceneId"], scene_id)
        self.assertTrue(request["commandId"])
        self.assertTrue(request["issuedAtUtc"].endswith("Z"))

        self.assertTrue(
            main.handle_unity_acknowledgement(
                {
                    "commandId": request["commandId"],
                    "sequence": request["sequence"],
                    "status": "applied",
                    "sceneId": scene_id,
                    "phase": load_phase,
                    "appliedAtUtc": "2026-08-25T00:00:00Z",
                    "errorCode": "",
                    "errorMessage": "",
                }
            )
        )

        await asyncio.sleep(0)
        self.assertEqual(len(socket.sent_messages), 2)
        start_request = socket.sent_messages[1]
        self.assertEqual(start_request["sequence"], 2)
        self.assertEqual(start_request["type"], "start_scene")
        self.assertEqual(start_request["sceneId"], scene_id)

        self.assertTrue(
            main.handle_unity_acknowledgement(
                {
                    "commandId": start_request["commandId"],
                    "sequence": start_request["sequence"],
                    "status": "applied",
                    "sceneId": scene_id,
                    "phase": "running",
                    "appliedAtUtc": "2026-08-25T00:00:01Z",
                    "errorCode": "",
                    "errorMessage": "",
                }
            )
        )

        self.assertTrue(await command_task)
        self.assertIn(
            f"[Server] Scene '{scene_id}' loaded and started.",
            self.messages,
        )
        self.assertFalse(main.pending_scene_commands)

    async def test_failed_acknowledgement_is_accepted_for_pending_command(self):
        socket = FakeWebSocket()
        main.unity_ws = socket
        future = asyncio.get_running_loop().create_future()
        main.pending_scene_commands["failed-command"] = main.PendingSceneCommand(
            sequence=1,
            scene_id="clinic",
            acknowledgement=future,
            owner=socket,
        )

        accepted = main.handle_unity_acknowledgement(
            {
                "commandId": "failed-command",
                "sequence": 1,
                "status": "failed",
                "sceneId": "clinic",
                "errorCode": "scene_load_failed",
                "errorMessage": "Scene did not become active.",
            },
            source=socket,
        )

        self.assertTrue(accepted)
        self.assertEqual(future.result()["status"], "failed")

    def test_applied_command_requires_expected_phase(self):
        self.assertFalse(
            main.scene_command_succeeded(
                {
                    "status": "applied",
                    "sceneId": "doctor",
                    "phase": "ready",
                },
                "doctor",
                "start_scene",
                "running",
            )
        )
        self.assertTrue(any("instead of 'running'" in message for message in self.messages))

    async def test_rejected_ack_logs_failure_without_success(self):
        socket = FakeWebSocket()
        main.unity_ws = socket
        command_task = asyncio.create_task(main.set_game("clinic", timeout_seconds=0.2))
        await asyncio.sleep(0)
        request = socket.sent_messages[0]

        main.handle_unity_acknowledgement(
            {
                "commandId": request["commandId"],
                "sequence": request["sequence"],
                "status": "rejected",
                "sceneId": "standby",
                "errorCode": "unknown_scene",
                "errorMessage": "Scene is unavailable.",
            }
        )

        self.assertFalse(await command_task)
        self.assertTrue(any("unknown_scene" in message for message in self.messages))
        self.assertFalse(any("Scene changed" in message for message in self.messages))

    async def test_mismatched_ack_is_ignored_until_matching_ack_arrives(self):
        socket = FakeWebSocket()
        main.unity_ws = socket
        command_task = asyncio.create_task(main.set_game("standby", timeout_seconds=0.2))
        await asyncio.sleep(0)
        request = socket.sent_messages[0]
        acknowledgement = {
            "commandId": request["commandId"],
            "sequence": request["sequence"] + 1,
            "status": "applied",
            "sceneId": "standby",
            "phase": "standby",
        }

        self.assertFalse(main.handle_unity_acknowledgement(acknowledgement))
        self.assertFalse(command_task.done())

        acknowledgement["sequence"] = request["sequence"]
        self.assertTrue(main.handle_unity_acknowledgement(acknowledgement))
        self.assertTrue(await command_task)

    async def test_ack_from_another_connection_is_ignored(self):
        socket = FakeWebSocket()
        other_socket = FakeWebSocket()
        main.unity_ws = socket
        command_task = asyncio.create_task(main.set_game("standby", timeout_seconds=0.2))
        await asyncio.sleep(0)
        request = socket.sent_messages[0]

        self.assertFalse(
            main.handle_unity_acknowledgement(
                {
                    "commandId": request["commandId"],
                    "sequence": request["sequence"],
                    "status": "applied",
                    "sceneId": "standby",
                    "phase": "standby",
                },
                source=other_socket,
            )
        )
        self.assertFalse(command_task.done())
        self.assertTrue(
            main.handle_unity_acknowledgement(
                {
                    "commandId": request["commandId"],
                    "sequence": request["sequence"],
                    "status": "applied",
                    "sceneId": "standby",
                    "phase": "standby",
                },
                source=socket,
            )
        )
        self.assertTrue(await command_task)

    async def test_timeout_removes_request_and_late_ack_is_ignored(self):
        socket = FakeWebSocket()
        main.unity_ws = socket

        self.assertFalse(await main.set_game("lawyer", timeout_seconds=0.01))
        request = socket.sent_messages[0]
        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("timed out after 0.01 seconds" in message for message in self.messages))

        self.assertFalse(
            main.handle_unity_acknowledgement(
                {
                    "commandId": request["commandId"],
                    "sequence": request["sequence"],
                    "status": "applied",
                    "sceneId": "lawyer",
                }
            )
        )
        self.assertFalse(any("Scene changed" in message for message in self.messages))

    async def test_no_connection_reports_error_without_creating_request(self):
        self.assertFalse(await main.set_game("doctor"))
        self.assertFalse(main.pending_scene_commands)
        self.assertIn("[Server] Cannot change scene: Unity is not connected.", self.messages)

    async def test_send_failure_is_reported_and_request_is_removed(self):
        main.unity_ws = FakeWebSocket(RuntimeError("socket closed"))

        self.assertFalse(await main.set_game("doctor"))
        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("socket closed" in message for message in self.messages))

    def test_malformed_ack_is_reported(self):
        self.assertFalse(main.handle_unity_acknowledgement("not an object"))
        self.assertFalse(main.handle_unity_acknowledgement({"commandId": "missing-sequence"}))
        self.assertEqual(len(self.messages), 2)


class ResetCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.unity_ws = None
        main.pending_scene_commands.clear()
        main.next_command_sequence = 1
        self.messages: list[str] = []
        self.log_patch = patch.object(main, "log", self.messages.append)
        self.log_patch.start()

    def tearDown(self):
        main.pending_scene_commands.clear()
        main.unity_ws = None
        self.log_patch.stop()

    def test_parse_reset_accepts_gameplay_ids_and_all(self):
        expected_targets = (main.RESET_ALL_TARGET, "clinic", "doctor", "lawyer")
        for target in expected_targets:
            with self.subTest(target=target):
                self.assertEqual(main.parse_reset_command(f"reset {target.upper()}"), target)

    def test_parse_reset_rejects_standby_invalid_syntax_and_unknown_scene(self):
        invalid_commands = (
            "reset",
            "reset doctor extra",
            "reset standby",
            "reset courtroom",
            "reset_game doctor",
        )
        for command in invalid_commands:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    main.parse_reset_command(command)

    async def test_matching_applied_ack_requires_standby(self):
        socket = FakeWebSocket()
        main.unity_ws = socket

        command_task = asyncio.create_task(main.reset_game("doctor", timeout_seconds=0.2))
        await asyncio.sleep(0)

        request = socket.sent_messages[0]
        self.assertEqual(request["type"], "reset_scene")
        self.assertEqual(request["sceneId"], "doctor")
        self.assertEqual(request["sequence"], 1)
        self.assertTrue(request["issuedAtUtc"].endswith("Z"))

        main.handle_unity_acknowledgement(
            {
                "commandId": request["commandId"],
                "sequence": request["sequence"],
                "status": "applied",
                "sceneId": "standby",
                "phase": "standby",
            }
        )

        self.assertTrue(await command_task)
        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("returned Unity to standby" in message for message in self.messages))

    async def test_applied_ack_for_non_standby_scene_is_failure(self):
        socket = FakeWebSocket()
        main.unity_ws = socket
        command_task = asyncio.create_task(main.reset_game("all", timeout_seconds=0.2))
        await asyncio.sleep(0)
        request = socket.sent_messages[0]

        main.handle_unity_acknowledgement(
            {
                "commandId": request["commandId"],
                "sequence": request["sequence"],
                "status": "applied",
                "sceneId": "doctor",
            }
        )

        self.assertFalse(await command_task)
        self.assertTrue(any("instead of 'standby'" in message for message in self.messages))

    async def test_rejected_reset_ack_logs_failure(self):
        socket = FakeWebSocket()
        main.unity_ws = socket
        command_task = asyncio.create_task(main.reset_game("lawyer", timeout_seconds=0.2))
        await asyncio.sleep(0)
        request = socket.sent_messages[0]

        main.handle_unity_acknowledgement(
            {
                "commandId": request["commandId"],
                "sequence": request["sequence"],
                "status": "rejected",
                "sceneId": "doctor",
                "errorCode": "scene_mismatch",
                "errorMessage": "The requested scene is not active.",
            }
        )

        self.assertFalse(await command_task)
        self.assertTrue(any("scene_mismatch" in message for message in self.messages))

    async def test_reset_timeout_removes_pending_request(self):
        socket = FakeWebSocket()
        main.unity_ws = socket

        self.assertFalse(await main.reset_game("clinic", timeout_seconds=0.01))

        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("timed out after 0.01 seconds" in message for message in self.messages))

    async def test_reset_without_connection_reports_error(self):
        self.assertFalse(await main.reset_game("all"))
        self.assertFalse(main.pending_scene_commands)
        self.assertIn("[Server] Cannot reset scene: Unity is not connected.", self.messages)

    async def test_reset_send_failure_is_reported_and_removed(self):
        main.unity_ws = FakeWebSocket(RuntimeError("socket closed"))

        self.assertFalse(await main.reset_game("doctor"))

        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("socket closed" in message for message in self.messages))

    async def test_reset_timeout_send_failure_and_missing_connection_cleanup(self):
        self.assertFalse(await main.reset_game("clinic"))
        self.assertIn("[Server] Cannot reset scene: Unity is not connected.", self.messages)

        main.unity_ws = FakeWebSocket(RuntimeError("socket closed"))
        self.assertFalse(await main.reset_game("doctor"))
        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("socket closed" in message for message in self.messages))

        socket = FakeWebSocket()
        main.unity_ws = socket
        self.assertFalse(await main.reset_game("all", timeout_seconds=0.01))
        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("timed out after 0.01 seconds" in message for message in self.messages))


class DefenseRecordingTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.recordings_directory = Path(self.temporary_directory.name)
        self.environment_patch = patch.dict(
            os.environ,
            {"RECORDINGS_DIR": self.temporary_directory.name},
        )
        self.environment_patch.start()
        self.messages: list[object] = []
        self.log_patch = patch.object(main, "log", self.messages.append)
        self.log_patch.start()
        self.schedule_patch = patch.object(main, "_schedule_lawyer_processing")
        self.schedule_patch.start()

    def tearDown(self):
        self.schedule_patch.stop()
        self.log_patch.stop()
        self.environment_patch.stop()
        self.temporary_directory.cleanup()

    async def assert_recording_rejected(self, event: dict, status_code: int = 422):
        with self.assertRaises(HTTPException) as raised:
            await main.telemetry(event)
        self.assertEqual(raised.exception.status_code, status_code)

    def test_relative_recording_directory_resolves_from_backend_root(self):
        with tempfile.TemporaryDirectory() as backend_root:
            with patch.object(save_recording, "BACKEND_ROOT", Path(backend_root)):
                with patch.dict(os.environ, {"RECORDINGS_DIR": "saved/audio"}):
                    self.assertEqual(
                        save_recording.get_recordings_directory(),
                        (Path(backend_root) / "saved" / "audio").resolve(),
                    )

    async def test_valid_recording_is_saved_as_readable_wav(self):
        round_id = "1234567890abcdef1234567890abcdef"
        wav_bytes = build_wav(fill_byte=7)
        event = build_defense_recording_event(wav_bytes, round_id=round_id)
        encoded_audio = event["payload"]["audio"]["dataBase64"]

        result = await main.telemetry(event)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["roundId"], round_id)
        self.assertEqual(result["assessmentStatus"], "processing")
        destination = self.recordings_directory / f"lawyer-defense-{round_id}.wav"
        self.assertEqual(destination.read_bytes(), wav_bytes)
        with wave.open(str(destination), "rb") as recording:
            self.assertEqual(recording.getnchannels(), 1)
            self.assertEqual(recording.getsampwidth(), 2)
            self.assertEqual(recording.getframerate(), 16000)
            self.assertEqual(recording.getnframes(), 160)
        self.assertFalse(any(encoded_audio in str(message) for message in self.messages))

    async def test_changed_round_upload_is_rejected_without_replacing_file(self):
        round_id = "c" * 32
        first_wav = build_wav(fill_byte=1)
        second_wav = build_wav(fill_byte=2)

        await main.telemetry(build_defense_recording_event(first_wav, round_id=round_id))
        await self.assert_recording_rejected(
            build_defense_recording_event(second_wav, round_id=round_id),
            status_code=409,
        )

        recordings = list(self.recordings_directory.glob("*.wav"))
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0].read_bytes(), first_wav)
        self.assertFalse(list(self.recordings_directory.glob("*.tmp")))

    async def test_invalid_contract_and_audio_are_rejected_without_files(self):
        invalid_events: list[tuple[str, dict]] = []

        invalid_round = build_defense_recording_event(round_id="not-a-round-id")
        invalid_events.append(("round ID", invalid_round))

        malformed_base64 = build_defense_recording_event()
        malformed_base64["payload"]["audio"]["dataBase64"] = "%%%"
        invalid_events.append(("Base64", malformed_base64))

        wrong_mime = build_defense_recording_event()
        wrong_mime["payload"]["audio"]["mimeType"] = "audio/mpeg"
        invalid_events.append(("MIME type", wrong_mime))

        wrong_encoding = build_defense_recording_event()
        wrong_encoding["payload"]["audio"]["encoding"] = "pcm_f32le"
        invalid_events.append(("encoding", wrong_encoding))

        not_wav = build_defense_recording_event(b"not a wave file")
        invalid_events.append(("WAV container", not_wav))

        wrong_sample_width = build_defense_recording_event(build_wav(sample_width=1))
        invalid_events.append(("sample width", wrong_sample_width))

        wrong_channels = build_defense_recording_event(build_wav(channels=2))
        invalid_events.append(("channels", wrong_channels))

        wrong_sample_rate = build_defense_recording_event(build_wav(sample_rate=8000))
        invalid_events.append(("sample rate", wrong_sample_rate))

        for label, event in invalid_events:
            with self.subTest(label=label):
                await self.assert_recording_rejected(event)

        self.assertFalse(list(self.recordings_directory.iterdir()))

    async def test_oversized_recording_is_rejected(self):
        event = build_defense_recording_event(build_wav(frame_count=160))

        settings = get_settings()
        with patch.object(
            lawyer_assessment,
            "get_settings",
            return_value=type("Settings", (), {"max_recording_bytes": 64, "recordings_dir": settings.recordings_dir})(),
        ):
            await self.assert_recording_rejected(event)

        self.assertFalse(list(self.recordings_directory.iterdir()))

    async def test_write_failure_returns_server_error_and_removes_temporary_file(self):
        event = build_defense_recording_event()

        with patch.object(lawyer_assessment.os, "replace", side_effect=OSError("disk full")):
            await self.assert_recording_rejected(event, status_code=500)

        self.assertFalse(list(self.recordings_directory.iterdir()))

    async def test_temporary_file_creation_failure_returns_server_error(self):
        event = build_defense_recording_event()

        with patch.object(
            lawyer_assessment.tempfile,
            "NamedTemporaryFile",
            side_effect=OSError("read-only directory"),
        ):
            await self.assert_recording_rejected(event, status_code=500)

        self.assertFalse(list(self.recordings_directory.iterdir()))

    async def test_non_recording_telemetry_remains_best_effort(self):
        event = {
            "eventType": "scene.ready",
            "payload": {"roundId": "not-validated-for-other-events"},
        }

        result = await main.telemetry(event)

        self.assertEqual(result, {"status": "ok"})
        self.assertTrue(any("scene.ready" in str(message) for message in self.messages))
        self.assertFalse(list(self.recordings_directory.iterdir()))

    async def test_sales_part2_error_log_includes_specific_error_code(self):
        event = {
            "eventType": "sales.part2.error",
            "payload": {"errorCode": "responder_timeout"},
        }

        result = await main.telemetry(event)

        self.assertEqual(result, {"status": "ok"})
        self.assertTrue(
            any(
                "sales.part2.error" in str(message)
                and "responder_timeout" in str(message)
                for message in self.messages
            )
        )


class LawyerResultProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_background_assessment_finalizes_and_syncs_result(self):
        class RecordingMongo:
            def __init__(self):
                self.documents: dict[str, dict] = {}

            def upsert(self, aggregate):
                self.documents[aggregate["runId"]] = dict(aggregate)

        round_id = "a" * 32
        run_id = "lawyer-background-run"
        record = {
            "roundId": round_id,
            "runId": run_id,
            "caseId": "case-1",
            "interviewRestartCount": 0,
            "assessmentContext": {"orderedEvidence": []},
            "completedEvidenceLinks": [],
            "transcript": "Lời bào chữa hoàn chỉnh.",
            "criteria": {
                "evidenceUse": 30,
                "logicalConnections": 25,
                "conclusionFidelity": 12,
                "clarityAndPersuasiveness": 8,
            },
            "rawScore": 75,
            "restartPenaltyPercent": 0,
            "finalScore": 75,
            "feedbackVi": "Tốt.",
            "createdAtUtc": "2026-09-15T00:00:00Z",
            "updatedAtUtc": "2026-09-15T00:01:00Z",
            "assessmentStatus": "completed",
        }

        with tempfile.TemporaryDirectory() as directory:
            mongo = RecordingMongo()
            store = main.RunResultStore(Path(directory), mongo=mongo)
            original_store = main.run_result_store
            original_participant = main.participant_manager.active
            original_activity = main.participant_manager.activity
            try:
                main.participant_manager.activity = "idle"
                main.participant_manager.assign("Lawyer Tester")
                main.run_result_store = store
                with patch.object(
                    main, "process_lawyer_attempt", new=AsyncMock(return_value=record)
                ):
                    await main._queue_lawyer_processing(round_id)
                    await main.lawyer_processing_tasks[round_id]

                result_path = (
                    Path(directory)
                    / "simulation-results"
                    / f"run-{run_id}.json"
                )
                self.assertTrue(result_path.exists())
                self.assertEqual(mongo.documents[run_id]["status"], "completed")
            finally:
                main.lawyer_processing_tasks.pop(round_id, None)
                main.run_result_store = original_store
                main.participant_manager.active = original_participant
                main.participant_manager.activity = original_activity


class FakeSetupTranscriber:
    def __init__(self, *, ready: bool = False) -> None:
        self.ready = ready
        self.last_error = None if ready else "Run 'ai setup'."

    async def initialize(self) -> bool:
        self.ready = True
        self.last_error = None
        return True


class AISetupCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_installs_initializes_and_resumes_pending_work(self):
        transcriber = FakeSetupTranscriber()
        setup_result = type(
            "SetupResult",
            (),
            {"installed": True, "model_dir": Path("models/zipformer")},
        )()
        with (
            patch.object(main, "sales_transcriber", transcriber),
            patch.object(main, "get_settings", return_value=type("Settings", (), {"sherpa_model_dir": "models/zipformer"})()),
            patch.object(main, "resolve_model_dir", return_value=Path("models/zipformer")),
            patch.object(main, "install_model_bundle", return_value=setup_result),
            patch.object(main, "install_supertonic"),
            patch.object(main, "_resume_sales_processing", new=AsyncMock()) as resume,
            patch.object(main, "log"),
        ):
            succeeded = await main.run_ai_server_command("ai setup")

        self.assertTrue(succeeded)
        self.assertTrue(transcriber.ready)
        resume.assert_awaited_once()

    async def test_health_requires_speech_recognition_readiness(self):
        service = type("LLM", (), {"configured": True, "last_error": None})()
        with (
            patch.object(main, "llm_service", service),
            patch.object(main, "sales_transcriber", FakeSetupTranscriber()),
        ):
            result = await main.health()

        self.assertFalse(result["ready"])
        self.assertFalse(result["sttReady"])


if __name__ == "__main__":
    unittest.main()
