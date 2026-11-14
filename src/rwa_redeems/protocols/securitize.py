"""Securitize DS tokens (BUIDL, ...).

Three redemption paths, each with exactly one row source:

- request: the holder transfers tokens to the token's redemption wallet (`params.redemption_wallet`); the issuer
  burns them later and settles off-chain. Row source: token `Transfer(from, to == wallet)`, filtered on topic2.
- instant via Redemption.sol: `redeem()` sends the tokens to `settlement.recipient()` and the settlement pays the
  stablecoin 1:1. Redemption.sol emits no redemption event; its settlement emits `SettlementCompleted(redeemer,
  amount)` with `amount` in payout-token units. Redemption.sol can switch to a new settlement (`SettlementUpdated`),
  so the settlements are discovered from that log and each one is read from the block it was set.
- instant via SecuritizeOffRamp: `RedemptionCompleted(redeemer, dsTokenValue, liquidityValue, rate, fee,
  liquidityToken)` on the off-ramp.

Double counting: the wallets that receive the tokens in instant redemptions (settlement recipients, off-ramp
liquidity wallets) later sweep them to the redemption wallet. Those sweeps are not requests. The liquidity wallets
are the union of
  (a) where the tokens actually went in every instant redemption: in the tx receipt, follow the token Transfer of the
      redeemed amount from the redeemer, through the instant contract itself for two-step off-ramps;
  (b) every recipient each settlement ever had: `RecipientUpdated` (old and new) plus `recipient()` at the last block,
      which covers a recipient set in the constructor without an event.
Redemption-wallet transfers from those wallets are dropped, and so are mints (from == 0x0). Burns are not rows:
burns from the redemption wallet repeat the requests; other burns are share-class swaps or bridging.

Config roles: `token` (one; params.redemption_wallet), `redemption` (Redemption.sol contracts; params.token_out =
payout asset), `offramps` (SecuritizeOffRamp contracts).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from rwa_redeems.config import ContractConfig, TokenConfig
from rwa_redeems.protocols.base import CollectResult, ProtocolAdapter, register

ZERO = "0x" + "0" * 40
MAX_HOPS = 4  # token transfers followed through instant contracts inside one redemption tx


def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:]


@register("securitize")
class SecuritizeAdapter(ProtocolAdapter):
    SETTLEMENT_ABI = "securitize/Settlement"

    def collect(self, token: TokenConfig) -> CollectResult:
        token_contract = token.require_contract("token")
        wallet = token_contract.params.get("redemption_wallet")
        if not wallet:
            raise ValueError(f"{token.symbol}: securitize token needs params.redemption_wallet on the 'token' contract")
        wallet = wallet.lower()

        settlements = self._settlements(token)
        settlement_rows, settlement_checks, recipients = self._settlement_rows(token, settlements)
        offramp_rows, offramp_checks = self._offramp_rows(token)
        instant = pd.DataFrame(settlement_rows + offramp_rows)

        pass_through = (
            {c.address for c in token.contracts_for("redemption")}
            | {c.address for c in token.contracts_for("offramps")}
            | {s.address for s, _ in settlements}
        )
        traced = self._trace_liquidity_wallets(token, instant, pass_through)
        liquidity_wallets = set(traced.dropna()) | recipients

        request_rows, request_checks = self._request_rows(token, token_contract, wallet, liquidity_wallets)

        frames = [f for f in (pd.DataFrame(request_rows), instant) if not f.empty]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        checks: dict[str, Any] = {
            **request_checks,
            "instant": len(instant),
            "instant_untraced": int(traced.isna().sum()),
            # sweeps are excluded by sender, however many instant redemptions they bundle; swept amount should be
            # about instant token_in + dividend mints received while the wallet held the tokens
            "liquidity_wallets": {
                w: {
                    "instant_redemptions": int((traced == w).sum()),
                    "instant_token_in": float(instant.loc[traced == w, "token_amount"].sum()) if len(instant) else 0.0,
                    "settlement_recipient": w in recipients,
                    **request_checks["excluded_liquidity_sweeps"].get(w, {"sweeps": 0, "token_amount": 0.0}),
                }
                for w in sorted(liquidity_wallets)
            },
            "settlements": settlement_checks,
            "offramps": offramp_checks,
        }
        return CollectResult(df, checks)

    # ------------------------------------------------------------------ request path

    def _request_rows(
        self, token: TokenConfig, token_contract: ContractConfig, wallet: str, liquidity_wallets: set[str]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        transfers = self.ctx.fetch(token, token_contract, ["Transfer"], where={"to": wallet})
        transfers = transfers.rename(columns={"from": "sender", "to": "receiver"})  # `from` is a Python keyword
        burns = self.ctx.fetch(token, token_contract, ["Burn"], where={"burner": wallet})

        is_mint = transfers["sender"] == ZERO
        is_sweep = transfers["sender"].isin(liquidity_wallets)
        rows = [
            self.base_row(token, ev)
            | {
                "contract": token.address,
                "contract_name": token_contract.name or "token",
                "redemption_type": "request",
                "method": "transfer_to_redemption_wallet",
                "caller": ev.sender,
                "holder": ev.sender,
                "token_amount": self.scale(ev.value, token.decimals),
                "token_amount_raw": str(ev.value),
            }
            for ev in transfers[~is_mint & ~is_sweep].itertuples(index=False)
        ]
        sweeps = transfers[is_sweep]
        checks = {
            "redemption_wallet": {
                "address": wallet,
                "transfers_in": len(transfers),
                "token_in": self._total(transfers["value"], token.decimals),
                "burns": len(burns),
                "token_burned": self._total(burns["value"], token.decimals),
            },
            "request": len(rows),
            "excluded_mints": int(is_mint.sum()),
            "excluded_liquidity_sweeps": {
                w: {"sweeps": len(g), "token_amount": self._total(g["value"], token.decimals)}
                for w, g in sweeps.groupby("sender")
            },
        }
        return rows, checks

    # ------------------------------------------------------------------ instant: Redemption.sol + settlements

    def _settlements(self, token: TokenConfig) -> list[tuple[ContractConfig, ContractConfig]]:
        """(settlement, redemption contract) for every settlement a Redemption.sol ever pointed to, read from the
        block it was set."""
        out: dict[str, tuple[ContractConfig, ContractConfig]] = {}
        for redemption in token.contracts_for("redemption"):
            for ev in self.ctx.fetch(token, redemption, ["SettlementUpdated"]).itertuples(index=False):
                if ev.newSettlement != ZERO and ev.newSettlement not in out:
                    settlement = ContractConfig(
                        name=redemption.name,
                        address=ev.newSettlement,
                        abi=self.SETTLEMENT_ABI,
                        from_block=ev.block_number,
                    )
                    out[ev.newSettlement] = (settlement, redemption)
        return list(out.values())

    def _settlement_rows(
        self, token: TokenConfig, settlements: list[tuple[ContractConfig, ContractConfig]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        rows: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        all_recipients: set[str] = set()
        for settlement, redemption in settlements:
            token_out = redemption.params.get("token_out")
            asset = self.ctx.asset(token_out)
            if asset is None:
                raise ValueError(f"redemption contract {redemption.address}: params.token_out must be a configured chain asset")
            token_out = token_out.lower()

            events = self.ctx.fetch(token, settlement, ["SettlementCompleted"])
            for ev in events.itertuples(index=False):
                # the payout is 1:1 in value; `amount` is in payout-token decimals
                raw = int(ev.amount) * 10**token.decimals // 10**asset.decimals
                received = self.scale(ev.amount, asset.decimals)
                rows.append(
                    self.base_row(token, ev)
                    | {
                        "contract": redemption.address,
                        "contract_name": redemption.name or "redemption",
                        "redemption_type": "instant",
                        "method": "settlement_completed",
                        "caller": ev.redeemer,
                        "holder": ev.redeemer,
                        "recipient": ev.redeemer,
                        "token_amount": self.scale(raw, token.decimals),
                        "token_amount_raw": str(raw),
                        "token_out": token_out,
                        "token_out_symbol": asset.symbol,
                        "token_out_amount": received,
                        "token_out_fee_amount": 0.0,
                        "token_out_usd": received * asset.usd_price if asset.usd_price is not None else None,
                    }
                )

            history = self.ctx.fetch(token, settlement, ["RecipientUpdated"])
            recipients = [r for r in [*history["oldRecipient"], *history["newRecipient"]] if r != ZERO]
            current = self._recipient(settlement.address)
            if current and current != ZERO:
                recipients.append(current)
            recipients = list(dict.fromkeys(recipients))
            all_recipients |= set(recipients)
            checks.append(
                {
                    "redemption": redemption.address,
                    "settlement": settlement.address,
                    "set_at_block": settlement.from_block,
                    "events": len(events),
                    "recipient_updates": [
                        {"block": int(h.block_number), "old": h.oldRecipient, "new": h.newRecipient}
                        for h in history.itertuples(index=False)
                    ],
                    "recipient_now": current,
                    "recipients": recipients,
                }
            )
        return rows, checks, all_recipients

    def _recipient(self, settlement: str) -> str | None:
        fn = self.ctx.fetcher.abis.function(self.SETTLEMENT_ABI, "recipient")
        result = self.ctx.rpc.eth_call(settlement, fn.encode(), self.ctx.to_block)
        return next(iter(fn.decode(result).values())) if result else None

    # ------------------------------------------------------------------ instant: SecuritizeOffRamp

    def _offramp_rows(self, token: TokenConfig) -> tuple[list[dict[str, Any]], dict[str, int]]:
        rows: list[dict[str, Any]] = []
        checks: dict[str, int] = {}
        for offramp in token.contracts_for("offramps"):
            events = self.ctx.fetch(token, offramp, ["RedemptionCompleted"])
            checks[offramp.name or offramp.address] = len(events)
            for ev in events.itertuples(index=False):
                asset = self.ctx.asset(ev.liquidityToken)
                if asset is None:
                    raise ValueError(f"off-ramp {offramp.address}: payout token {ev.liquidityToken} is not a configured chain asset")
                received = self.scale(ev.liquidityValue, asset.decimals)
                rows.append(
                    self.base_row(token, ev)
                    | {
                        "contract": offramp.address,
                        "contract_name": offramp.name or "offramp",
                        "redemption_type": "instant",
                        "method": "redemption_completed",
                        "caller": ev.redeemer,
                        "holder": ev.redeemer,
                        "recipient": ev.redeemer,
                        "token_amount": self.scale(ev.dsTokenValue, token.decimals),
                        "token_amount_raw": str(ev.dsTokenValue),
                        "token_out": ev.liquidityToken,
                        "token_out_symbol": asset.symbol,
                        "token_out_amount": received,
                        "token_out_fee_amount": self.scale(ev.fee, asset.decimals),
                        "token_out_usd": received * asset.usd_price if asset.usd_price is not None else None,
                    }
                )
        return rows, checks

    # ------------------------------------------------------------------ double-counting guard

    def _trace_liquidity_wallets(self, token: TokenConfig, instant: pd.DataFrame, pass_through: set[str]) -> pd.Series:
        """Per instant row: the wallet that received the redeemed tokens (None if the receipt has no such transfer)."""
        if instant.empty:
            return pd.Series(dtype="object")
        transfer_topic = self.ctx.fetcher.abis.event(token.require_contract("token").abi, "Transfer").topic0
        receipts = self.ctx.receipt_logs(instant["tx_hash"].tolist())

        def trace(tx_hash: str, holder: str, amount: int) -> str | None:
            legs = [
                (_topic_address(lg["topics"][1]), _topic_address(lg["topics"][2]), int(lg["data"], 16))
                for lg in receipts[tx_hash]
                if lg["address"] == token.address and len(lg["topics"]) == 3 and lg["topics"][0] == transfer_topic
            ]
            current, used = holder, set()
            for _ in range(MAX_HOPS):
                step = next((i for i, (src, _, v) in enumerate(legs) if i not in used and src == current and v == amount), None)
                if step is None:
                    return None
                used.add(step)
                current = legs[step][1]
                if current not in pass_through:
                    return current
            return None

        return pd.Series(
            [trace(r.tx_hash, r.holder, int(r.token_amount_raw)) for r in instant.itertuples(index=False)],
            index=instant.index,
            dtype="object",
        )

    @staticmethod
    def _total(values: pd.Series, decimals: int) -> float:
        return sum(int(v) for v in values) / 10**decimals
