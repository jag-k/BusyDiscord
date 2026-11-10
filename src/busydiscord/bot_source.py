import asyncio
import json
import random
import sys
import time

from enum import IntEnum
from http import HTTPStatus

import aiohttp

from .sources import SourceError, VoiceSource


BOT_INTENTS = (1 << 0) | (1 << 7)  # GUILDS and GUILD_VOICE_STATES; no privileged intents.
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4012, 4013, 4014}


class GatewayOp(IntEnum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    RESUME = 6
    RECONNECT = 7
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11


class BotSource(VoiceSource):
    def __init__(self, token, user_id, report):
        super().__init__(report)
        self.token = token
        self.tracker.user_id = user_id
        self.sequence = None
        self.session_id = None
        self.resume_url = None
        self.pending_guilds = set()
        self.unavailable_guilds = set()
        self.heartbeat_acked = True

    def dispatch(self, event, data):
        now = time.monotonic()
        if event == "READY":
            self.session_id = data["session_id"]
            self.resume_url = data["resume_gateway_url"]
            self.tracker.state = None
            self.tracker.started = None
            self.tracker.channels.clear()
            self.tracker.voice_starts.clear()
            self.pending_guilds = {guild["id"] for guild in data.get("guilds", [])}
            self.unavailable_guilds.clear()
            self.authenticated = self.connected = True
            self.report({"connection": "authenticated", "mode": "bot"})
        elif event == "RESUMED":
            self.connected = True
        elif event == "GUILD_CREATE":
            guild_id = data["id"]
            self.pending_guilds.discard(guild_id)
            if data.get("unavailable"):
                self.unavailable_guilds.add(guild_id)
                return
            self.unavailable_guilds.discard(guild_id)
            # A fresh guild snapshot is authoritative, including absence of our user.
            old = self.tracker.state or {}
            if old.get("guild_id") == guild_id:
                own = next((s for s in data.get("voice_states", []) if s["user_id"] == self.tracker.user_id), None)
                if not own:
                    self.tracker.update(
                        {"user_id": self.tracker.user_id, "guild_id": guild_id, "channel_id": None}, now
                    )
            self.tracker.dispatch(event, data, now)
        elif event == "GUILD_DELETE":
            guild_id = data["id"]
            self.pending_guilds.discard(guild_id)
            if data.get("unavailable"):
                self.unavailable_guilds.add(guild_id)
            else:
                self.unavailable_guilds.discard(guild_id)
            if (self.tracker.state or {}).get("guild_id") == guild_id:
                self.tracker.state = None
                self.tracker.started = None
        else:
            self.tracker.dispatch(event, data, now)
            self.tracker.update_channel_info(event, data)
        if not self.pending_guilds and not self.unavailable_guilds and self.tracker.state is None:
            self.tracker.state = {"channel_id": None}
        self.report(self.snapshot())

    async def heartbeat(self, ws, interval):
        await asyncio.sleep(random.random() * interval)  # noqa: S311 -- Protocol heartbeat jitter, not cryptography.
        while True:
            if not self.heartbeat_acked:
                await ws.close()
                return
            self.heartbeat_acked = False
            try:
                await ws.send_json({"op": GatewayOp.HEARTBEAT, "d": self.sequence})
            except aiohttp.ClientError, OSError:
                await ws.close()
                return
            await asyncio.sleep(interval)

    async def handle_payload(self, ws, payload):
        if payload.get("s") is not None:
            self.sequence = payload["s"]
        opcode = payload["op"]
        if opcode == GatewayOp.DISPATCH:
            self.dispatch(payload["t"], payload["d"])
        elif opcode == GatewayOp.HEARTBEAT_ACK:
            self.heartbeat_acked = True
        elif opcode == GatewayOp.HEARTBEAT:
            await ws.send_json({"op": GatewayOp.HEARTBEAT, "d": self.sequence})
        elif opcode == GatewayOp.RECONNECT:
            return False
        elif opcode == GatewayOp.INVALID_SESSION:
            if not payload["d"]:
                self.session_id = self.sequence = self.resume_url = None
            return False
        return True

    async def session(self, http, url):
        async with http.ws_connect(url, params={"v": "10", "encoding": "json"}, max_msg_size=16 * 1024 * 1024) as ws:
            hello = await asyncio.wait_for(ws.receive_json(), 15)
            if hello.get("op") != GatewayOp.HELLO:
                raise SourceError("Discord Gateway did not send Hello")
            self.heartbeat_acked = True
            if self.session_id:
                await ws.send_json(
                    {"op": 6, "d": {"token": self.token, "session_id": self.session_id, "seq": self.sequence}}
                )
            else:
                await ws.send_json(
                    {
                        "op": 2,
                        "d": {
                            "token": self.token,
                            "intents": BOT_INTENTS,
                            "properties": {"os": sys.platform, "browser": "BusyDiscord", "device": "BusyDiscord"},
                        },
                    }
                )
            heartbeat = asyncio.create_task(self.heartbeat(ws, hello["d"]["heartbeat_interval"] / 1000))
            try:
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        break
                    if not await self.handle_payload(ws, json.loads(message.data)):
                        break
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if ws.close_code in FATAL_CLOSE_CODES:
                raise SourceError(
                    f"Discord rejected bot connection (close code {ws.close_code}); check Bot Token and intents"
                )
            if ws.close_code in {4007, 4009}:
                self.session_id = self.sequence = self.resume_url = None

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.get(
                "https://discord.com/api/v10/gateway/bot", headers={"Authorization": "Bot " + self.token}
            ) as response:
                if response.status != HTTPStatus.OK:
                    raise SourceError(f"Bot Gateway discovery failed (HTTP {response.status}); check Bot Token")
                gateway = await response.json()
            limit = gateway.get("session_start_limit", {})
            if limit.get("remaining") == 0:
                raise SourceError("Discord bot session start limit exhausted; wait before restarting")
            delay = 5
            while True:
                try:
                    await self.session(http, self.resume_url or gateway["url"])
                    delay = 5
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    self.report({"connection": "disconnected", "mode": "bot", "error_type": type(exc).__name__})
                    delay = min(delay * 2, 60)
                finally:
                    self.connected = False
                self.report({"connection": "reconnecting", "mode": "bot", "voice_status": "unknown"})
                await asyncio.sleep(delay)
