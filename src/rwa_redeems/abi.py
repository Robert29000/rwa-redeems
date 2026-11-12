"""ABI registry: loads JSON ABIs from `abis/` and exposes event decoders / function codecs.

Topics and selectors are always derived from the ABI, never hardcoded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import eth_abi
from eth_utils import (
    event_abi_to_log_topic,
    function_abi_to_4byte_selector,
    get_abi_input_types,
    get_abi_output_names,
    get_abi_output_types,
    to_checksum_address,
)
from hexbytes import HexBytes
from web3 import Web3


@dataclass(frozen=True, slots=True)
class RawLog:
    """Source-agnostic log record (all hex strings lowercase)."""

    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    timestamp: int
    tx_hash: str
    log_index: int
    tx_index: int

    @property
    def key(self) -> tuple[str, int]:
        return (self.tx_hash, self.log_index)

    def to_json(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "topics": list(self.topics),
            "data": self.data,
            "block_number": self.block_number,
            "timestamp": self.timestamp,
            "tx_hash": self.tx_hash,
            "log_index": self.log_index,
            "tx_index": self.tx_index,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RawLog:
        return cls(
            address=d["address"],
            topics=tuple(d["topics"]),
            data=d["data"],
            block_number=d["block_number"],
            timestamp=d["timestamp"],
            tx_hash=d["tx_hash"],
            log_index=d["log_index"],
            tx_index=d["tx_index"],
        )


def _normalize_value(v: Any) -> Any:
    if isinstance(v, str) and v.startswith("0x") and len(v) == 42:
        return v.lower()
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return type(v)(_normalize_value(x) for x in v)
    return v


class EventSpec:
    def __init__(self, abi: dict[str, Any]):
        self.abi = abi
        self.name: str = abi["name"]
        self.topic0 = "0x" + event_abi_to_log_topic(abi).hex()
        self.arg_names: list[str] = [i["name"] for i in abi["inputs"]]
        self._event = Web3().eth.contract(abi=[abi]).events[self.name]()

    def decode(self, log: RawLog) -> dict[str, Any]:
        receipt_log = {
            "address": to_checksum_address(log.address),
            "topics": [HexBytes(t) for t in log.topics],
            "data": HexBytes(log.data),
            "blockNumber": log.block_number,
            "transactionHash": HexBytes(log.tx_hash),
            "transactionIndex": log.tx_index,
            "logIndex": log.log_index,
            "blockHash": HexBytes(b"\x00" * 32),
        }
        event = self._event.process_log(receipt_log)  # type: ignore[arg-type]
        return {k: _normalize_value(v) for k, v in event["args"].items()}


class FunctionSpec:
    def __init__(self, abi: dict[str, Any]):
        self.abi = abi
        self.name: str = abi["name"]
        self.selector = function_abi_to_4byte_selector(abi)
        self.input_types = get_abi_input_types(abi)
        self.output_types = get_abi_output_types(abi)
        self.output_names = [n or str(i) for i, n in enumerate(get_abi_output_names(abi))]

    def encode(self, *args: Any) -> str:
        return "0x" + (self.selector + eth_abi.encode(self.input_types, list(args))).hex()

    def decode(self, data: bytes) -> dict[str, Any]:
        values = eth_abi.decode(self.output_types, data)
        return {n: _normalize_value(v) for n, v in zip(self.output_names, values, strict=True)}


class AbiRegistry:
    """Loads `<abi_dir>/<name>.json` (name like `midas/RedemptionVault`)."""

    def __init__(self, abi_dir: Path):
        self.abi_dir = abi_dir
        self._abis: dict[str, list[dict[str, Any]]] = {}
        self._specs: dict[tuple[str, str, str], Any] = {}

    def load(self, name: str) -> list[dict[str, Any]]:
        if name not in self._abis:
            path = self.abi_dir / f"{name}.json"
            if not path.exists():
                raise FileNotFoundError(f"ABI {name!r} not found at {path}")
            self._abis[name] = json.loads(path.read_text())
        return self._abis[name]

    def _find(self, name: str, kind: str, item: str) -> dict[str, Any]:
        matches = [x for x in self.load(name) if x.get("type") == kind and x.get("name") == item]
        if not matches:
            raise KeyError(f"{kind} {item!r} not in ABI {name!r}")
        if len(matches) > 1:
            raise KeyError(f"{kind} {item!r} is overloaded in ABI {name!r}; split the ABI file")
        return matches[0]

    def event(self, name: str, event: str) -> EventSpec:
        key = ("event", name, event)
        if key not in self._specs:
            self._specs[key] = EventSpec(self._find(name, "event", event))
        return self._specs[key]

    def function(self, name: str, fn: str) -> FunctionSpec:
        key = ("function", name, fn)
        if key not in self._specs:
            self._specs[key] = FunctionSpec(self._find(name, "function", fn))
        return self._specs[key]
