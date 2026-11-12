"""Ordered fallback over several price sources + earliest-price backfill, and the factory building sources from config."""

from __future__ import annotations

import pandas as pd

from rwa_redeems.config import PriceSourceConfig
from rwa_redeems.pricing.base import PriceSource, PricingContext, source_class

RESULT_COLUMNS = ["price_usd", "price_timestamp", "price_source", "price_is_backfilled"]


class CompositePriceSource:
    def __init__(self, sources: list[PriceSource]):
        if not sources:
            raise ValueError("at least one price source required")
        self.sources = sources

    def lookup(self, blocks: pd.Series, timestamps: pd.Series) -> pd.DataFrame:
        """Each row gets the first source (in config order) that has a price for its block. Rows no source can price
        get the nearest later known price (i.e. the earliest available price for pre-oracle history), flagged
        `price_is_backfilled`; if there is no later price, the nearest earlier one."""
        out = pd.DataFrame(index=blocks.index, columns=RESULT_COLUMNS)
        out["price_usd"] = float("nan")
        out["price_timestamp"] = float("nan")
        out["price_source"] = None
        out["price_is_backfilled"] = False

        for src in self.sources:
            mask = out["price_usd"].isna()
            if not mask.any():
                break
            res = src.lookup(blocks[mask], timestamps[mask])
            got = res.index[res["price"].notna()]
            out.loc[got, "price_usd"] = res.loc[got, "price"]
            out.loc[got, "price_timestamp"] = res.loc[got, "price_timestamp"]
            out.loc[got, "price_source"] = src.label

        missing = out["price_usd"].isna()
        if missing.any() and (~missing).any():
            order = blocks.sort_values(kind="stable").index
            filled = out.loc[order, ["price_usd", "price_timestamp", "price_source"]].bfill().ffill()
            idx = out.index[missing]
            out.loc[idx, ["price_usd", "price_timestamp"]] = filled.loc[idx, ["price_usd", "price_timestamp"]]
            out.loc[idx, "price_source"] = "backfill:" + filled.loc[idx, "price_source"].astype(str)
            out.loc[idx, "price_is_backfilled"] = True
        return out


class PriceSourceFactory:
    """Builds (and memoises) price sources so caches/series are shared between tokens and contracts."""

    def __init__(self, contexts: dict[str, PricingContext]):
        self._contexts = contexts
        self._built: dict[tuple[str, str], PriceSource] = {}

    def build(self, chain_key: str, cfg: PriceSourceConfig) -> PriceSource:
        key = (chain_key, cfg.model_dump_json())
        if key not in self._built:
            self._built[key] = source_class(cfg.type).from_config(cfg, self._contexts[chain_key])
        return self._built[key]

    def composite(self, chain_key: str, cfgs: list[PriceSourceConfig]) -> CompositePriceSource:
        return CompositePriceSource([self.build(chain_key, c) for c in cfgs])

    def observations(self) -> pd.DataFrame:
        frames = [
            src.observations().assign(chain=chain_key)
            for (chain_key, _), src in self._built.items()
        ]
        frames = [f for f in frames if len(f)]
        if not frames:
            return pd.DataFrame(columns=["chain", "source", "block_number", "price", "price_timestamp"])
        df = pd.concat(frames, ignore_index=True)
        return df[["chain", "source", "block_number", "price", "price_timestamp"]]
