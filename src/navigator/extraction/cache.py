"""Content-addressed cache of raw provider responses.

key = sha256(canonical JSON of the key fields): source content hash, doc id,
provider, model, prompt version + rendered-prompt hash, response-schema hash and
generation settings. No timestamps. Changing any input produces a new key.

One JSON file per key. Entries store the raw response text, so every rerun
re-parses and re-validates it with the current deterministic code.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def cache_key(key_fields: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(key_fields))


class CacheIntegrityError(RuntimeError):
    pass


class ResponseCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path(key)
        if not path.is_file():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("key") != key or cache_key(entry.get("key_fields", {})) != key:
            raise CacheIntegrityError(f"cache entry {path.name} does not match its key; delete it or use --force")
        return entry

    def put(self, key: str, entry: dict[str, Any]) -> Path:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path
