import json
from pathlib import Path


class LocalStorage:
    def __init__(self, base_dir: str = "./data"):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def load(self, source: str) -> bytes:
        return Path(source).read_bytes()

    def save(self, data: bytes, key: str) -> str:
        dest = self._base / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return str(dest)

    def save_json(self, data: dict, key: str) -> str:
        dest = self._base / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(data, indent=2))
        return str(dest)
