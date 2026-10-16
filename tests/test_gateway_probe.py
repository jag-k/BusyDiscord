import asyncio
import json
import os
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from busydiscord.main import VoiceClient, VoiceTracker, observe, prepare_token
from busydiscord.token_store import TokenStore


class VoiceTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = VoiceTracker()
        self.tracker.dispatch("READY", {"user": {"id": "me"}}, 0)

    def state(self, **changes):
        return {
            "user_id": "me",
            "guild_id": "guild",
            "channel_id": "voice",
            "self_mute": False,
            "self_deaf": False,
            "mute": False,
            "deaf": False,
            "suppress": False,
            **changes,
        }

    def test_initial_absence_is_unknown_and_other_users_are_ignored(self):
        self.assertFalse(self.tracker.dispatch("VOICE_STATE_UPDATE", {"user_id": "other", "channel_id": "voice"}, 1))
        self.assertEqual(self.tracker.snapshot(2)["status"], "unknown")

    def test_mute_move_and_leave_preserve_or_reset_timer(self):
        state = self.state()
        self.tracker.update(state, 10)
        self.tracker.update({**state, "self_mute": True, "self_deaf": True}, 20)
        self.assertTrue(self.tracker.snapshot(25)["self_deaf"])
        self.assertEqual(self.tracker.snapshot(25)["observed_seconds"], 15)
        self.tracker.update({**state, "channel_id": "next"}, 30)
        self.assertEqual(self.tracker.snapshot(40)["observed_seconds"], 30)
        self.tracker.update({**state, "channel_id": None}, 50)
        self.assertEqual(self.tracker.snapshot(60)["status"], "outside_voice")
        self.tracker.update(state, 70)
        self.assertEqual(self.tracker.snapshot(75)["observed_seconds"], 5)

    def test_unrelated_guild_leave_does_not_erase_active_voice(self):
        self.tracker.update(self.state(), 10)
        self.assertFalse(self.tracker.update({"user_id": "me", "guild_id": "other", "channel_id": None}, 20))
        self.assertEqual(self.tracker.snapshot(30)["status"], "in_voice")

    def test_ready_snapshot_and_channel_name(self):
        self.tracker.dispatch(
            "READY",
            {
                "user": {"id": "me"},
                "guilds": [
                    {
                        "id": "guild",
                        "channels": [{"id": "voice", "name": "General"}],
                        "voice_states": [{"user_id": "me", "channel_id": "voice", "self_mute": True}],
                    }
                ],
            },
            10,
        )
        snapshot = self.tracker.snapshot(15)
        self.assertEqual(snapshot["channel_name"], "General")
        self.assertEqual(snapshot["guild_id"], "guild")
        self.assertTrue(snapshot["self_mute"])

    def test_server_timer_restores_existing_session_and_tracks_channel_moves(self):
        self.tracker.update(self.state(), 10)
        self.tracker.update_channel_info(
            "CHANNEL_INFO",
            {
                "guild_id": "guild",
                "channels": [
                    {"id": "voice", "voice_start_time": 1000},
                    {"id": "next", "voice_start_time": 1100},
                ],
            },
        )
        state = self.tracker.snapshot(15, wall_now=1200)
        self.assertEqual(state["elapsed_seconds"], 200)
        self.assertEqual(state["observed_seconds"], 5)
        self.assertEqual(state["timer_source"], "discord")
        self.tracker.update(self.state(channel_id="next", self_mute=True), 20)
        self.assertEqual(self.tracker.snapshot(25, wall_now=1205)["elapsed_seconds"], 105)
        self.tracker.update(self.state(channel_id=None), 30)
        self.assertEqual(self.tracker.snapshot(35)["status"], "outside_voice")

    def test_server_timer_updates_null_and_missing_fields(self):
        self.tracker.update(self.state(), 10)
        data = {"guild_id": "guild", "id": "voice", "voice_start_time": 1000}
        self.tracker.update_channel_info("VOICE_CHANNEL_START_TIME_UPDATE", data)
        self.tracker.update_channel_info("CHANNEL_INFO", {"guild_id": "guild", "channels": [{"id": "voice"}]})
        self.assertEqual(self.tracker.snapshot(15, wall_now=1200)["elapsed_seconds"], 200)
        self.tracker.update_channel_info("VOICE_CHANNEL_START_TIME_UPDATE", {**data, "voice_start_time": 1300})
        self.assertEqual(self.tracker.snapshot(15, wall_now=1200)["elapsed_seconds"], 0)
        self.tracker.update_channel_info("VOICE_CHANNEL_START_TIME_UPDATE", {**data, "voice_start_time": None})
        state = self.tracker.snapshot(15, wall_now=1200)
        self.assertEqual(state["elapsed_seconds"], 5)
        self.assertEqual(state["timer_source"], "observed")


class LibraryClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_run_closes_client_and_keeps_rotated_token(self):
        clients = []

        async def start(client, token, *, reconnect):
            clients.append(client)
            self.assertTrue(reconnect)
            self.assertEqual(token, "initial")
            client.authenticated = client.connected = True
            client.http._token("rotated-during-session")
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.json"
            store = TokenStore(path, "initial")
            with patch.object(VoiceClient, "start", start), patch("busydiscord.main.output"):
                self.assertEqual(await observe("initial", 0.01, store), 0)
            self.assertTrue(clients[0].is_closed())
            self.assertEqual(TokenStore(path, "initial").load(), "rotated-during-session")

    async def test_own_voice_events_and_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "token.json", "initial")
            async with VoiceClient(store) as client:
                own = SimpleNamespace(id=1)
                client._connection.user = own
                channel = SimpleNamespace(id=3, name="General", guild=SimpleNamespace(id=2))
                before = SimpleNamespace(channel=None)
                after = SimpleNamespace(
                    channel=channel, self_mute=True, self_deaf=False, mute=False, deaf=False, suppress=False
                )
                with patch("busydiscord.main.output") as output:
                    await client.on_voice_state_update(SimpleNamespace(id=99), before, after)
                    output.assert_not_called()
                    await client.on_voice_state_update(own, before, after)
                    self.assertTrue(output.call_args.args[0]["self_mute"])
                    self.assertEqual(output.call_args.args[0]["channel_name"], "General")
                    await client.on_disconnect()
                    self.assertEqual(client.tracker.snapshot(1)["status"], "unknown")

    async def test_gateway_timer_request_response_and_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            async with VoiceClient(TokenStore(Path(directory) / "token.json")) as client:
                client.tracker.user_id = "me"
                client.tracker.update({"user_id": "me", "guild_id": "guild", "channel_id": "voice"}, 10)
                client.ws = SimpleNamespace(send_as_json=AsyncMock())
                await client.request_voice_start()
                await client.request_voice_start()
                client.ws.send_as_json.assert_awaited_once_with(
                    {
                        "op": 43,
                        "d": {"guild_id": "guild", "fields": ["voice_start_time"]},
                    }
                )
                with patch("busydiscord.main.output") as output:
                    await client.on_socket_raw_receive(
                        json.dumps(
                            {
                                "t": "CHANNEL_INFO",
                                "d": {
                                    "guild_id": "guild",
                                    "channels": [{"id": "voice", "voice_start_time": 1000}],
                                },
                            }
                        )
                    )
                    self.assertEqual(output.call_args.args[0]["timer_source"], "discord")
                    output.reset_mock()
                    await client.on_socket_raw_receive('{"t":"READY","d":{"auth_token":"private"}}')
                    output.assert_not_called()
                    await client.on_disconnect()
                self.assertFalse(client.tracker.voice_starts)
                self.assertIsNone(client.requested_voice_channel)
                client.ws = None

    async def test_library_rotation_is_saved_and_not_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.json"
            async with VoiceClient(TokenStore(path, "initial")) as client:
                # Exercise the actual library method used for READY.auth_token.
                client.http._token("replacement-test-token")
                with patch("busydiscord.main.output") as output:
                    await client.on_connect()
                    await client.on_connect()
                    output.assert_called_once_with({"token": "rotated_and_saved"})
                    self.assertNotIn("replacement-test-token", str(output.call_args_list))
                self.assertEqual(TokenStore(path, "initial").load(), "replacement-test-token")


class TokenStoreTests(unittest.TestCase):
    def test_rotated_token_survives_restart_without_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "token.json"
            store = TokenStore(path, "initial")
            self.assertEqual(store.load(), "initial")
            self.assertFalse(store.save("initial"))
            self.assertTrue(store.save("replacement"))
            self.assertEqual(TokenStore(path, "initial").load(), "replacement")
            self.assertEqual(TokenStore(path).load(), "replacement")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_migration_removes_only_discord_from_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("BUSYBAR_ADDR=bar.local\nBUSYBAR_TOKEN=busy-secret\nDISCORD_USER_TOKEN=discord-secret\n")
            path = root / ".state" / "discord-token.json"
            _store, token = prepare_token(path, env)
            self.assertEqual(token, "discord-secret")
            self.assertEqual(env.read_text(), "BUSYBAR_ADDR=bar.local\nBUSYBAR_TOKEN=busy-secret\n")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(prepare_token(path, env)[1], "discord-secret")

    def test_cached_rotation_wins_over_legacy_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("DISCORD_USER_TOKEN=old-token\nBUSYBAR_TOKEN=unchanged\n")
            path = root / "token.json"
            TokenStore(path).save("latest-token")
            self.assertEqual(prepare_token(path, env)[1], "latest-token")
            self.assertEqual(env.read_text(), "BUSYBAR_TOKEN=unchanged\n")

    def test_failed_atomic_replace_keeps_previous_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.json"
            store = TokenStore(path, "initial")
            store.save("first")
            with patch("busydiscord.token_store.os.replace", side_effect=OSError), self.assertRaises(OSError):
                store.save("second")
            self.assertEqual(TokenStore(path, "initial").load(), "first")
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
