import asyncio
import json
import os
import struct
import time
import uuid

from contextlib import suppress
from enum import IntEnum
from http import HTTPStatus
from pathlib import Path

import aiohttp

from .sources import SourceError, VoiceSource
from .token_store import save_private_json


RPC_SCOPES = ["identify", "rpc", "rpc.voice.read"]
MAX_FRAME_BYTES = 16 * 1024 * 1024


class IpcOp(IntEnum):
    HANDSHAKE = 0
    FRAME = 1
    CLOSE = 2
    PING = 3
    PONG = 4


def ipc_paths():
    override = os.environ.get("DISCORD_RPC_PATH")
    if override:
        return [override]
    if os.name == "nt":
        return [rf"\\?\pipe\discord-ipc-{index}" for index in range(10)]
    directories = [os.environ.get(name) for name in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")]
    directories.append("/tmp")  # noqa: S108 -- Discord's documented Unix IPC fallback.
    return [
        str(Path(directory) / f"discord-ipc-{index}")
        for directory in dict.fromkeys(directories)
        if directory
        for index in range(10)
    ]


class RpcConnection:
    def __init__(self, client_id):
        self.client_id = client_id
        self.reader = None
        self.writer = None

    async def connect(self):
        for path in ipc_paths():
            try:
                async with asyncio.timeout(3):
                    if os.name == "nt":
                        loop = asyncio.get_running_loop()
                        reader = asyncio.StreamReader()
                        protocol = asyncio.StreamReaderProtocol(reader)
                        transport, _ = await loop.create_pipe_connection(lambda protocol=protocol: protocol, path)
                        self.reader, self.writer = reader, asyncio.StreamWriter(transport, protocol, reader, loop)
                    else:
                        self.reader, self.writer = await asyncio.open_unix_connection(path)
                    await self.send(0, {"v": 1, "client_id": self.client_id})
                    opcode, payload = await self.receive()
                    if opcode != 1 or payload.get("evt") != "READY":
                        raise SourceError("Discord rejected the RPC handshake; check Client ID")
                    return
            except OSError, TimeoutError, asyncio.IncompleteReadError:
                await self.close()
        raise ConnectionError("Discord desktop IPC socket not found")

    async def send(self, opcode, payload):
        if self.writer is None:
            raise ConnectionError("RPC is not connected")
        data = json.dumps(payload).encode()
        self.writer.write(struct.pack("<II", opcode, len(data)) + data)
        await self.writer.drain()

    async def receive(self):
        if self.reader is None:
            raise ConnectionError("RPC is not connected")
        opcode, size = struct.unpack("<II", await self.reader.readexactly(8))
        if size > MAX_FRAME_BYTES:
            raise SourceError("Discord RPC frame is too large")
        return opcode, json.loads(await self.reader.readexactly(size))

    async def command(self, command, args=None, *, deadline=15):
        nonce = uuid.uuid4().hex
        async with asyncio.timeout(deadline):
            await self.send(1, {"cmd": command, "args": args or {}, "nonce": nonce})
            while True:
                opcode, payload = await self.receive()
                if opcode == IpcOp.PING:
                    await self.send(IpcOp.PONG, payload)
                    continue
                if opcode == IpcOp.CLOSE:
                    raise ConnectionError("Discord closed RPC")
                if payload.get("nonce") != nonce:
                    continue
                if payload.get("evt") == "ERROR":
                    code = payload.get("data", {}).get("code")
                    raise SourceError(
                        f"Discord RPC {command} rejected (code {code}); check app scopes and tester access. "
                        "Run --mode rpc --login again"
                    )
                return payload.get("data")

    async def close(self):
        if self.writer:
            self.writer.close()
            with suppress(OSError):
                await self.writer.wait_closed()
        self.reader = self.writer = None


class RpcSource(VoiceSource):
    def __init__(self, client_id, client_secret, state_path, report):
        super().__init__(report)
        self.client_id = client_id
        self.client_secret = client_secret
        self.state_path = state_path

    async def exchange(self, http, grant):
        async with http.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                **grant,
            },
        ) as response:
            if response.status != HTTPStatus.OK:
                raise SourceError(
                    f"RPC OAuth token exchange failed (HTTP {response.status}); "
                    "check app credentials or run --mode rpc --login"
                )
            data = await response.json()
        if not data.get("access_token") or not data.get("refresh_token"):
            raise SourceError("Discord did not return RPC access and refresh tokens")
        data["client_id"] = self.client_id
        data["expires_at"] = time.time() + data["expires_in"]
        save_private_json(self.state_path, data)
        return data["access_token"]

    async def token(self, http):
        try:
            data = json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError) as exc:
            raise SourceError("Run busydiscord --mode rpc --login on the PC running Discord first") from exc
        if data.get("client_id") != self.client_id:
            raise SourceError("RPC Client ID changed; run --mode rpc --login again")
        if data.get("expires_at", 0) <= time.time() + 60:
            return await self.exchange(http, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
        return data["access_token"]

    async def login(self):
        rpc = RpcConnection(self.client_id)
        try:
            await rpc.connect()
            self.report({"rpc": "authorize_in_discord", "scopes": RPC_SCOPES})
            grant = await rpc.command("AUTHORIZE", {"client_id": self.client_id, "scopes": RPC_SCOPES}, deadline=180)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
                token = await self.exchange(http, {"grant_type": "authorization_code", "code": grant["code"]})
            await rpc.command("AUTHENTICATE", {"access_token": token})
            self.report({"rpc": "authorization_saved"})
            return 0
        finally:
            await rpc.close()

    def record_channel(self, channel):
        if channel is None:
            self.tracker.update(
                {
                    "user_id": self.tracker.user_id,
                    "channel_id": None,
                    "guild_id": (self.tracker.state or {}).get("guild_id"),
                },
                time.monotonic(),
            )
            return
        channel_id = str(channel["id"])
        self.tracker.channels[channel_id] = channel.get("name") or "Voice"
        own = next(
            (
                state
                for state in channel.get("voice_states", [])
                if str(state.get("user", {}).get("id")) == self.tracker.user_id
            ),
            None,
        )
        if own is None:
            self.tracker.state = None
            return
        self.tracker.update(
            {
                **own["voice_state"],
                "user_id": self.tracker.user_id,
                "channel_id": channel_id,
                "guild_id": channel.get("guild_id"),
            },
            time.monotonic(),
        )

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            while True:
                rpc = RpcConnection(self.client_id)
                try:
                    # Check saved authorization before looking for the desktop socket.
                    token = await self.token(http)
                    await rpc.connect()
                    user = await rpc.command("AUTHENTICATE", {"access_token": token})
                    self.tracker.user_id = str(user["user"]["id"])
                    self.authenticated = self.connected = True
                    self.report({"connection": "authenticated", "mode": "rpc"})
                    while True:
                        self.record_channel(await rpc.command("GET_SELECTED_VOICE_CHANNEL"))
                        self.report(self.snapshot())
                        # Local read-only polling; BarDisplay still draws only semantic changes.
                        await asyncio.sleep(1)
                except (OSError, TimeoutError, asyncio.IncompleteReadError, aiohttp.ClientError) as exc:
                    self.report({"connection": "disconnected", "mode": "rpc", "error_type": type(exc).__name__})
                finally:
                    self.connected = False
                    await rpc.close()
                await asyncio.sleep(3)
