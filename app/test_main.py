import asyncio
import unittest
from unittest.mock import patch

from app import main


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


if __name__ == "__main__":
    unittest.main()
