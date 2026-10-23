"""Versioned on-disk cache of fingerprints.

Compared with the original ``pd.to_pickle`` dumps this is smaller, atomic, and
carries a schema version so a change to the record layout rebuilds cleanly
instead of crashing on a stale file.
"""

from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any

import numpy as np

CACHE_SCHEMA = 2


def _to_disk(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for record in records:
        item = dict(record)
        item["fp"] = np.asarray(item["fp"], dtype=np.uint32).tobytes()
        out.append(item)
    return out


def _from_disk(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for record in records:
        item = dict(record)
        raw = item.get("fp")
        if isinstance(raw, (bytes, bytearray, memoryview)):
            item["fp"] = np.frombuffer(raw, dtype=np.uint32)
        else:
            item["fp"] = np.asarray(raw if raw is not None else [], dtype=np.uint32)
        out.append(item)
    return out


class FingerprintCache:
    """Reads and writes one cache file. Failures degrade to "no cache"."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.last_error: str | None = None

    def load(self) -> list[dict] | None:
        """Return cached records, or ``None`` if the cache is unusable."""
        self.last_error = None
        if not self.path.is_file():
            return None
        try:
            payload: Any = pickle.loads(self.path.read_bytes())
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ValueError) as exc:
            self.last_error = f"Cache unreadable ({exc}); rebuilding."
            return None

        if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
            self.last_error = "Cache schema changed; rebuilding."
            return None

        records = payload.get("records")
        if not isinstance(records, list):
            self.last_error = "Cache payload malformed; rebuilding."
            return None

        try:
            return _from_disk(records)
        except (TypeError, ValueError) as exc:
            self.last_error = f"Cache records malformed ({exc}); rebuilding."
            return None

    def save(self, records: list[dict]) -> None:
        """Write atomically so a crash never leaves a half-written cache."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": CACHE_SCHEMA, "records": _to_disk(records)}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
        os.replace(tmp, self.path)

    def clear(self) -> bool:
        """Delete the cache file. Returns True if something was removed."""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False