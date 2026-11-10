import json
import os
import tempfile

from pathlib import Path


def save_private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class TokenStore:
    def __init__(self, path: Path, bootstrap: str = ""):
        self.path = path
        self.bootstrap = bootstrap
        self.current = ""

    def load(self) -> str:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            if self.bootstrap:
                self.save(self.bootstrap)
            return self.current
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("Invalid token state")
        self.current = token
        return self.current

    def save(self, token: str) -> bool:
        if not token or (token == self.current and self.path.exists()):
            return False
        save_private_json(self.path, {"token": token})
        self.current = token
        return True
