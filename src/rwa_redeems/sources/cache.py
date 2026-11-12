"""On-disk caches so reruns only fetch new data.

- `CachedLogSource`: wraps any LogSource; stores logs per (chain, address, topic0) as JSONL plus the scanned block range.
- `JsonlKV`: append-only key/value store for immutable historical lookups (oracle answers at a block, receipts).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from rwa_redeems.abi import RawLog
from rwa_redeems.sources.logs import LogSource


class CachedLogSource:
    def __init__(self, inner: LogSource, cache_dir: Path):
        self._inner = inner
        self._dir = cache_dir

    def _paths(self, chain_id: int, address: str, topic0: str) -> tuple[Path, Path]:
        base = self._dir / str(chain_id) / address.lower() / topic0.lower()
        return base.with_suffix(".jsonl"), base.with_suffix(".meta.json")

    def get_logs(
        self, chain_id: int, address: str, topic0: str, from_block: int, to_block: int
    ) -> list[RawLog]:
        data_path, meta_path = self._paths(chain_id, address, topic0)
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else None

        if meta is None or meta["from_block"] > from_block:
            logs = self._inner.get_logs(chain_id, address, topic0, from_block, to_block)
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_path.write_text("".join(json.dumps(r.to_json()) + "\n" for r in logs))
            self._write_meta(meta_path, from_block, to_block)
        elif meta["to_block"] < to_block:
            new = self._inner.get_logs(chain_id, address, topic0, meta["to_block"] + 1, to_block)
            with data_path.open("a") as f:
                f.writelines(json.dumps(r.to_json()) + "\n" for r in new)
            self._write_meta(meta_path, meta["from_block"], to_block)

        logs = {}
        if data_path.exists():
            for line in data_path.read_text().splitlines():
                if line:
                    r = RawLog.from_json(json.loads(line))
                    logs[r.key] = r  # dedupe in case a previous run was interrupted mid-write
        return sorted(
            (r for r in logs.values() if from_block <= r.block_number <= to_block),
            key=lambda r: (r.block_number, r.log_index),
        )

    @staticmethod
    def _write_meta(path: Path, from_block: int, to_block: int) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"from_block": from_block, "to_block": to_block}))
        tmp.replace(path)


class JsonlKV:
    """Append-only JSONL key/value cache; values must be JSON-serialisable."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line:
                    rec = json.loads(line)
                    self._data[rec["k"]] = rec["v"]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def put_many(self, items: dict[str, Any]) -> None:
        if not items:
            return
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a") as f:
                f.writelines(json.dumps({"k": k, "v": v}) + "\n" for k, v in items.items())
            self._data.update(items)
