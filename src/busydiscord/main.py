import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import time

from pathlib import Path

import discord

from dotenv import dotenv_values, load_dotenv, unset_key

from .bar_display import BarDisplay, make_bar
from .token_store import TokenStore


class VoiceTracker:
    def __init__(self):
        self.user_id = None
        self.state = None
        self.started = None
        self.channels = {}
        self.voice_starts = {}

    def update(self, state, now):
        if not self.user_id or state.get("user_id") != self.user_id:
            return False
        current = {
            key: state.get(key)
            for key in ("channel_id", "guild_id", "self_mute", "self_deaf", "mute", "deaf", "suppress")
        }
        # Do not treat an unrelated/stale guild leave as leaving the active call.
        if (
            self.state
            and self.state.get("channel_id")
            and not current["channel_id"]
            and current["guild_id"] != self.state.get("guild_id")
        ):
            return False
        if current == self.state:
            return False
        if current["channel_id"]:
            if not self.state or not self.state.get("channel_id"):
                self.started = now
        else:
            self.started = None
        self.state = current
        return True

    def update_channel_info(self, event, data):
        if event == "CHANNEL_INFO":
            channels = data.get("channels", [])
        elif event == "VOICE_CHANNEL_START_TIME_UPDATE":
            channels = [data]
        else:
            return False
        changed = False
        for channel in channels:
            if "voice_start_time" not in channel:
                continue
            key = (str(data.get("guild_id")), str(channel["id"]))
            value = channel["voice_start_time"]
            if value is None:
                self.voice_starts.pop(key, None)
            elif type(value) is int and value > 0:
                self.voice_starts[key] = value
            else:
                continue
            if self.state and key == (self.state.get("guild_id"), self.state.get("channel_id")):
                changed = True
        return changed

    def dispatch(self, event, data, now):
        changed = False
        if event == "READY":
            self.user_id = data["user"]["id"]
            self.state = None
            self.started = None
            self.channels.clear()
            self.voice_starts.clear()
        if event == "VOICE_STATE_UPDATE":
            return self.update(data, now)
        if event in ("READY", "GUILD_CREATE", "CALL_CREATE", "CALL_UPDATE"):
            containers = [data]
            if event == "READY":
                containers += data.get("guilds", []) + data.get("calls", [])
            for container in containers:
                for channel in container.get("channels", []) + container.get("private_channels", []):
                    if channel.get("name"):
                        self.channels[channel["id"]] = channel["name"]
                for voice_state in container.get("voice_states", []):
                    state = dict(voice_state)
                    if event == "GUILD_CREATE" or (event == "READY" and container in data.get("guilds", [])):
                        state.setdefault("guild_id", container.get("id"))
                    changed |= self.update(state, now)
        if event in ("CHANNEL_CREATE", "CHANNEL_UPDATE") and data.get("name"):
            self.channels[data["id"]] = data["name"]
        return changed

    def snapshot(self, now, wall_now=None):
        if self.state is None:
            return {"status": "unknown", "reason": "waiting_for_own_voice_state"}
        if not self.state["channel_id"]:
            return {"status": "outside_voice"}
        observed = max(0, int(now - self.started))
        voice_start = self.voice_starts.get((self.state.get("guild_id"), self.state["channel_id"]))
        elapsed = (
            max(0, int((time.time() if wall_now is None else wall_now) - voice_start)) if voice_start else observed
        )
        return {
            "status": "in_voice",
            **self.state,
            "channel_name": self.channels.get(self.state["channel_id"]),
            "observed_seconds": observed,
            "voice_start_time": voice_start,
            "elapsed_seconds": elapsed,
            "timer_source": "discord" if voice_start else "observed",
        }


def output(data):
    # No raw Gateway frames: they can contain messages, tokens, and other users' data.
    print(json.dumps(data, ensure_ascii=False), flush=True)


class VoiceClient(discord.Client):
    def __init__(self, store):
        super().__init__(
            max_messages=None,
            chunk_guilds_at_startup=False,
            guild_subscriptions=False,
            sync_presence=False,
            enable_debug_events=True,
        )
        self.store = store
        self.tracker = VoiceTracker()
        self.authenticated = False
        self.connected = False
        self.failed = False
        self.requested_voice_channel = None

    def save_token(self):
        # discord.py-self 2.1 updates HTTPClient.token on READY.auth_token.
        # There is no public token-rotation callback; keep this adapter isolated.
        token = self.http.token
        if token and self.store.save(token):
            output({"token": "rotated_and_saved"})

    async def on_connect(self):
        self.save_token()

    async def request_voice_start(self):
        state = self.tracker.state or {}
        guild_id = state.get("guild_id")
        channel_id = state.get("channel_id")
        if not channel_id or not guild_id:
            self.requested_voice_channel = None
            return
        key = (guild_id, channel_id)
        if key == self.requested_voice_channel or not self.ws:
            return
        try:
            await self.ws.send_as_json({"op": 43, "d": {"guild_id": guild_id, "fields": ["voice_start_time"]}})
        except Exception as exc:
            output({"voice_timer": "request_failed", "error_type": type(exc).__name__})
        else:
            self.requested_voice_channel = key

    async def on_socket_raw_receive(self, message):
        # discord.py-self 2.1 does not parse these events. Never log raw frames.
        payload = json.loads(message)
        event = payload.get("t")
        if event in ("CHANNEL_INFO", "VOICE_CHANNEL_START_TIME_UPDATE") and self.tracker.update_channel_info(
            event, payload["d"]
        ):
            output(self.tracker.snapshot(time.monotonic()))

    def record_voice(self, user, state, previous=None):
        if not self.user or not user or user.id != self.user.id:
            return
        channel = state.channel
        old_channel = previous.channel if previous else None
        guild = getattr(channel or old_channel, "guild", None)
        if guild is None:
            guild = getattr(user, "guild", None)
        channel_id = str(channel.id) if channel else None
        if channel and getattr(channel, "name", None):
            self.tracker.channels[channel_id] = channel.name
        data = {
            "user_id": str(user.id),
            "channel_id": channel_id,
            "guild_id": str(guild.id) if guild else None,
            **{key: getattr(state, key, None) for key in ("self_mute", "self_deaf", "mute", "deaf", "suppress")},
        }
        if self.tracker.update(data, time.monotonic()):
            output(self.tracker.snapshot(time.monotonic()))

    def seed_voice(self):
        if not self.user:
            return
        self.tracker.user_id = str(self.user.id)
        states = [(self.user, self.user.voice)]
        states.extend((guild.me, guild.me.voice) for guild in self.guilds if guild.me)
        for user, state in states:
            if state and state.channel:
                self.record_voice(user, state)
                return
        # READY_SUPPLEMENTAL has populated voice/call caches before on_ready.
        if not any(guild.unavailable for guild in self.guilds):
            self.tracker.state = {"channel_id": None}
            self.tracker.started = None

    async def on_ready(self):
        self.authenticated = self.connected = True
        self.save_token()
        self.seed_voice()
        await self.request_voice_start()
        output({"connection": "authenticated", "library": "discord.py-self"})
        output(self.tracker.snapshot(time.monotonic()))

    async def on_resumed(self):
        self.connected = True
        self.seed_voice()
        await self.request_voice_start()
        output({"connection": "resumed"})

    async def on_disconnect(self):
        self.connected = False
        self.tracker.state = None
        self.tracker.started = None
        self.tracker.voice_starts.clear()
        self.requested_voice_channel = None
        output({"connection": "disconnected", "voice_status": "unknown"})

    async def on_voice_state_update(self, user, before, after):
        # Own voice events can arrive before on_ready.
        if self.user:
            self.tracker.user_id = str(self.user.id)
        self.record_voice(user, after, before)
        await self.request_voice_start()

    async def on_error(self, event, *args, **kwargs):
        # The default handler prints a traceback, potentially including private data.
        self.failed = True
        output({"connection": "handler_failed", "voice_status": "unknown"})
        await self.close()


async def observe(token, seconds=None, store=None, bar=None):
    if store is None:
        raise ValueError("A token store is required")
    async with VoiceClient(store) as client:
        task = asyncio.create_task(client.start(token, reconnect=True))
        display = BarDisplay(bar, output) if bar else None
        display_task = (
            asyncio.create_task(
                display.run(
                    lambda: client.tracker.snapshot(time.monotonic()) if client.connected else {"status": "unknown"},
                ),
            )
            if display
            else None
        )
        deadline = time.monotonic() + seconds if seconds is not None else float("inf")
        try:
            while time.monotonic() < deadline:
                done, _ = await asyncio.wait({task}, timeout=min(5, max(0, deadline - time.monotonic())))
                # Also persist a rotation if it happened before an event callback ran.
                client.save_token()
                if done:
                    await task
                    return 1
                if client.connected:
                    output(client.tracker.snapshot(time.monotonic()))
            if not client.authenticated:
                output({"connection": "authentication_not_confirmed", "voice_status": "unknown"})
            return 0 if client.authenticated and not client.failed else 1
        finally:
            try:
                client.save_token()
            finally:
                await client.close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if display_task:
                    display_task.cancel()
                    await asyncio.gather(display_task, return_exceptions=True)
                    if display:
                        await display.close()


def prepare_token(state_path, env_path, *, prompt=False):
    store = TokenStore(state_path)
    token = store.load()
    legacy = dotenv_values(env_path).get("DISCORD_USER_TOKEN") if env_path.exists() else None
    if not token and not prompt:
        token = legacy or os.environ.get("DISCORD_USER_TOKEN", "")
        if token:
            store.save(token.strip())
            token = store.current
    if prompt or not token:
        if not sys.stdin.isatty():
            raise ValueError("Run busydiscord --login in an interactive terminal first")
        token = getpass.getpass("Discord account token (hidden): ").strip()
        if not token:
            raise ValueError("Discord token cannot be empty")
        store.save(token)
    # Delete only the migrated Discord credential; BusyBar configuration stays in .env.
    if legacy:
        unset_key(env_path, "DISCORD_USER_TOKEN")
    return store, token


async def demo(bar, *, single_frame=False):
    display = BarDisplay(bar, output)
    state = {
        "status": "in_voice",
        "channel_name": "Discord Test",
        "observed_seconds": 123,
        "self_mute": False,
        "self_deaf": False,
    }
    try:
        for label, change in [
            ("MIC", {}),
            ("MUTE", {"self_mute": True}),
            ("DEAF", {"self_mute": True, "self_deaf": True}),
        ]:
            await display.update({**state, **change})
            output({"busybar_demo": label})
            if single_frame:
                await asyncio.sleep(0.25)
                break
            await asyncio.sleep(3)
    finally:
        await display.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="BUSY Bar IP[:port] or URL; defaults to BUSYBAR_ADDR or 10.0.4.20 (USB)")
    parser.add_argument("--seconds", type=int, help="Stop after this many seconds; default runs until Ctrl+C")
    parser.add_argument("--console-only", action="store_true", help="Monitor Discord without drawing on BUSY Bar")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Save/replace the Discord token with hidden terminal input",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Test MIC/MUTE/DEAF on BUSY Bar without connecting to Discord",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Draw one sample frame and exit without connecting to Discord",
    )
    args = parser.parse_args(argv)
    if args.seconds is not None and args.seconds < 1:
        parser.error("--seconds must be positive")
    if (args.demo or args.test) and (args.console_only or args.login):
        parser.error("--demo/--test cannot be combined with --console-only or --login")
    if args.demo and args.test:
        parser.error("--demo and --test cannot be combined")
    root = Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    # Only BusyBar settings use .env during normal operation.
    load_dotenv(env_path)
    # Library debug logs may contain raw frames. This CLI emits only selected state.
    logging.getLogger("discord").addHandler(logging.NullHandler())
    logging.getLogger("discord").propagate = False
    logging.getLogger("discord").setLevel(logging.CRITICAL)
    try:
        if not (args.demo or args.test):
            store, token = prepare_token(root / ".state" / "discord-token.json", env_path, prompt=args.login)
            if args.login:
                output({"discord_token": "saved"})
                return
        addr = args.host or os.environ.get("BUSYBAR_ADDR") or "10.0.4.20"
        bar_token = os.environ.get("BUSYBAR_TOKEN")

        async def run():
            if args.console_only:
                return await observe(token, args.seconds, store)
            async with make_bar(addr, bar_token) as bar:
                if args.demo or args.test:
                    await demo(bar, single_frame=args.test)
                    return 0
                return await observe(token, args.seconds, store, bar)

        return_code = asyncio.run(run())
    except KeyboardInterrupt:
        return_code = 0
    except ValueError as exc:
        output({"connection": "configuration_error", "error_type": type(exc).__name__})
        return_code = 1
    except discord.LoginFailure:
        output({"connection": "authentication_failed", "voice_status": "unknown"})
        return_code = 1
    except Exception as exc:
        output({"connection": "failed", "error_type": type(exc).__name__, "voice_status": "unknown"})
        return_code = 1
    finally:
        output({"connection": "stopped", "voice_status": "unknown"})
    raise SystemExit(return_code)


if __name__ == "__main__":
    main()
