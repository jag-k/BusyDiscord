"""Read-only connectivity checks. Never prints credentials or user data."""

import argparse
import json
import os
import shlex
import socket
import struct

from pathlib import Path

from busylib import BusyBar


def load_env() -> None:
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        words = shlex.split(line.removeprefix("export "), comments=True)
        if words and "=" in words[0]:
            key, value = words[0].split("=", 1)
            os.environ.setdefault(key, value)


def receive(sock: socket.socket) -> tuple[int, dict]:
    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = sock.recv(size - len(result))
            if not chunk:
                raise ConnectionError("IPC closed")
            result.extend(chunk)
        return bytes(result)

    opcode, size = struct.unpack("<II", exact(8))
    if size > 1024 * 1024:
        raise ValueError("Unexpected IPC frame size")
    return opcode, json.loads(exact(size))


def discord_probe(client_id: str | None) -> None:
    roots = dict.fromkeys(
        [os.environ[k] for k in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP") if os.environ.get(k)] + ["/tmp"]  # noqa: S108 -- Discord's documented IPC fallback, not a temporary file we create.
    )
    for root in roots:
        for index in range(10):
            path = Path(root) / f"discord-ipc-{index}"
            if not path.exists():
                continue
            try:
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(3)
                    sock.connect(str(path))
                    print(f"Discord: {path.name} connection OK")
                    if client_id:
                        payload = json.dumps({"v": 1, "client_id": client_id}).encode()
                        sock.sendall(struct.pack("<II", 0, len(payload)) + payload)
                        opcode, reply = receive(sock)
                        print(
                            "Discord handshake:", "READY" if opcode == 1 and reply.get("evt") == "READY" else "rejected"
                        )
                    else:
                        print("Discord: handshake skipped (no Application ID)")
                    return
            except OSError, ValueError:
                print(f"Discord: {path.name} connection/handshake failed")
    print("Discord: no reachable IPC socket (macOS/Linux probe)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", help="Your public Discord Application ID; only tests handshake")
    args = parser.parse_args()
    load_env()
    discord_probe(args.client_id or os.environ.get("DISCORD_CLIENT_ID"))
    addr = os.environ.get("BUSYBAR_ADDR")
    if not addr:
        print("BUSY Bar: skipped (BUSYBAR_ADDR is not configured)")
        return
    try:
        with BusyBar(addr, token=os.environ.get("BUSYBAR_TOKEN"), timeout=5, max_retries=0) as bar:
            version = bar.version()
            print("BUSY Bar: connection OK; API", version.api_semver or "unknown")
    except Exception as exc:
        # Exception messages can contain request URLs. Print only the type.
        print("BUSY Bar: connection failed;", type(exc).__name__)


if __name__ == "__main__":
    main()
