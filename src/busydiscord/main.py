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
from .bot_source import BotSource
from .rpc_source import RpcSource
from .sources import SourceError, observe_source
from .token_store import TokenStore
from .voice_state import VoiceTracker


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


def prepare_bot_token(state_path, *, prompt=False):
    store = TokenStore(state_path)
    token = store.load() or os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if prompt or not token:
        if not sys.stdin.isatty():
            raise SourceError("Set DISCORD_BOT_TOKEN or run busydiscord --mode bot --login first")
        token = getpass.getpass("Discord Bot Token (hidden): ").strip()
    token = token.removeprefix("Bot ").strip()
    if not token:
        raise SourceError("Bot Token cannot be empty")
    store.save(token)
    return token


def source_for_mode(mode, args, root):
    if mode == "bot":
        user_id = args.user_id or os.environ.get("DISCORD_USER_ID", "")
        if not user_id.isascii() or not user_id.isdecimal():
            raise SourceError("Set DISCORD_USER_ID or --user-id to the Discord user ID to monitor")
        token = prepare_bot_token(root / ".state" / "discord-bot-token.json", prompt=args.login)
        return BotSource(token, user_id, output)
    client_id = args.client_id or os.environ.get("DISCORD_CLIENT_ID", "")
    client_secret = os.environ.get("DISCORD_CLIENT_SECRET", "")
    if not client_id.isascii() or not client_id.isdecimal() or not client_secret:
        raise SourceError("RPC requires DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET from your developer application")
    return RpcSource(client_id, client_secret, root / ".state" / "discord-rpc.json", output)


async def demo(bar, *, single_frame=False):
    display = BarDisplay(bar, output)
    state = {
        "status": "in_voice",
        "channel_name": "Discord Test",
        "observed_seconds": 123,
        "timer_started_at": int(time.time()) - 123,
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("rpc", "bot", "user-token"), help="Discord source; defaults to DISCORD_MODE or user-token"
    )
    parser.add_argument("--user-id", help="Discord user ID to monitor in bot mode; overrides DISCORD_USER_ID")
    parser.add_argument("--client-id", help="Developer application Client ID for RPC; overrides DISCORD_CLIENT_ID")
    parser.add_argument("--host", help="BUSY Bar IP[:port] or URL; defaults to BUSYBAR_ADDR or 10.0.4.20 (USB)")
    parser.add_argument("--seconds", type=int, help="Stop after this many seconds; default runs until Ctrl+C")
    parser.add_argument("--console-only", action="store_true", help="Monitor Discord without drawing on BUSY Bar")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Save/replace the selected mode's credentials; RPC opens authorization in Discord",
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
    return args


def login_source(source, mode):
    if isinstance(source, RpcSource):
        asyncio.run(source.login())
    else:
        output({"discord_token": "saved", "mode": mode})


def main(argv=None):
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    # Source selection and BusyBar settings can be supplied through .env.
    load_dotenv(env_path)
    mode = args.mode or os.environ.get("DISCORD_MODE") or "user-token"
    if mode not in ("rpc", "bot", "user-token"):
        raise SystemExit("DISCORD_MODE must be rpc, bot, or user-token")
    # Library debug logs may contain raw frames. This CLI emits only selected state.
    logging.getLogger("discord").addHandler(logging.NullHandler())
    logging.getLogger("discord").propagate = False
    logging.getLogger("discord").setLevel(logging.CRITICAL)
    try:
        source = None
        if not (args.demo or args.test):
            if mode == "user-token":
                store, token = prepare_token(root / ".state" / "discord-token.json", env_path, prompt=args.login)
            else:
                source = source_for_mode(mode, args, root)
            if args.login:
                login_source(source, mode)
                return
        addr = args.host or os.environ.get("BUSYBAR_ADDR") or "10.0.4.20"
        bar_token = os.environ.get("BUSYBAR_TOKEN")

        async def run():
            if args.console_only:
                if source:
                    return await observe_source(source, args.seconds)
                return await observe(token, args.seconds, store)
            async with make_bar(addr, bar_token) as bar:
                if args.demo or args.test:
                    await demo(bar, single_frame=args.test)
                    return 0
                if source:
                    return await observe_source(source, args.seconds, bar)
                return await observe(token, args.seconds, store, bar)

        return_code = asyncio.run(run())
    except KeyboardInterrupt:
        return_code = 0
    except SourceError as exc:
        output({"connection": "configuration_error", "mode": mode, "message": str(exc)})
        return_code = 1
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
