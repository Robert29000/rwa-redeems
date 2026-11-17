"""Ondo USDY.

Two redemption paths are collected, each with exactly one row source:

- instant (`instant_managers`, xManager `USDY_InstantManager`): `redeem(rwaAmount, receivingToken, minReceived)` burns
  the USDY and pays the stablecoin in the same tx. Row source: `Redemption(redeemer, redeemerId, rwaAmount,
  receivingToken, receiveTokenAmount, redemptionUSDValue, fee)`; `fee` and `redemptionUSDValue` are USD with
  18 decimals. `redeemRebasingUSDY` (rUSDY in) emits the same `Redemption` plus `InstantRedemptionRebasingUSDY`
  in the same tx; those rows get method `redeem_rebasing_usdy` and `token_amount` in USDY (the rUSDY amount is in
  the manifest checks).

rUSDY (rebasing wrapper) needs no separate logs: it has no redemption function of its own. `unwrap` converts rUSDY
back to USDY for the holder (a conversion; a later redemption of that USDY is a normal `redeem` row), and the admin
`burnShares` has never been used on Ethereum. The only rUSDY redemption path is `redeemRebasingUSDY` above.
- request (`request_managers`, legacy RWAHub `USDYManager`): `requestRedemptionServicedOffchain` burns the USDY at once
  and Ondo pays USD off-chain → `RedemptionRequestedServicedOffChain(user, redemptionId, amount, destination)`.
  `requestRedemption` (claim USDC on-chain later) → `RedemptionRequested(user, redemptionId, amount)`.

Not collected, on purpose:
- Holders also transfer USDY to Ondo-controlled Safes, which burn it in batches. Ondo does not document this path,
  so those transfers are not rows. Request data for USDY is therefore NOT complete (see docs/notes/usdy-redemptions.md).
- Bridge burns (LayerZero OFT adapter, Axelar SourceBridge) are not redemptions.

Config roles: `instant_managers` and `request_managers` (one or more contracts each).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from rwa_redeems.config import ContractConfig, TokenConfig
from rwa_redeems.protocols.base import CollectResult, ProtocolAdapter, register

FIAT_TOKEN = "0x" + "0" * 40
USD_DECIMALS = 18

NOT_COLLECTED = (
    "USDY transfers to Ondo-controlled Safes that are later burned (undocumented path); request rows are incomplete"
)


@register("ondo")
class OndoAdapter(ProtocolAdapter):
    INSTANT_EVENT = "Redemption"
    REBASING_EVENT = "InstantRedemptionRebasingUSDY"
    REQUEST_EVENTS = {
        "RedemptionRequestedServicedOffChain": "request_redemption_serviced_offchain",
        "RedemptionRequested": "request_redemption",
    }

    def collect(self, token: TokenConfig) -> CollectResult:
        instant_managers = token.contracts_for("instant_managers")
        request_managers = token.contracts_for("request_managers")
        if not instant_managers and not request_managers:
            raise ValueError(f"{token.symbol}: ondo token needs 'instant_managers' and/or 'request_managers'")
        rows: list[dict[str, Any]] = []
        checks: dict[str, Any] = {"not_collected": NOT_COLLECTED, "instant_managers": {}, "request_managers": {}}
        for manager in instant_managers:
            manager_rows, checks["instant_managers"][manager.name or manager.address] = self._instant(token, manager)
            rows += manager_rows
        for manager in request_managers:
            manager_rows, checks["request_managers"][manager.name or manager.address] = self._requests(token, manager)
            rows += manager_rows
        return CollectResult(pd.DataFrame(rows), checks)

    def _instant(self, token: TokenConfig, manager: ContractConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        events = self.ctx.fetch(token, manager, [self.INSTANT_EVENT, self.REBASING_EVENT])
        redemptions = events[events["event"] == self.INSTANT_EVENT]
        rebasing = events[events["event"] == self.REBASING_EVENT]
        rebasing_keys = {(ev.tx_hash, int(ev.usdyAmountIn)) for ev in rebasing.itertuples(index=False)}

        rows = []
        for ev in redemptions.itertuples(index=False):
            asset = self.ctx.asset(ev.receivingToken)
            if asset is None or asset.usd_price is None:
                raise ValueError(f"{manager.address}: payout token {ev.receivingToken} must be a configured chain asset")
            rebased = (ev.tx_hash, int(ev.rwaAmount)) in rebasing_keys
            received = self.scale(ev.receiveTokenAmount, asset.decimals)
            fee_usd = self.scale(ev.fee, USD_DECIMALS)
            rows.append(
                self.base_row(token, ev)
                | {
                    "contract": manager.address,
                    "contract_name": manager.name,
                    "redemption_type": "instant",
                    "method": "redeem_rebasing_usdy" if rebased else "redeem",
                    "caller": ev.redeemer,
                    "holder": ev.redeemer,
                    "recipient": ev.redeemer,  # the manager pays msg.sender
                    "token_amount": self.scale(ev.rwaAmount, token.decimals),
                    "token_amount_raw": str(ev.rwaAmount),
                    "fee_token_amount": 0.0,  # the fee is charged in USD on the payout side
                    "token_out": ev.receivingToken,
                    "token_out_symbol": asset.symbol,
                    "token_out_amount": received,
                    "token_out_fee_amount": fee_usd / asset.usd_price,
                    "token_out_usd": received * asset.usd_price,
                }
            )
        matched = sum(1 for ev in redemptions.itertuples(index=False) if (ev.tx_hash, int(ev.rwaAmount)) in rebasing_keys)
        checks = {
            "address": manager.address,
            "events": events["event"].value_counts().to_dict(),
            # every rUSDY redemption must also have a Redemption event with the same USDY amount
            "rebasing_unmatched": len(rebasing) - matched,
            "rebasing_rusdy_in": sum(self.scale(v, token.decimals) for v in rebasing["rusdyAmountIn"]),
            "rebasing_usdy_in": sum(self.scale(v, token.decimals) for v in rebasing["usdyAmountIn"]),
            "redemption_usd_value": sum(self.scale(v, USD_DECIMALS) for v in redemptions["redemptionUSDValue"]),
            "fees_usd": sum(self.scale(v, USD_DECIMALS) for v in redemptions["fee"]),
        }
        return rows, checks

    def _requests(self, token: TokenConfig, manager: ContractConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        events = self.ctx.fetch(token, manager, list(self.REQUEST_EVENTS))
        rows = []
        for ev in events.itertuples(index=False):
            offchain = ev.event == "RedemptionRequestedServicedOffChain"
            amount = ev.rwaTokenAmountIn if offchain else ev.rwaAmountIn
            rows.append(
                self.base_row(token, ev)
                | {
                    "contract": manager.address,
                    "contract_name": manager.name,
                    "redemption_type": "request",
                    "method": self.REQUEST_EVENTS[ev.event],
                    "caller": ev.user,
                    "holder": ev.user,
                    "request_id": int(ev.redemptionId, 16),
                    "token_amount": self.scale(amount, token.decimals),
                    "token_amount_raw": str(amount),
                    # serviced off-chain = USD paid off-chain; an on-chain request is claimed later in the hub's
                    # collateral, which the event does not name
                    "token_out": FIAT_TOKEN if offchain else None,
                    "token_out_symbol": "FIAT" if offchain else None,
                }
            )
        checks = {"address": manager.address, "events": events["event"].value_counts().to_dict()}
        return rows, checks
