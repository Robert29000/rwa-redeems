"""Price from an on-chain view call (archive `eth_call`) at each event block. Works for any Chainlink-compatible
feed (`latestRoundData`) or any view returning a scaled price (e.g. Midas `getDataInBase18`)."""

from __future__ import annotations

import logging
import math

import pandas as pd

from rwa_redeems.abi import FunctionSpec
from rwa_redeems.config import RpcCallSourceConfig
from rwa_redeems.pricing.base import PriceSource, PricingContext, register
from rwa_redeems.sources.cache import JsonlKV
from rwa_redeems.sources.rpc import RpcClient

log = logging.getLogger(__name__)


@register("rpc_call")
class RpcCallPriceSource(PriceSource):
    def __init__(self, cfg: RpcCallSourceConfig, rpc: RpcClient, fn: FunctionSpec, cache: JsonlKV):
        super().__init__(cfg.label)
        self.cfg = cfg
        self._rpc = rpc
        self._fn = fn
        self._calldata = fn.encode()
        self._cache = cache
        names = fn.output_names
        self._price_field = cfg.output or ("answer" if "answer" in names else names[0])
        self._ts_field = cfg.timestamp_output or ("updatedAt" if "updatedAt" in names else None)
        if self._price_field not in names:
            raise ValueError(f"{cfg.label}: output {self._price_field!r} not in {names}")
        self._seen_blocks: set[int] = set()

    @classmethod
    def from_config(cls, cfg: RpcCallSourceConfig, ctx: PricingContext) -> RpcCallPriceSource:
        fn = ctx.abis.function(cfg.abi, cfg.fn)
        cache = JsonlKV(ctx.cache_dir / ctx.chain_key / f"{cfg.address}_{cfg.fn}.jsonl")
        return cls(cfg, ctx.rpc, fn, cache)

    def _call(self, block: int) -> dict | None:
        data = self._rpc.eth_call(self.cfg.address, self._calldata, block)
        if data is None:
            return None
        try:
            out = self._fn.decode(data)
        except Exception:  # unexpected return shape (e.g. proxy not initialised yet)
            log.warning("%s: undecodable return at block %d", self.label, block)
            return None
        return {"raw": str(out[self._price_field]), "ts": out.get(self._ts_field) if self._ts_field else None}

    def _value(self, block: int) -> tuple[float, float]:
        rec = self._cache.get(str(block))
        if rec is None:
            return math.nan, math.nan
        raw = int(rec["raw"])
        if raw <= 0:
            return math.nan, math.nan
        ts = rec.get("ts")
        return raw / 10**self.cfg.decimals, (float(ts) if ts else math.nan)

    def lookup(self, blocks: pd.Series, timestamps: pd.Series) -> pd.DataFrame:
        min_block = self.cfg.from_block or 0
        wanted = sorted({int(b) for b in blocks if int(b) >= min_block})
        missing = [b for b in wanted if str(b) not in self._cache]
        if missing:
            log.info("  %s: %d eth_calls (%d cached)", self.label, len(missing), len(wanted) - len(missing))
            results = self._rpc.map(lambda b: (b, self._call(b)), missing)
            self._cache.put_many({str(b): r for b, r in results})
        self._seen_blocks.update(wanted)
        values = [self._value(int(b)) if int(b) >= min_block else (math.nan, math.nan) for b in blocks]
        return pd.DataFrame(values, index=blocks.index, columns=["price", "price_timestamp"])

    def observations(self) -> pd.DataFrame:
        rows = []
        for b in sorted(self._seen_blocks):
            price, ts = self._value(b)
            rows.append({"source": self.label, "block_number": b, "price": price, "price_timestamp": ts})
        return pd.DataFrame(rows, columns=["source", "block_number", "price", "price_timestamp"])
