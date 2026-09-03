import asyncio
import base64
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from fastapi import HTTPException

from app import main


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
        self.messages: list[str] = []
        self.log_patch = patch.object(main, "log", self.messages.append)
        self.log_patch.start()

    def tearDown(self):
        main.pending_scene_commands.clear()
        main.unity_ws = None
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
        )
        for command in invalid_commands:
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    main.parse_set_game_command(command)

    async def test_matching_applied_ack_logs_success_only_after_ack(self):
        socket = FakeWebSocket()
        main.unity_ws = socket

        command_task = asyncio.create_task(main.set_game("doctor", timeout_seconds=0.2))
        await asyncio.sleep(0)

        self.assertEqual(len(socket.sent_messages), 1)
        request = socket.sent_messages[0]
        self.assertEqual(request["sequence"], 1)
        self.assertEqual(request["type"], "load_scene")
        self.assertEqual(request["sceneId"], "doctor")
        self.assertTrue(request["commandId"])
        self.assertTrue(request["issuedAtUtc"].endswith("Z"))
        self.assertFalse(any("Scene changed" in message for message in self.messages))

        accepted = main.handle_unity_acknowledgement(
            {
                "commandId": request["commandId"],
                "sequence": request["sequence"],
                "status": "applied",
                "sceneId": "doctor",
                "phase": "ready",
                "appliedAtUtc": "2026-08-25T00:00:00Z",
                "errorCode": "",
                "errorMessage": "",
            }
        )

        self.assertTrue(accepted)
        self.assertTrue(await command_task)
        self.assertIn("[Server] Scene changed to 'doctor'.", self.messages)
        self.assertFalse(main.pending_scene_commands)

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
        }

        self.assertFalse(main.handle_unity_acknowledgement(acknowledgement))
        self.assertFalse(command_task.done())

        acknowledgement["sequence"] = request["sequence"]
        self.assertTrue(main.handle_unity_acknowledgement(acknowledgement))
        self.assertTrue(await command_task)

    async def test_timeout_removes_request_and_late_ack_is_ignored(self):
        socket = FakeWebSocket()
        main.unity_ws = socket

        self.assertFalse(await main.set_game("lawyer-office", timeout_seconds=0.01))
        request = socket.sent_messages[0]
        self.assertFalse(main.pending_scene_commands)
        self.assertTrue(any("timed out after 0.01 seconds" in message for message in self.messages))

        self.assertFalse(
            main.handle_unity_acknowledgement(
                {
                    "commandId": request["commandId"],
                    "sequence": request["sequence"],
                    "status": "applied",
                    "sceneId": "lawyer-office",
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

    def tearDown(self):
        self.log_patch.stop()
        self.environment_patch.stop()
        self.temporary_directory.cleanup()

    async def assert_recording_rejected(self, event: dict, status_code: int = 422):
        with self.assertRaises(HTTPException) as raised:
            await main.telemetry(event)
        self.assertEqual(raised.exception.status_code, status_code)

    def test_relative_recording_directory_resolves_from_backend_root(self):
        with tempfile.TemporaryDirectory() as backend_root:
            with patch.object(main, "BACKEND_ROOT", Path(backend_root)):
                with patch.dict(os.environ, {"RECORDINGS_DIR": "saved/audio"}):
                    self.assertEqual(
                        main.get_recordings_directory(),
                        (Path(backend_root) / "saved" / "audio").resolve(),
                    )

    async def test_valid_recording_is_saved_as_readable_wav(self):
        round_id = "1234567890abcdef1234567890abcdef"
        wav_bytes = build_wav(fill_byte=7)
        event = build_defense_recording_event(wav_bytes, round_id=round_id)
        encoded_audio = event["payload"]["audio"]["dataBase64"]

        result = await main.telemetry(event)

        self.assertEqual(result, {"status": "ok"})
        destination = self.recordings_directory / f"lawyer-defense-{round_id}.wav"
        self.assertEqual(destination.read_bytes(), wav_bytes)
        with wave.open(str(destination), "rb") as recording:
            self.assertEqual(recording.getnchannels(), 1)
            self.assertEqual(recording.getsampwidth(), 2)
            self.assertEqual(recording.getframerate(), 16000)
            self.assertEqual(recording.getnframes(), 160)
        self.assertFalse(any(encoded_audio in str(message) for message in self.messages))

    async def test_repeated_round_upload_replaces_one_file(self):
        round_id = "c" * 32
        first_wav = build_wav(fill_byte=1)
        second_wav = build_wav(fill_byte=2)

        await main.telemetry(build_defense_recording_event(first_wav, round_id=round_id))
        await main.telemetry(build_defense_recording_event(second_wav, round_id=round_id))

        recordings = list(self.recordings_directory.glob("*.wav"))
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0].read_bytes(), second_wav)
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

        with patch.object(main, "MAX_RECORDING_BYTES", 64):
            await self.assert_recording_rejected(event)

        self.assertFalse(list(self.recordings_directory.iterdir()))

    async def test_write_failure_returns_server_error_and_removes_temporary_file(self):
        event = build_defense_recording_event()

        with patch.object(main.os, "replace", side_effect=OSError("disk full")):
            await self.assert_recording_rejected(event, status_code=500)

        self.assertFalse(list(self.recordings_directory.iterdir()))

    async def test_temporary_file_creation_failure_returns_server_error(self):
        event = build_defense_recording_event()

        with patch.object(
            main.tempfile,
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
        self.assertIn(event, self.messages)
        self.assertFalse(list(self.recordings_directory.iterdir()))


if __name__ == "__main__":
    unittest.main()
