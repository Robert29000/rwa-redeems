"""Protocol adapter interface + registry.

An adapter turns one configured token into canonical redemption rows (see `schema.COLUMNS`) without prices;
pricing, flags and output are handled generically by the pipeline. Add a protocol by subclassing `ProtocolAdapter`,
decorating it with `@register("<protocol>")` and importing it in `protocols/__init__.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from rwa_redeems.config import AssetConfig, ChainConfig, ContractConfig, TokenConfig
from rwa_redeems.events import EventFetcher
from rwa_redeems.pricing import PriceSourceFactory
from rwa_redeems.sources.cache import JsonlKV
from rwa_redeems.sources.rpc import RpcClient


@dataclass
class AdapterContext:
    chain_key: str
    chain: ChainConfig
    to_block: int
    fetcher: EventFetcher
    rpc: RpcClient
    prices: PriceSourceFactory
    cache_dir: Path

    def fetch(
        self, token: TokenConfig, contract: ContractConfig, events: list[str], where: dict[str, Any] | None = None
    ) -> pd.DataFrame:
        assert contract.address is not None
        return self.fetcher.fetch(
            self.chain.chain_id,
            contract.address,
            contract.abi,
            events,
            token.from_block(contract),
            self.to_block,
            where,
        )

    def asset(self, address: str | None) -> AssetConfig | None:
        return self.chain.assets.get(address.lower()) if address else None

    def receipt_logs(self, tx_hashes: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Per tx: [{"address", "topics", "data"}, ...] in log order, lowercase hex (cached; receipts are immutable
        once confirmed)."""
        cache = JsonlKV(self.cache_dir / "receipts" / f"{self.chain_key}.logs.jsonl")
        missing = [h for h in dict.fromkeys(tx_hashes) if h not in cache]

        def fetch(h: str) -> tuple[str, list[dict[str, Any]]]:
            receipt = self.rpc.get_receipt(h)
            return h, [
                {
                    "address": lg["address"].lower(),
                    "topics": [t.lower() for t in lg["topics"]],
                    "data": lg["data"].lower(),
                }
                for lg in receipt["logs"]
            ]

        cache.put_many(dict(self.rpc.map(fetch, missing)))
        return {h: cache.get(h) for h in tx_hashes}


@dataclass
class CollectResult:
    redemptions: pd.DataFrame
    checks: dict[str, Any] = field(default_factory=dict)


class ProtocolAdapter(ABC):
    protocol: ClassVar[str]

    def __init__(self, ctx: AdapterContext):
        self.ctx = ctx

    @abstractmethod
    def collect(self, token: TokenConfig) -> CollectResult: ...

    def base_row(self, token: TokenConfig, ev: Any) -> dict[str, Any]:
        """Columns common to every protocol, from a row of an `EventFetcher` frame (itertuples)."""
        return {
            "chain": self.ctx.chain_key,
            "chain_id": self.ctx.chain.chain_id,
            "protocol": self.protocol,
            "token": token.symbol,
            "token_address": token.address,
            "tx_hash": ev.tx_hash,
            "log_index": ev.log_index,
            "block_number": ev.block_number,
            "timestamp": ev.block_timestamp,
        }

    @staticmethod
    def scale(raw: int | None, decimals: int) -> float | None:
        return None if raw is None or pd.isna(raw) else int(raw) / 10**decimals


_REGISTRY: dict[str, type[ProtocolAdapter]] = {}


def register(protocol: str):
    def deco(cls: type[ProtocolAdapter]) -> type[ProtocolAdapter]:
        cls.protocol = protocol
        _REGISTRY[protocol] = cls
        return cls

    return deco


def adapter_class(protocol: str) -> type[ProtocolAdapter]:
    if protocol not in _REGISTRY:
        raise KeyError(f"no adapter for protocol {protocol!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[protocol]
