"""Price source interface + registry.

A price source answers "what was the USD price at (block, timestamp)?" for many events at once. New source types
are added by subclassing `PriceSource` and decorating with `@register("<type>")`; the YAML `type` selects it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pandas as pd

from rwa_redeems.abi import AbiRegistry
from rwa_redeems.sources.rpc import RpcClient

LOOKUP_COLUMNS = ["price", "price_timestamp"]


@dataclass
class PricingContext:
    chain_key: str
    rpc: RpcClient
    abis: AbiRegistry
    http: httpx.Client
    cache_dir: Path


class PriceSource(ABC):
    type_name: ClassVar[str]

    def __init__(self, label: str):
        self.label = label

    @classmethod
    @abstractmethod
    def from_config(cls, cfg: Any, ctx: PricingContext) -> PriceSource: ...

    @abstractmethod
    def lookup(self, blocks: pd.Series, timestamps: pd.Series) -> pd.DataFrame:
        """Return a frame indexed like `blocks` with `price` (float, NaN if unknown) and
        `price_timestamp` (unix seconds, NaN if unknown)."""

    def observations(self) -> pd.DataFrame:
        """Prices this source looked up/loaded during the run (for prices.csv)."""
        return pd.DataFrame(columns=["source", "block_number", "price", "price_timestamp"])


_REGISTRY: dict[str, type[PriceSource]] = {}


def register(type_name: str):
    def deco(cls: type[PriceSource]) -> type[PriceSource]:
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return deco


def source_class(type_name: str) -> type[PriceSource]:
    if type_name not in _REGISTRY:
        raise KeyError(f"unknown price source type {type_name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[type_name]
