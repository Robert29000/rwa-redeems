"""Superstate (USTB, USCC, ...).

Token (v4+) emits `OffchainRedeem(burner, src, amount)` for both redemption paths a holder has:
`offchainRedeem(amount)` and transferring tokens to the token contract itself. These are *requests* settled off-chain.

Instant redemption (USTB): the holder calls a redemption contract, which pulls the tokens, pays USDC and calls
`offchainRedeem` itself, so the token event has burner == src == redemption contract. The redemption contract's own
`Redeem` / `RedeemV2` event (same tx) carries the real holder, recipient and USDC amounts; we join on
(tx_hash, amount) and cross-check counts both ways.

Config roles: `token` (one, address defaults to the token) and `redemption` (zero or more redemption contracts,
`params.token_out` = payout asset address).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from rwa_redeems.config import TokenConfig
from rwa_redeems.protocols.base import CollectResult, ProtocolAdapter, register

ZERO = "0x" + "0" * 40


@register("superstate")
class SuperstateAdapter(ProtocolAdapter):
    TOKEN_EVENT = "OffchainRedeem"
    SETTER_EVENT = "SetRedemptionContract"
    CONTRACT_EVENTS = {"Redeem": "redeem_v1", "RedeemV2": "redeem_v2"}

    def collect(self, token: TokenConfig) -> CollectResult:
        ctx = self.ctx
        token_contract = token.require_contract("token")
        redeems = ctx.fetch(token, token_contract, [self.TOKEN_EVENT])
        setters = ctx.fetch(token, token_contract, [self.SETTER_EVENT])
        redemption_contracts = {c.address: c for c in token.contracts_for("redemption")}
        contract_events = self._contract_events(token)

        # pair each instant token event with its redemption-contract event: same tx, same amount (k-th with k-th)
        redeems["_amount"] = redeems["amount"].map(str)
        redeems["_k"] = redeems.groupby(["tx_hash", "_amount"]).cumcount()
        contract_events["_k"] = contract_events.groupby(["tx_hash", "_amount"]).cumcount()
        is_instant = redeems["burner"].isin(redemption_contracts)
        matched = redeems[is_instant].merge(
            contract_events, on=["tx_hash", "_amount", "_k"], how="left", suffixes=("", "_c")
        )
        matched_by_key = {(r.tx_hash, r.log_index): r for r in matched.itertuples(index=False)}

        rows = []
        for ev in redeems.itertuples(index=False):
            row = self.base_row(token, ev) | {
                "contract": token.address,
                "contract_name": token_contract.name or "token",
                "redemption_type": "request",
                "method": "offchain_redeem",
                "caller": ev.burner,
                "holder": ev.src,
                "token_amount": self.scale(ev.amount, token.decimals),
                "token_amount_raw": str(ev.amount),
            }
            if ev.burner in redemption_contracts:
                row |= self._instant_fields(redemption_contracts[ev.burner], matched_by_key[(ev.tx_hash, ev.log_index)])
            rows.append(row)

        n_instant = int(is_instant.sum())
        unmatched_token = int(matched["method"].isna().sum()) if len(matched) else 0
        checks: dict[str, Any] = {
            "offchain_redeem_events": len(redeems),
            "instant": n_instant,
            "request": len(redeems) - n_instant,
            "contract_events": contract_events["method"].value_counts().to_dict(),
            "instant_without_contract_event": unmatched_token,
            "contract_events_without_token_event": len(contract_events) - (n_instant - unmatched_token),
            "set_redemption_contract": [
                {"block": int(s.block_number), "old": s.oldRedemptionContract, "new": s.newRedemptionContract}
                for s in setters.itertuples(index=False)
            ],
            "unconfigured_redemption_contracts": sorted(
                {a for a in setters.get("newRedemptionContract", []) if a != ZERO} - set(redemption_contracts)
            ),
        }
        return CollectResult(pd.DataFrame(rows), checks)

    def _contract_events(self, token: TokenConfig) -> pd.DataFrame:
        """Normalise Redeem / RedeemV2 of all redemption contracts into one frame."""
        frames = []
        for c in token.contracts_for("redemption"):
            ev = self.ctx.fetch(token, c, list(self.CONTRACT_EVENTS))
            if ev.empty:
                continue
            # the fetcher returns the union of both events' args; Redeem (v1) has no recipient and no fee split
            v2 = ev["event"] == "RedeemV2"
            frames.append(
                pd.DataFrame(
                    {
                        "tx_hash": ev["tx_hash"],
                        "_amount": ev["superstateTokenInAmount"].map(str),
                        "method": ev["event"].map(self.CONTRACT_EVENTS),
                        "redeemer": ev["redeemer"],
                        "to": ev["to"].where(v2, ev["redeemer"]),
                        "usdc_after": ev["usdcOutAmountAfterFee"].where(v2, ev["usdcOutAmount"]),
                        "usdc_before": ev["usdcOutAmountBeforeFee"].where(v2, ev["usdcOutAmount"]),
                    }
                )
            )
        if not frames:
            return pd.DataFrame(columns=["tx_hash", "_amount", "method", "redeemer", "to", "usdc_after", "usdc_before"])
        return pd.concat(frames, ignore_index=True)

    def _instant_fields(self, contract, m) -> dict[str, Any]:
        out: dict[str, Any] = {
            "redemption_type": "instant",
            "contract": contract.address,
            "contract_name": contract.name or "redemption",
        }
        if pd.isna(m.method):  # token event without a matching contract event: keep, holder unknown
            out |= {"method": "offchain_redeem", "holder": None}
            return out
        token_out = contract.params.get("token_out")
        asset = self.ctx.asset(token_out)
        if asset is None:
            raise ValueError(f"redemption contract {contract.address}: params.token_out must be a configured chain asset")
        received = self.scale(m.usdc_after, asset.decimals)
        out |= {
            "method": m.method,
            "holder": m.redeemer,
            "recipient": m.to,
            "token_out": token_out.lower(),
            "token_out_symbol": asset.symbol,
            "token_out_amount": received,
            "token_out_fee_amount": self.scale(int(m.usdc_before) - int(m.usdc_after), asset.decimals),
            "token_out_usd": received * asset.usd_price if asset.usd_price is not None else None,
        }
        return out
