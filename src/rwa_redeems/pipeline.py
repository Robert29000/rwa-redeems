"""Orchestration: config → adapters → pricing → flags → CSV/JSON outputs. Protocol-agnostic."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from rwa_redeems import schema
from rwa_redeems.abi import AbiRegistry
from rwa_redeems.config import AppConfig, TokenConfig
from rwa_redeems.events import EventFetcher
from rwa_redeems.pricing import PriceSourceFactory, PricingContext
from rwa_redeems.protocols import AdapterContext, adapter_class
from rwa_redeems.settings import Settings
from rwa_redeems.sources.cache import CachedLogSource
from rwa_redeems.sources.logs import EtherscanLogSource
from rwa_redeems.sources.rpc import RpcClient

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    redemptions: pd.DataFrame
    prices: pd.DataFrame
    manifest: dict[str, Any]


class Pipeline:
    def __init__(self, settings: Settings, config: AppConfig, *, refresh: bool = False, to_block: int | None = None):
        self.settings = settings
        self.config = config
        self._to_block_override = to_block
        self._to_blocks: dict[str, int] = {}
        if refresh and settings.raw_dir.exists():
            shutil.rmtree(settings.raw_dir)

        self.http = httpx.Client(timeout=httpx.Timeout(60.0), follow_redirects=True)
        self.abis = AbiRegistry(settings.abi_dir)
        log_source = CachedLogSource(
            EtherscanLogSource(settings.etherscan_api_key, self.http), settings.raw_dir / "logs"
        )
        self.fetcher = EventFetcher(log_source, self.abis)
        self.rpcs = {key: RpcClient(chain.resolved_rpc_url(), self.http) for key, chain in config.chains.items()}
        self.prices = PriceSourceFactory(
            {
                key: PricingContext(key, rpc, self.abis, self.http, settings.raw_dir / "prices")
                for key, rpc in self.rpcs.items()
            }
        )

    def rpc(self, chain_key: str) -> RpcClient:
        return self.rpcs[chain_key]

    def to_block(self, chain_key: str) -> int:
        if chain_key not in self._to_blocks:
            if self._to_block_override is not None:
                self._to_blocks[chain_key] = self._to_block_override
            else:
                head = self.rpc(chain_key).block_number()
                self._to_blocks[chain_key] = head - self.config.chains[chain_key].confirmations
        return self._to_blocks[chain_key]

    # ------------------------------------------------------------------ per token

    def collect_token(self, token: TokenConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
        to_block = self.to_block(token.chain)
        log.info("%s (%s on %s): blocks %d..%d", token.symbol, token.protocol, token.chain, token.start_block, to_block)
        ctx = AdapterContext(
            chain_key=token.chain,
            chain=self.config.chains[token.chain],
            to_block=to_block,
            fetcher=self.fetcher,
            rpc=self.rpc(token.chain),
            prices=self.prices,
            cache_dir=self.settings.raw_dir,
        )
        result = adapter_class(token.protocol)(ctx).collect(token)
        df = result.redemptions
        if df.empty:
            df = schema.empty()
        else:
            df = self._price(token, df)
            df["is_pre_launch"] = (
                df["block_number"] < token.live_from_block if token.live_from_block is not None else False
            )
            df = schema.conform(df)
        entry = {
            "protocol": token.protocol,
            "chain": token.chain,
            "from_block": token.start_block,
            "to_block": to_block,
            "rows": len(df),
            "by_type": _counts(df["redemption_type"]),
            "by_method": _counts(df["method"]),
            "by_contract": _counts(df["contract_name"]),
            "price_sources": _counts(df["price_source"]),
            "unpriced": int(df["price_usd"].isna().sum()),
            "backfilled_prices": int(df["price_is_backfilled"].fillna(False).sum()),
            "pre_launch": int(df["is_pre_launch"].fillna(False).sum()),
            "price_consistency": _price_consistency(df),
            "checks": result.checks,
        }
        log.info("%s: %d redemptions %s", token.symbol, len(df), entry["by_type"])
        return df, entry

    def _price(self, token: TokenConfig, df: pd.DataFrame) -> pd.DataFrame:
        """Token NAV/price at each event block, using the contract's price sources if overridden."""
        parts = []
        for contract, idx in df.groupby("contract").groups.items():
            sources = self.prices.composite(token.chain, token.price_sources_for(contract))
            sub = df.loc[idx]
            parts.append(sources.lookup(sub["block_number"], sub["timestamp"]))
        prices = pd.concat(parts).loc[df.index]
        df = df.drop(columns=[c for c in prices.columns if c in df.columns]).join(prices)
        df["price_usd"] = df["price_usd"].astype(float)
        df["amount_usd"] = df["token_amount"].astype(float) * df["price_usd"]
        return df

    # ------------------------------------------------------------------ run / write

    def run(self, symbols: list[str] | None = None) -> RunResult:
        frames, tokens_manifest = [], {}
        for token in self.config.select(symbols):
            df, entry = self.collect_token(token)
            frames.append(df)
            tokens_manifest[f"{token.chain}:{token.symbol}"] = entry
        redemptions = pd.concat(frames, ignore_index=True) if frames else schema.empty()
        redemptions = redemptions.sort_values(["timestamp", "chain", "token", "log_index"], ignore_index=True)
        prices = self.prices.observations()
        prices["price_timestamp"] = pd.to_datetime(prices["price_timestamp"].astype(float), unit="s", utc=True)
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "to_blocks": dict(self._to_blocks),
            "total_rows": len(redemptions),
            "tokens": tokens_manifest,
        }
        return RunResult(redemptions, prices, manifest)

    @staticmethod
    def write(result: RunResult, output_dir: Path) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "redemptions": output_dir / "redemptions.csv",
            "prices": output_dir / "prices.csv",
            "manifest": output_dir / "manifest.json",
        }
        result.redemptions.to_csv(paths["redemptions"], index=False)
        result.prices.to_csv(paths["prices"], index=False)
        paths["manifest"].write_text(json.dumps(result.manifest, indent=2, default=_json_default))
        return paths


def _counts(s: pd.Series) -> dict[str, int]:
    return {str(k): int(v) for k, v in s.value_counts(dropna=False).items()}


def _price_consistency(df: pd.DataFrame) -> dict[str, Any]:
    """For instant redemptions, compare the payout-implied price ((payout + payout fee) / (tokens - token fee))
    with the oracle price used for valuation. Exact for Superstate; Midas may differ by swap slippage."""
    rows = df[(df["redemption_type"] == "instant") & df["token_out_usd"].notna() & df["price_usd"].notna()]
    out: dict[str, Any] = {}
    for name, g in rows.groupby("contract_name"):
        out_price = g["token_out_usd"] / g["token_out_amount"]
        payout_before_fee = g["token_out_usd"] + g["token_out_fee_amount"].fillna(0) * out_price
        net_tokens = g["token_amount"] - g["fee_token_amount"].fillna(0)
        rel = (payout_before_fee / net_tokens / g["price_usd"] - 1).abs().astype(float).dropna()
        if rel.empty:
            continue
        out[str(name)] = {
            "n": len(rel),
            "median_abs_rel_diff": float(rel.median()),
            "p95_abs_rel_diff": float(rel.quantile(0.95)),
            "max_abs_rel_diff": float(rel.max()),
            "share_within_0.1pct": float((rel < 1e-3).mean()),
        }
    return out


def _json_default(o: Any) -> Any:
    if hasattr(o, "item"):
        return o.item()
    return str(o)
