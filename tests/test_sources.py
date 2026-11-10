import asyncio
import json
import os
import struct
import tempfile
import time
import unittest

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from busydiscord.bot_source import BOT_INTENTS, BotSource
from busydiscord.main import main, prepare_bot_token
from busydiscord.rpc_source import RpcConnection, RpcSource, ipc_paths
from busydiscord.sources import SourceError, VoiceSource, observe_source
from busydiscord.token_store import save_private_json


class BotStateTests(unittest.TestCase):
    def setUp(self):
        self.report = MagicMock()
        self.source = BotSource("bot-secret", "42", self.report)
        self.source.dispatch(
            "READY",
            {
                "user": {"id": "bot"},
                "session_id": "session",
                "resume_gateway_url": "wss://gateway.discord.gg",
                "guilds": [{"id": "g1"}, {"id": "g2"}],
            },
        )

    def guild(self, guild_id, states=()):
        return {"id": guild_id, "channels": [{"id": "voice", "name": "General"}], "voice_states": list(states)}

    def test_initial_snapshots_track_target_instead_of_bot_and_wait_for_guilds(self):
        self.assertEqual(self.source.tracker.user_id, "42")
        self.source.dispatch("GUILD_CREATE", self.guild("g1"))
        self.assertEqual(self.source.snapshot()["status"], "unknown")
        self.source.dispatch(
            "GUILD_CREATE", self.guild("g2", [{"user_id": "42", "channel_id": "voice", "self_mute": True}])
        )
        state = self.source.snapshot()
        self.assertEqual((state["status"], state["channel_name"], state["guild_id"]), ("in_voice", "General", "g2"))
        self.assertTrue(state["self_mute"])
        self.source.dispatch("VOICE_STATE_UPDATE", {"user_id": "someone-else", "guild_id": "g2", "channel_id": None})
        self.assertEqual(self.source.snapshot()["status"], "in_voice")

    def test_unavailable_guild_is_unknown_and_restored_snapshot_can_clear_call(self):
        self.source.dispatch("GUILD_CREATE", self.guild("g1"))
        self.source.dispatch("GUILD_CREATE", self.guild("g2", [{"user_id": "42", "channel_id": "voice"}]))
        self.source.dispatch("GUILD_DELETE", {"id": "g2", "unavailable": True})
        self.assertEqual(self.source.snapshot()["status"], "unknown")
        self.source.dispatch("GUILD_CREATE", self.guild("g2"))
        self.assertEqual(self.source.snapshot()["status"], "outside_voice")

    def test_voice_flags_timer_and_disconnect(self):
        self.source.dispatch("GUILD_CREATE", self.guild("g1"))
        self.source.dispatch("GUILD_CREATE", self.guild("g2"))
        self.assertEqual(self.source.snapshot()["status"], "outside_voice")
        state = {"user_id": "42", "guild_id": "g2", "channel_id": "voice"}
        self.source.dispatch("VOICE_STATE_UPDATE", state)
        started = self.source.snapshot()["timer_started_at"]
        self.source.dispatch("VOICE_STATE_UPDATE", {**state, "self_deaf": True, "mute": True, "suppress": True})
        self.assertEqual(self.source.snapshot()["timer_started_at"], started)
        self.assertTrue(self.source.snapshot()["self_deaf"])
        self.source.connected = False
        self.assertEqual(self.source.snapshot()["status"], "unknown")
        self.assertNotIn("bot-secret", str(self.report.call_args_list))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_identifies_with_minimal_intents_and_rejects_fatal_close(self):
        source = BotSource("bot-secret", "42", MagicMock())
        ws = MagicMock()
        ws.receive_json = AsyncMock(return_value={"op": 10, "d": {"heartbeat_interval": 45000}})
        ws.send_json = AsyncMock()
        ws.__aiter__.return_value = []
        ws.close_code = 4004
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=ws)
        context.__aexit__ = AsyncMock(return_value=False)
        http = MagicMock()
        http.ws_connect.return_value = context
        with self.assertRaisesRegex(SourceError, "4004"):
            await source.session(http, "wss://gateway.discord.gg")
        identify = ws.send_json.call_args.args[0]
        self.assertEqual(identify["op"], 2)
        self.assertEqual(identify["d"]["intents"], BOT_INTENTS)
        self.assertEqual(BOT_INTENTS, 129)

    async def test_bot_resumes_and_invalid_session_resets_resume_state(self):
        source = BotSource("bot-secret", "42", MagicMock())
        source.session_id, source.sequence, source.resume_url = "session", 123, "wss://gateway.discord.gg"
        ws = AsyncMock()
        source.heartbeat_acked = False
        self.assertTrue(await source.handle_payload(ws, {"op": 11}))
        self.assertTrue(source.heartbeat_acked)
        await source.handle_payload(ws, {"op": 1})
        ws.send_json.assert_awaited_with({"op": 1, "d": 123})
        self.assertFalse(await source.handle_payload(ws, {"op": 9, "d": False}))
        self.assertIsNone(source.session_id)
        self.assertIsNone(source.sequence)
        self.assertIsNone(source.resume_url)

    async def test_missing_heartbeat_ack_closes_gateway(self):
        source = BotSource("secret", "42", MagicMock())
        source.heartbeat_acked = False
        ws = AsyncMock()
        with patch("busydiscord.bot_source.asyncio.sleep", new_callable=AsyncMock):
            await source.heartbeat(ws, 45)
        ws.close.assert_awaited_once()
        ws.send_json.assert_not_awaited()

    async def test_bot_reconnect_uses_resume_with_sequence(self):
        source = BotSource("bot-secret", "42", MagicMock())
        source.session_id, source.sequence = "session", 321
        ws = MagicMock()
        ws.receive_json = AsyncMock(return_value={"op": 10, "d": {"heartbeat_interval": 45000}})
        ws.send_json = AsyncMock()
        ws.__aiter__.return_value = []
        ws.close_code = None
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=ws)
        context.__aexit__ = AsyncMock(return_value=False)
        http = MagicMock()
        http.ws_connect.return_value = context
        await source.session(http, "wss://gateway.discord.gg")
        ws.send_json.assert_awaited_once_with(
            {
                "op": 6,
                "d": {"token": "bot-secret", "session_id": "session", "seq": 321},
            }
        )

    async def test_rpc_errors_do_not_expose_server_message_or_credentials(self):
        rpc = RpcConnection("123")
        rpc.send = AsyncMock()

        async def receive():
            payload = rpc.send.call_args.args[1]
            return 1, {"nonce": payload["nonce"], "evt": "ERROR", "data": {"code": 4006, "message": "sensitive-token"}}

        rpc.receive = AsyncMock(side_effect=receive)
        with self.assertRaises(SourceError) as error:
            await rpc.command("AUTHENTICATE", {"access_token": "sensitive-token"})
        self.assertNotIn("sensitive-token", str(error.exception))
        self.assertIn("4006", str(error.exception))

    @unittest.skipIf(os.name == "nt", "Unix socket integration test")
    async def test_rpc_handles_fragmented_frames_ping_and_matching_nonce(self):
        failures = []
        finished = asyncio.Event()

        async def read(reader):
            opcode, size = struct.unpack("<II", await reader.readexactly(8))
            return opcode, json.loads(await reader.readexactly(size))

        async def send(writer, opcode, payload):
            data = json.dumps(payload).encode()
            packet = struct.pack("<II", opcode, len(data)) + data
            writer.write(packet[:5])
            await writer.drain()
            await asyncio.sleep(0)
            writer.write(packet[5:])
            await writer.drain()

        async def handler(reader, writer):
            try:
                opcode, handshake = await read(reader)
                self.assertEqual((opcode, handshake), (0, {"v": 1, "client_id": "123"}))
                await send(writer, 1, {"evt": "READY"})
                opcode, command = await read(reader)
                self.assertEqual((opcode, command["cmd"]), (1, "GET_SELECTED_VOICE_CHANNEL"))
                await send(writer, 3, {"ping": "value"})
                self.assertEqual(await read(reader), (4, {"ping": "value"}))
                await send(writer, 1, {"evt": "DISPATCH", "nonce": "other", "data": {}})
                await send(writer, 1, {"nonce": command["nonce"], "data": {"id": "voice"}})
            except Exception as exc:
                failures.append(exc)
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "rpc.sock")
            async with await asyncio.start_unix_server(handler, path):
                with patch.dict(os.environ, {"DISCORD_RPC_PATH": path}):
                    rpc = RpcConnection("123")
                    try:
                        await rpc.connect()
                        self.assertEqual(await rpc.command("GET_SELECTED_VOICE_CHANNEL"), {"id": "voice"})
                    finally:
                        await rpc.close()
                    await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(failures, [])

    async def test_observer_cancels_source_and_releases_display(self):
        source = VoiceSource(MagicMock())

        async def run():
            source.authenticated = True
            await asyncio.Event().wait()

        source.run = AsyncMock(side_effect=run)
        display = AsyncMock()
        with patch("busydiscord.sources.BarDisplay", return_value=display):
            result = await observe_source(source, seconds=0.01, bar=object())
        self.assertEqual(result, 0)
        display.close.assert_awaited_once()


class RpcStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_rotates_tokens_and_binds_authorization_to_client_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rpc.json"
            save_private_json(
                path, {"client_id": "123", "access_token": "old", "refresh_token": "refresh", "expires_at": 0}
            )
            source = RpcSource("123", "client-secret", path, MagicMock())
            response = MagicMock()
            response.status = 200
            response.json = AsyncMock(
                return_value={"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}
            )
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            context.__aexit__ = AsyncMock(return_value=False)
            http = MagicMock()
            http.post.return_value = context
            self.assertEqual(await source.token(http), "new")
            self.assertEqual(http.post.call_args.kwargs["data"]["grant_type"], "refresh_token")
            self.assertEqual(json.loads(path.read_text())["refresh_token"], "new-refresh")
            self.assertGreater(json.loads(path.read_text())["expires_at"], time.time())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            source.client_id = "456"
            with self.assertRaisesRegex(SourceError, "Client ID changed"):
                await source.token(http)

    async def test_rpc_tracks_only_own_flags_and_clears_guild_call_on_exit(self):
        source = RpcSource("123", "secret", Path("unused"), MagicMock())
        source.tracker.user_id = "42"
        source.connected = True
        channel = {
            "id": "voice",
            "guild_id": "guild",
            "name": "General",
            "voice_states": [
                {"user": {"id": "other"}, "voice_state": {"self_mute": False}},
                {"user": {"id": "42"}, "voice_state": {"self_mute": True, "self_deaf": True}},
            ],
        }
        source.record_channel(channel)
        self.assertTrue(source.snapshot()["self_deaf"])
        start = source.snapshot()["timer_started_at"]
        source.record_channel({**channel, "id": "next"})
        self.assertEqual(source.snapshot()["timer_started_at"], start)
        source.record_channel(None)
        self.assertEqual(source.snapshot()["status"], "outside_voice")
        source.record_channel({**channel, "voice_states": []})
        self.assertEqual(source.snapshot()["status"], "unknown")


class ModeConfigurationTests(unittest.TestCase):
    def test_cli_mode_overrides_environment_and_never_reads_user_token(self):
        for arguments, environment, expected in [
            (["--mode", "rpc"], {"DISCORD_MODE": "bot"}, "rpc"),
            ([], {"DISCORD_MODE": "bot"}, "bot"),
        ]:
            with self.subTest(mode=expected):
                source = object()
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("busydiscord.main.load_dotenv"),
                    patch("busydiscord.main.source_for_mode", return_value=source) as build,
                    patch("busydiscord.main.prepare_token") as prepare,
                    patch("busydiscord.main.observe_source", new_callable=AsyncMock, return_value=0) as observe,
                    patch("busydiscord.main.output"),
                    self.assertRaises(SystemExit) as status,
                ):
                    main([*arguments, "--console-only", "--seconds", "1"])
                self.assertEqual(status.exception.code, 0)
                self.assertEqual(build.call_args.args[0], expected)
                prepare.assert_not_called()
                observe.assert_awaited_once_with(source, 1)

    def test_bot_token_is_separate_and_accepts_authorization_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discord-bot-token.json"
            with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "Bot bot-secret"}, clear=True):
                self.assertEqual(prepare_bot_token(path), "bot-secret")
            self.assertEqual(json.loads(path.read_text()), {"token": "bot-secret"})
            self.assertFalse((path.parent / "discord-token.json").exists())

    def test_rpc_path_override(self):
        with patch.dict(os.environ, {"DISCORD_RPC_PATH": "custom-socket"}):
            self.assertEqual(ipc_paths(), ["custom-socket"])
