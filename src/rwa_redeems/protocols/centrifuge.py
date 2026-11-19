"""Centrifuge share tokens (JAAA, ...).

Holders redeem only through ERC-7540 vaults (one vault per share token and payout asset). Redemption is always
asynchronous; the protocol has no instant redeem (SyncDepositVault is sync deposit + async redeem).
- `requestRedeem(shares, controller, owner)` moves the shares to the pool escrow and makes the vault emit
  `RedeemRequest(controller, owner, requestId = 0, sender, shares)`. One row per event, type `request`.
- The hub approves the request later; the shares are burned per epoch (batched, not per holder) and the vault emits
  `RedeemClaimable`. `withdraw` / `redeem` only claim the payout of a fulfilled request (`Withdraw` event).
- `cancelRedeemRequest` / `claimCancelRedeemRequest` (`CancelRedeemRequest`, `CancelRedeemClaimable`,
  `CancelRedeemClaim`) take a pending request back. Cancellations are counted in the checks only; the request
  rows are not reduced.
The legacy v2 vault (ERC7540Vault) has the same event signatures (its ABI names the last RedeemRequest field
`assets`, but the value is the share amount), so one ABI reads all vaults.

Each row is valued with the vault's `pricePerShare()` (payout asset per share, asset decimals) at the event block;
this is the vault's per-vault price source in config.

Not collected: manual BalanceSheet revokes (a pool manager pulls a holder's shares and burns them; no vault event)
and crosschain share transfers (burn here, mint on another chain; not redemptions).

Config role: `vaults` (one or more vaults; `params.token_out` = vault asset) and an optional `token` (share token,
reads `VaultUpdate` to report linked vaults that are not configured). `params.not_collected` on any contract is
repeated in the checks.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from rwa_redeems.config import ContractConfig, TokenConfig
from rwa_redeems.protocols.base import CollectResult, ProtocolAdapter, register

ZERO = "0x" + "0" * 40


@register("centrifuge")
class CentrifugeAdapter(ProtocolAdapter):
    REQUEST_EVENT = "RedeemRequest"
    LIFECYCLE_EVENTS = [
        "RedeemClaimable",
        "Withdraw",
        "CancelRedeemRequest",
        "CancelRedeemClaimable",
        "CancelRedeemClaim",
    ]

    def collect(self, token: TokenConfig) -> CollectResult:
        vaults = token.contracts_for("vaults")
        if not vaults:
            raise ValueError(f"{token.symbol}: centrifuge token needs at least one contract under 'vaults'")
        frames, vault_checks = [], {}
        for vault in vaults:
            requests = self.ctx.fetch(token, vault, [self.REQUEST_EVENT])
            lifecycle = self.ctx.fetch(token, vault, self.LIFECYCLE_EVENTS)
            frames.append(pd.DataFrame([self._row(token, vault, ev) for ev in requests.itertuples(index=False)]))
            counts = lifecycle["event"].value_counts()
            vault_checks[vault.name or vault.address] = {
                "address": vault.address,
                "redeem_requests": len(requests),
                **{e: int(counts.get(e, 0)) for e in self.LIFECYCLE_EVENTS},
                "controller_not_owner": int((requests["controller"] != requests["owner"]).sum()),
                "sender_not_owner": int((requests["sender"] != requests["owner"]).sum()),
            }
        non_empty = [f for f in frames if not f.empty]
        df = pd.concat(non_empty, ignore_index=True) if non_empty else pd.DataFrame()
        checks: dict[str, Any] = {
            "vaults": vault_checks,
            "cancellations": {
                e: sum(v[e] for v in vault_checks.values())
                for e in ("CancelRedeemRequest", "CancelRedeemClaimable", "CancelRedeemClaim")
            },
        }
        # known gaps, written in the config as `params.not_collected` on any contract, are repeated in the manifest
        gaps = [c.params["not_collected"] for cs in token.contracts.values() for c in cs if "not_collected" in c.params]
        if gaps:
            checks["not_collected"] = gaps
        checks |= self._linked_vaults(token, vaults)
        return CollectResult(df, checks)

    def _row(self, token: TokenConfig, vault: ContractConfig, ev) -> dict[str, Any]:
        token_out = vault.params.get("token_out")
        asset = self.ctx.asset(token_out)
        if asset is None:
            raise ValueError(f"vault {vault.address}: params.token_out must be a configured chain asset")
        return self.base_row(token, ev) | {
            "contract": vault.address,
            "contract_name": vault.name,
            "redemption_type": "request",
            "method": "request_redeem",
            "caller": ev.sender,
            "holder": ev.owner,
            "request_id": int(ev.requestId),
            "token_amount": self.scale(ev.shares, token.decimals),
            "token_amount_raw": str(ev.shares),
            "token_out": token_out.lower(),
            "token_out_symbol": asset.symbol,
        }

    def _linked_vaults(self, token: TokenConfig, vaults: list[ContractConfig]) -> dict[str, Any]:
        """Every vault the share token was ever linked to (`VaultUpdate`), and those missing from the config."""
        if "token" not in token.contracts:
            return {}
        updates = self.ctx.fetch(token, token.require_contract("token"), ["VaultUpdate"])
        linked = [
            {"block": int(u.block_number), "asset": u.asset, "vault": u.vault} for u in updates.itertuples(index=False)
        ]
        configured = {v.address for v in vaults}
        return {
            "vault_updates": linked,
            "unconfigured_vaults": sorted({u["vault"] for u in linked if u["vault"] != ZERO} - configured),
        }
