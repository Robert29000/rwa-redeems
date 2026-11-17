"""Ondo RWA tokens (USDY, OUSG).

Each redemption path has exactly one row source. Contracts are configured by role; `params.kind` picks the event
layout when one role has several contract generations.

instant_managers
- kind `xmanager` (default; USDY_InstantManager, OUSG_InstantManager): `redeem(rwaAmount, receivingToken, min)` burns
  the token and pays a stablecoin in the same tx. Row source: `Redemption(redeemer, redeemerId, rwaAmount,
  receivingToken, receiveTokenAmount, redemptionUSDValue, fee)`; `fee` and `redemptionUSDValue` are USD with
  18 decimals. Redeeming the rebasing wrapper (rUSDY / rOUSG) emits the same `Redemption` plus
  `params.rebasing_event` in the same tx; those rows get method `redeem_rebasing_<symbol>` and `token_amount` in
  the base token (the wrapper amount is in the manifest checks).
- kind `ousg_legacy` (legacy OUSGInstantManager): `InstantRedemptionOUSG(sender, ousgAmountIn, usdcAmountOut)` and
  `InstantRedemptionRebasingOUSG(sender, rousgAmountIn, ousgAmountIn, usdcAmountOut)`, one per redemption;
  `usdcAmountOut` is after the fee from `RedeemFeesDeducted(sender, feeReceiver, usdcFees, usdcAmountOut)` (same tx).
  The payout asset is `params.token_out`.

request_managers
- kind `rwa_hub` (default; USDYManager, OUSGManager): `RedemptionRequestedServicedOffChain(user, redemptionId,
  amount, destination)` (USD paid off-chain → token_out FIAT) and `RedemptionRequested(user, redemptionId, amount)`
  (claimed later on-chain in `params.token_out`). The token is burned when the request is made.
- kind `cash_manager` (legacy CashManager): `RedemptionRequested(user, cashAmountIn, epoch)`; paid later in
  `params.token_out`. No request id (only the epoch).

token (optional, `params.redemption_wallet`)
- Manual redemptions: the holder transfers the token to Ondo's documented redemption wallet, which burns it later.
  Row source: token `Transfer(from, to == wallet)`, filtered on the indexed `to` at the source; mints (from == 0x0)
  are dropped.

Not collected, on purpose:
- USDY: holders also send USDY to Ondo-controlled Safes that burn it in batches. Ondo does not document that path,
  so USDY request data is NOT complete (docs/notes/usdy-redemptions.md).
- Bridge burns and wrapper unwraps (rUSDY/rOUSG → base token) are not redemptions.

Rebasing wrappers need no separate logs: their only redemption path is the managers' rebasing redeem above.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from rwa_redeems.config import ContractConfig, TokenConfig
from rwa_redeems.protocols.base import CollectResult, ProtocolAdapter, register

ZERO = "0x" + "0" * 40
FIAT_TOKEN = ZERO
USD_DECIMALS = 18


@register("ondo")
class OndoAdapter(ProtocolAdapter):
    RWA_HUB_EVENTS = {
        "RedemptionRequestedServicedOffChain": "request_redemption_serviced_offchain",
        "RedemptionRequested": "request_redemption",
    }
    LEGACY_INSTANT_EVENTS = {
        "InstantRedemptionOUSG": "redeem",
        "InstantRedemptionRebasingOUSG": "redeem_rebasing_ousg",
    }

    def collect(self, token: TokenConfig) -> CollectResult:
        instant_managers = token.contracts_for("instant_managers")
        request_managers = token.contracts_for("request_managers")
        wallet_contracts = [c for c in token.contracts_for("token") if c.params.get("redemption_wallet")]
        if not (instant_managers or request_managers or wallet_contracts):
            raise ValueError(f"{token.symbol}: ondo token needs instant_managers, request_managers or a redemption wallet")

        rows: list[dict[str, Any]] = []
        checks: dict[str, Any] = {"instant_managers": {}, "request_managers": {}}
        # known gaps, written in the config as `params.not_collected` on any contract, are repeated in the manifest
        gaps = [c.params["not_collected"] for cs in token.contracts.values() for c in cs if "not_collected" in c.params]
        if gaps:
            checks["not_collected"] = gaps
        for manager in instant_managers:
            handler = {"xmanager": self._xmanager, "ousg_legacy": self._ousg_legacy}[manager.params.get("kind", "xmanager")]
            manager_rows, checks["instant_managers"][manager.name or manager.address] = handler(token, manager)
            rows += manager_rows
        for manager in request_managers:
            handler = {"rwa_hub": self._rwa_hub, "cash_manager": self._cash_manager}[manager.params.get("kind", "rwa_hub")]
            manager_rows, checks["request_managers"][manager.name or manager.address] = handler(token, manager)
            rows += manager_rows
        for token_contract in wallet_contracts:
            wallet_rows, checks["redemption_wallet"] = self._wallet_transfers(token, token_contract)
            rows += wallet_rows
        return CollectResult(pd.DataFrame(rows), checks)

    # ------------------------------------------------------------------ instant

    def _payout(self, manager: ContractConfig, address: str) -> Any:
        asset = self.ctx.asset(address)
        if asset is None or asset.usd_price is None:
            raise ValueError(f"{manager.address}: payout token {address} must be a configured chain asset with usd_price")
        return asset

    def _instant_row(self, token, manager, ev, method, holder, amount, token_out, received_raw, fee) -> dict:
        asset = self._payout(manager, token_out)
        received = self.scale(received_raw, asset.decimals)
        return self.base_row(token, ev) | {
            "contract": manager.address,
            "contract_name": manager.name,
            "redemption_type": "instant",
            "method": method,
            "caller": holder,
            "holder": holder,
            "recipient": holder,  # the managers pay msg.sender
            "token_amount": self.scale(amount, token.decimals),
            "token_amount_raw": str(amount),
            "fee_token_amount": 0.0,  # fees are charged on the payout side
            "token_out": token_out,
            "token_out_symbol": asset.symbol,
            "token_out_amount": received,
            "token_out_fee_amount": fee,  # in payout-token units
            "token_out_usd": received * asset.usd_price,
        }

    def _xmanager(self, token: TokenConfig, manager: ContractConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rebasing_event = manager.params.get("rebasing_event")
        amount_field = manager.params.get("rebasing_amount_field")
        wrapper_field = manager.params.get("rebasing_wrapper_field")
        events = self.ctx.fetch(token, manager, ["Redemption"] + ([rebasing_event] if rebasing_event else []))
        redemptions = events[events["event"] == "Redemption"]
        rebasing = events[events["event"] == rebasing_event] if rebasing_event else events.iloc[0:0]
        rebasing_keys = {(ev.tx_hash, int(getattr(ev, amount_field))) for ev in rebasing.itertuples(index=False)}

        rows, matched = [], 0
        for ev in redemptions.itertuples(index=False):
            rebased = (ev.tx_hash, int(ev.rwaAmount)) in rebasing_keys
            matched += rebased
            asset = self._payout(manager, ev.receivingToken)
            fee = self.scale(ev.fee, USD_DECIMALS) / asset.usd_price
            method = f"redeem_rebasing_{token.symbol.lower()}" if rebased else "redeem"
            rows.append(
                self._instant_row(token, manager, ev, method, ev.redeemer, ev.rwaAmount, ev.receivingToken,
                                  ev.receiveTokenAmount, fee)
            )
        checks: dict[str, Any] = {
            "address": manager.address,
            "events": events["event"].value_counts().to_dict(),
            "redemption_usd_value": sum(self.scale(v, USD_DECIMALS) for v in redemptions["redemptionUSDValue"]),
            "fees_usd": sum(self.scale(v, USD_DECIMALS) for v in redemptions["fee"]),
        }
        if rebasing_event:
            # every wrapper redemption must also have a Redemption event with the same base-token amount
            checks["rebasing_unmatched"] = len(rebasing) - matched
            checks["rebasing_wrapper_in"] = sum(self.scale(v, token.decimals) for v in rebasing[wrapper_field])
            checks["rebasing_token_in"] = sum(self.scale(v, token.decimals) for v in rebasing[amount_field])
        return rows, checks

    def _ousg_legacy(self, token: TokenConfig, manager: ContractConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        token_out = (manager.params.get("token_out") or "").lower()
        asset = self._payout(manager, token_out)
        events = self.ctx.fetch(token, manager, list(self.LEGACY_INSTANT_EVENTS) + ["RedeemFeesDeducted"])
        fees = {
            (ev.tx_hash, ev.sender, int(ev.usdcAmountOut)): int(ev.usdcFees)
            for ev in events[events["event"] == "RedeemFeesDeducted"].itertuples(index=False)
        }
        rows, unmatched_fees = [], 0
        for ev in events[events["event"].isin(list(self.LEGACY_INSTANT_EVENTS))].itertuples(index=False):
            fee_raw = fees.get((ev.tx_hash, ev.sender, int(ev.usdcAmountOut)))
            unmatched_fees += fee_raw is None
            rows.append(
                self._instant_row(token, manager, ev, self.LEGACY_INSTANT_EVENTS[ev.event], ev.sender, ev.ousgAmountIn,
                                  token_out, ev.usdcAmountOut, self.scale(fee_raw, asset.decimals))
            )
        checks = {
            "address": manager.address,
            "events": events["event"].value_counts().to_dict(),
            "fees_unmatched": unmatched_fees,  # redemptions without a RedeemFeesDeducted in the same tx
            "fees_usdc": sum(self.scale(v, asset.decimals) for v in fees.values()),
        }
        return rows, checks

    # ------------------------------------------------------------------ requests

    def _request_row(self, token, contract, ev, method, holder, amount, token_out, request_id=None) -> dict:
        asset = self.ctx.asset(token_out)
        return self.base_row(token, ev) | {
            "contract": contract.address,
            "contract_name": contract.name,
            "redemption_type": "request",
            "method": method,
            "caller": holder,
            "holder": holder,
            "request_id": request_id,
            "token_amount": self.scale(amount, token.decimals),
            "token_amount_raw": str(amount),
            "token_out": token_out,
            "token_out_symbol": "FIAT" if token_out == FIAT_TOKEN else (asset.symbol if asset else None),
        }

    def _rwa_hub(self, token: TokenConfig, manager: ContractConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        collateral = (manager.params.get("token_out") or "").lower() or None
        events = self.ctx.fetch(token, manager, list(self.RWA_HUB_EVENTS))
        rows = []
        for ev in events.itertuples(index=False):
            offchain = ev.event == "RedemptionRequestedServicedOffChain"
            rows.append(
                self._request_row(
                    token, manager, ev, self.RWA_HUB_EVENTS[ev.event], ev.user,
                    ev.rwaTokenAmountIn if offchain else ev.rwaAmountIn,
                    FIAT_TOKEN if offchain else collateral,
                    int(ev.redemptionId, 16),
                )
            )
        return rows, {"address": manager.address, "events": events["event"].value_counts().to_dict()}

    def _cash_manager(self, token: TokenConfig, manager: ContractConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        collateral = (manager.params.get("token_out") or "").lower() or None
        events = self.ctx.fetch(token, manager, ["RedemptionRequested"])
        rows = [
            self._request_row(token, manager, ev, "request_redemption", ev.user, ev.cashAmountIn, collateral)
            for ev in events.itertuples(index=False)
        ]
        epochs = sorted({int(e) for e in events["epoch"]}) if not events.empty else []
        checks = {"address": manager.address, "events": events["event"].value_counts().to_dict(), "epochs": len(epochs)}
        return rows, checks

    def _wallet_transfers(self, token: TokenConfig, token_contract: ContractConfig) -> tuple[list[dict], dict]:
        wallet = token_contract.params["redemption_wallet"].lower()
        transfers = self.ctx.fetch(token, token_contract, ["Transfer"], where={"to": wallet})
        mints = transfers["from"] == ZERO
        # the row's contract is the wallet the tokens went to (its price sources default to the token's)
        via = token_contract.model_copy(update={"address": wallet, "name": "redemption_wallet"})
        rows = [
            self._request_row(token, via, ev, "transfer_to_redemption_wallet", ev["from"], ev["value"], None)
            for _, ev in transfers[~mints].iterrows()
        ]
        checks = {
            "address": wallet,
            "transfers_in": int((~mints).sum()),
            "token_in": sum(self.scale(v, token.decimals) for v in transfers.loc[~mints, "value"]),
            "senders": int(transfers.loc[~mints, "from"].nunique()),
            "excluded_mints": int(mints.sum()),
        }
        return rows, checks
