"""Midas mTokens (mTBILL, mWIN, ...).

Each redemption vault emits one event per redemption:
- `RedeemInstant` / `RedeemInstantWithCustomRecipient`  → instant (payout in the same tx)
- `RedeemRequest` / `RedeemRequestWithCustomRecipient` → request; tokenOut == address(0) means a fiat request
  (`redeemFiatRequest`). Approvals/rejections are intentionally not tracked.
Event amounts are in 18-decimal base units (mToken amount, fee in mToken, amountTokenOut).

An instant redemption can source its payout liquidity in the same tx, and the row is then flagged `via_fallback`:
- Swapper vaults redeem at another Midas vault; that vault emits its own RedeemInstant with `user` == our vault.
  We only read configured vaults, so this is not double counted within one token.
- Vaults with `params.liquidity_sources` (e.g. mTBILL's RedemptionVaultWithBUIDL/WithUSTB) redeem BUIDL or USTB at an
  external contract whose event has topic1 (redeemer) == our vault.
Those nested redemptions are real redemptions of the other token, so they also show up in that token's rows
(holder = our vault) when that token is collected.

Config role: `vaults` (one or more redemption vaults, optional per-vault `price_sources`,
`params.liquidity_sources` = [{address, abi, events}]).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from rwa_redeems.config import ContractConfig, RpcCallSourceConfig, TokenConfig
from rwa_redeems.protocols.base import CollectResult, ProtocolAdapter, register

FIAT_TOKEN = "0x" + "0" * 40
BASE_DECIMALS = 18


@register("midas")
class MidasAdapter(ProtocolAdapter):
    REDEEM_EVENTS = {
        "RedeemInstant": ("instant", "redeem_instant"),
        "RedeemInstantWithCustomRecipient": ("instant", "redeem_instant_custom_recipient"),
        "RedeemRequest": ("request", "redeem_request"),
        "RedeemRequestWithCustomRecipient": ("request", "redeem_request_custom_recipient"),
    }
    PAYMENT_TOKEN_EVENTS = ["AddPaymentToken", "RemovePaymentToken"]
    # Vault code before the 2025 upgrades emitted AddPaymentToken without `allowance` (other topic0); mTBILL added
    # its payment tokens with that version.
    LEGACY_ABI = "midas/RedemptionVaultLegacy"
    LEGACY_PAYMENT_TOKEN_EVENTS = ["AddPaymentToken"]

    def collect(self, token: TokenConfig) -> CollectResult:
        vaults = token.contracts_for("vaults")
        if not vaults:
            raise ValueError(f"{token.symbol}: midas token needs at least one contract under 'vaults'")
        frames, checks = [], {}
        for vault in vaults:
            events = self.ctx.fetch(token, vault, list(self.REDEEM_EVENTS))
            payment_tokens = self._payment_tokens(self._payment_token_events(token, vault))
            rows = pd.DataFrame([self._row(token, vault, ev) for ev in events.itertuples(index=False)])
            if not rows.empty:
                rows["token_out_usd"] = rows["token_out_amount"] * self._token_out_prices(rows, payment_tokens)
            frames.append(rows)
            checks[vault.name or vault.address] = {
                "address": vault.address,
                "events": events["event"].value_counts().to_dict(),
                "payment_tokens": payment_tokens,
            }
        non_empty = [f for f in frames if not f.empty]
        df = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()
        if not df.empty:
            df["via_fallback"] = self._via_fallback(df, vaults)
        return CollectResult(df, {"vaults": checks})

    def _row(self, token: TokenConfig, vault, ev) -> dict[str, Any]:
        redemption_type, method = self.REDEEM_EVENTS[ev.event]
        instant = redemption_type == "instant"
        amount = ev.amount if instant else ev.amountMTokenIn
        token_out = ev.tokenOut
        if not instant and token_out == FIAT_TOKEN:
            redemption_type = "fiat_request"
        recipient = ev.recipient if "custom_recipient" in method else ev.user
        asset = self.ctx.asset(token_out)
        return self.base_row(token, ev) | {
            "contract": vault.address,
            "contract_name": vault.name,
            "redemption_type": redemption_type,
            "method": method,
            "caller": ev.user,
            "holder": ev.user,
            "recipient": recipient,
            "request_id": None if instant else int(ev.requestId),
            "token_amount": self.scale(amount, BASE_DECIMALS),
            "token_amount_raw": str(amount),
            "fee_token_amount": self.scale(ev.feeAmount, BASE_DECIMALS),
            "token_out": token_out,
            "token_out_symbol": "FIAT" if token_out == FIAT_TOKEN else (asset.symbol if asset else None),
            "token_out_amount": self.scale(ev.amountTokenOut, BASE_DECIMALS) if instant else None,
        }

    def _payment_token_events(self, token: TokenConfig, vault: ContractConfig) -> pd.DataFrame:
        legacy = vault.model_copy(update={"abi": self.LEGACY_ABI})
        frames = [
            self.ctx.fetch(token, vault, self.PAYMENT_TOKEN_EVENTS),
            self.ctx.fetch(token, legacy, self.LEGACY_PAYMENT_TOKEN_EVENTS),
        ]
        return pd.concat(frames, ignore_index=True).sort_values(["block_number", "log_index"], ignore_index=True)

    @staticmethod
    def _payment_tokens(events: pd.DataFrame) -> dict[str, dict[str, Any]]:
        """Latest AddPaymentToken per token (data feed, stable flag); `removed` if removed afterwards."""
        tokens: dict[str, dict[str, Any]] = {}
        for ev in events.itertuples(index=False):
            if ev.event == "AddPaymentToken":
                tokens[ev.token] = {"data_feed": ev.dataFeed, "stable": bool(ev.stable), "removed": False}
            elif ev.token in tokens:
                tokens[ev.token]["removed"] = True
        return tokens

    def _token_out_prices(self, rows: pd.DataFrame, payment_tokens: dict[str, dict[str, Any]]) -> pd.Series:
        """USD price of the payout token per row: stable payment token → 1.0, else fixed asset price,
        else the vault's data feed (`getDataInBase18`) at the event block."""
        prices = pd.Series(float("nan"), index=rows.index)
        for token_out, idx in rows.groupby("token_out").groups.items():
            info = payment_tokens.get(token_out)
            asset = self.ctx.asset(token_out)
            if info and info["stable"]:
                prices[idx] = 1.0
            elif asset and asset.usd_price is not None:
                prices[idx] = asset.usd_price
            elif info:
                source = self.ctx.prices.build(
                    self.ctx.chain_key,
                    RpcCallSourceConfig(
                        type="rpc_call",
                        name=f"midas data feed {info['data_feed']}",
                        address=info["data_feed"],
                        abi="midas/DataFeed",
                        fn="getDataInBase18",
                        decimals=18,
                    ),
                )
                sub = rows.loc[idx]
                prices[idx] = source.lookup(sub["block_number"], sub["timestamp"])["price"].to_numpy()
        return prices

    def _liquidity_sources(self, vault: ContractConfig) -> dict[str, set[str]]:
        """`params.liquidity_sources` as {emitter address: {topic0, ...}}."""
        sources: dict[str, set[str]] = {}
        for src in vault.params.get("liquidity_sources", []):
            topics = {self.ctx.fetcher.abis.event(src["abi"], name).topic0 for name in src["events"]}
            sources.setdefault(src["address"].lower(), set()).update(topics)
        return sources

    def _via_fallback(self, df: pd.DataFrame, vaults: list[ContractConfig]) -> pd.Series:
        """True if, in the same tx, another Midas vault's RedeemInstant or a configured liquidity source event has
        topic1 (user / redeemer) == this vault."""
        topic0 = self.ctx.fetcher.abis.event(vaults[0].abi, "RedeemInstant").topic0
        vault_addresses = {v.address for v in vaults}
        sources = {v.address: self._liquidity_sources(v) for v in vaults}
        instant = df["redemption_type"] == "instant"
        receipts = self.ctx.receipt_logs(df.loc[instant, "tx_hash"].tolist())

        def used_fallback(row) -> bool:
            vault_topic = "0x" + "0" * 24 + row.contract[2:]
            extra = sources[row.contract]
            return any(
                len(lg["topics"]) > 1
                and lg["topics"][1] == vault_topic
                and (
                    (lg["address"] not in vault_addresses and lg["topics"][0] == topic0)
                    or lg["topics"][0] in extra.get(lg["address"], ())
                )
                for lg in receipts[row.tx_hash]
            )

        flags = pd.Series(pd.NA, index=df.index, dtype="boolean")
        for i, row in df[instant].iterrows():
            flags[i] = used_fallback(row)
        return flags
