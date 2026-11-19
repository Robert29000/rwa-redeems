# JAAA redemptions: how the collector works and how to keep it current

Notes for humans and agents who work on the Centrifuge JAAA integration.
Facts were checked on-chain on 2026-11-19 (data up to block 26,449,658) unless marked **[doc]**.

- Code: `src/rwa_redeems/protocols/centrifuge.py` (the module docstring is the short version)
- Config: `config/tokens.yaml` (JAAA entry); AUSD and USAT in `config/chains.yaml`
- ABIs: `abis/centrifuge/` (`AsyncVault` = v3 vault, `ShareToken` = the JAAA `Tranche` contract)
- Tests: `tests/test_centrifuge.py`, `tests/test_pricing.py::test_rpc_call_source_timestamp_fn` (real mainnet logs in `tests/fixtures/logs.jsonl`)
- Run: `uv run rwa-redeems collect --tokens JAAA --output-dir <dir>` (use a separate dir: a single-token run overwrites `redemptions.csv` with that token only)

Sources: GitHub `centrifuge/protocol` (`src/vaults/`, `env/spell/*ethereum*`), verified sources on Etherscan, and
Centrifuge's indexer API (`api.centrifuge.io`, code in `centrifuge/api-v3`).
docs.centrifuge.io (incl. <https://docs.centrifuge.io/developer/protocol/deployments/#jaaa-1>) refuses automated
fetches (HTTP 403), so the addresses were not compared with that page word for word. The indexer API, the spells in
the protocol repo and the share token's own `VaultUpdate` events all give the same vaults.

## 1. Token facts

| Item | Value |
|---|---|
| Token | JAAA, Janus Henderson Anemoy AAA CLO Fund (Centrifuge pool) |
| Address (Ethereum) | `0x5a0f93d040de44e78f251b03c43be9cf317dcf64` (deploy block 22,297,839 = `start_block`) |
| Contract | Centrifuge v2 `Tranche`, reused by v3 (spell 001 linked it to v3) |
| Decimals | 6 |
| Pool / share class | `281474976710663` / `0x00010000000000070000000000000001` (hub on Ethereum) |
| Value accrual | The price goes up (1.00 USDC at launch, 1.057 USDC on 2026-11-19) |

## 2. Vaults

Each vault is an ERC-7540 vault for one payout asset. The share token logs `VaultUpdate(asset, vault)` at each link
and unlink (13 events). The adapter reads them and reports linked vaults that are not in the config
(`checks.unconfigured_vaults`, empty now).

| Name in config | Address | Asset | Contract | Deployed | Requests | JAAA | USD |
|---|---|---|---|---|---|---|---|
| `usdc_v2` | `0xe9d1f733f406d4bbbdfac6d4cfcd2e13a6ee1d01` | USDC | v2 `ERC7540Vault` | 22,297,843 | 2 | 4,595,987 | 4.60M |
| `usdc` | `0x4880799ee5200fc58da299e965df644fbf46780b` | USDC | v3 `AsyncVault` | 22,982,600 | 83 | 670,507,766 | 687.64M |
| `usdt` | `0x4a76fb02421b1d38aaa40db541f59b2cb04b0770` | USDT | v3 `AsyncVault` | 25,350,680 | 8 | 9,423,867 | 9.88M |
| `ausd` | `0x3148910f61c761a8ea939c5d6ab88e5265b71106` | AUSD | v3 `AsyncVault` | 25,351,412 | 0 | | |
| `usat` | `0x13470dbaaafddbf1a0d9048ddcafd6dd82e14372` | USAT | v3 `AsyncVault` | 25,839,861 | 0 | | |

- The USDC link moved v2 → v3 → v2 → v3: v2 at 22,297,843; v3 at 22,982,600; v2 again at 23,015,872 (spell 002 "RelinkV2"); v3 from 23,382,941. Between 24,349,719 and 24,385,775 (v3.0 → v3.1 upgrade) no USDC vault was linked.
- The `usdc` vault kept its address through the v3.0 → v3.1 upgrade. Only its manager changed (`File` at 24,376,326: AsyncRequestManager `0xf06f89a1…` → `0xf48256ab…`).
- The v2 vault ABI names the last `RedeemRequest` field `assets`. The types are the same, so the topic0 is the same. The value is the share amount: both v2 requests move exactly that JAAA amount to the v2 escrow `0x0000000005f458fd…` in the same tx. One ABI (`centrifuge/AsyncVault`) reads all five vaults.
- deJAAA (pool `281474976710659`, token `0xaaa0008c…`) is a different token with its own vaults. It is not part of JAAA.

## 3. How holders redeem

Claims checked against the contract source (`src/vaults/BaseVaults.sol`, `AsyncRequestManager.sol`, `SyncManager.sol`, `VaultRouter.sol`):

| Claim | Result |
|---|---|
| JAAA uses an async redeem vault; the holder calls `requestRedeem` to start a redemption | **True.** `requestRedeem(shares, controller, owner)` on the vault. All JAAA vaults are `AsyncVault` (async deposit + async redeem). |
| There are other methods to start a redemption | **Through the vault only.** `VaultRouter.requestRedeem` (`0xf684014771c01e50b8b526968b3a1e33acda63f6`) calls the vault's `requestRedeem`, so the vault still emits `RedeemRequest` with `sender` = router (48 of 93 rows). An operator (`setOperator` / `setEndorsedOperator`) or an address with a share allowance can also call it for the owner. A pool manager can also burn a holder's shares outside the vaults (§6); this is not a holder function. |
| `redeem` and `withdraw` only claim | **True.** Both pay out an already fulfilled request (`maxWithdraw` / `maxRedeem` of the controller) and emit the ERC-4626 `Withdraw(sender, receiver, owner = controller, assets, shares)`. The code recommends `withdraw`, because `redeem` can leave dust. `previewRedeem` and `previewWithdraw` revert. |
| The protocol has no instant redemption | **True.** `SyncManager` has deposit/mint only. `SyncDepositVault` is sync deposit + async redeem. Redemption is always async, in every vault type. |
| Each request emits `RedeemRequest(controller, owner, REQUEST_ID, msg.sender, shares)` | **True.** Emitted by the vault (`BaseAsyncRedeemVault.requestRedeem`). `REQUEST_ID` is always 0. The same `emit` exists in the auth hook `onRedeemRequest` (v2: manager-triggered redeem), but nothing calls it in v3. If it is called, the row has `sender` = the manager. |

Lifecycle (on the spoke chain, here Ethereum):

```
holder ──requestRedeem──► vault: JAAA → pool escrow             event RedeemRequest            (row)
hub epoch: approve + revoke ─► BalanceSheet burns the epoch's shares (one burn for many holders)
                          ─► vault                               event RedeemClaimable(assets, shares)
holder ──withdraw / redeem──► vault: payout asset to receiver    event Withdraw                 (claim)
```

Only `RedeemRequest` makes rows (`redemption_type = request`, `method = request_redeem`). The other events are
counted per vault in `checks.vaults`. Counts: `RedeemClaimable` 2 / 85 / 7 and `Withdraw` 2 / 87 / 7 (usdc_v2 / usdc
/ usdt). A request can be filled in parts and claimed in parts, so the counts differ from the request counts.

Row columns: `holder` = `owner` (the shares came from this address), `caller` = `sender`, `request_id` = 0,
`token_out` = the vault asset. `controller` (the address that claims later) is equal to `owner` in every row so far
(`checks.vaults.*.controller_not_owner` = 0). `recipient` is empty: the receiver is known only at the claim.

## 4. Cancellations

`cancelRedeemRequest(0, controller)` → `CancelRedeemRequest`; the hub confirms → `CancelRedeemClaimable`;
`claimCancelRedeemRequest` → `CancelRedeemClaim` (the shares go back). The hub can also force a cancellation, which
emits only `CancelRedeemClaimable`.

**No JAAA vault has ever emitted one of these events** (`checks.cancellations`, all 0). The collector does not reduce
request rows for cancellations. If cancellations start, a cancelled request still shows its full amount in
`redemptions.csv`; the manifest counts show it.

## 5. Price

Each row is valued with `pricePerShare()` of the vault that got the request, as an archive `eth_call` at the event
block. `priceLastUpdated()` at the same block fills `price_timestamp` (`rpc_call` with `timestamp_fn`).

- `pricePerShare()` = `convertToAssets(10**6)`: payout asset per 1 JAAA, in **asset decimals** (6 for all five assets), from the last share and asset prices that the hub pushed. The prices do not expire.
- The asset is valued at 1.0 USD (USDC, USDT, AUSD, USAT are fixed 1.0 in `chains.yaml`), so `price_usd` = `pricePerShare / 1e6`.
- All 93 rows have a price from their own vault. No backfill.
- Age of the price at the request (`timestamp - price_timestamp`): median 33 h, max 42 days (the first v2 request, 2025-06-25, price from 2025-05-14).
- Check against the paid price. Every request was filled (claimable shares = requested shares in each vault). Paid price = Σ `RedeemClaimable.assets` / Σ `shares`:

| Vault | Request-time value / JAAA | Paid / JAAA | Difference |
|---|---|---|---|
| `usdc_v2` | 1.000873 | 1.000997 | −0.012% |
| `usdc` | 1.025547 | 1.025483 | +0.006% |
| `usdt` | 1.048653 | 1.048972 | −0.030% |

- Three requests by `0x491edfb0…` are worth exactly 50,000,000.00 USD at the request-block price. The holder sized them with the same on-chain price.

## 6. Reconciliation: all JAAA burns

All 102 burns (`Transfer` to `0x0`) since deploy:

| Burn source | Burns | JAAA | Meaning |
|---|---|---|---|
| BalanceSheet v3.1 `0x12a110ce…`, shares from AsyncRequestManager `0xf48256ab…` | 74 | 385,285,515 | Epoch revokes of vault requests |
| BalanceSheet v3.0 `0xbcc8d02d…`, shares from AsyncRequestManager `0xf06f89a1…` | 7 | 294,646,118 | Epoch revokes of vault requests |
| v2 escrow `0x0000000005f458fd…` | 2 | 4,595,987 | The 2 v2 requests |
| BalanceSheet v3.1, shares from manager EOA `0x42b71ced…` | 6 | 160,691,460 | **Manual revokes, not collected** (below) |
| Spoke v3.0 `0xd30da1d7…` / v3.1 `0xec3582fc…` (`InitiateTransferShares`) | 5 + 7 | 250,456,845 | Crosschain share transfers to other Centrifuge chains: not redemptions |
| Spell 002 `0x5a110bc4…` | 1 | 50,000,000 | V2 relink migration: not a redemption |

The vault revokes (679,931,633 JAAA) are equal to the v3 vault requests (670,507,766 + 9,423,867). The v2 burns are
equal to the v2 requests. So every request is in the rows, and every vault burn has a request.

Manual revokes: in each of the 6 txs, the holder sends JAAA to the manager EOA `0x42b71ced90c43c7ab3cfc5e975e9502884c2208f`,
which sends it to the BalanceSheet, which burns it. There is no vault event.

| Holder | Burns | JAAA | Blocks |
|---|---|---|---|
| `0x227942bd9c3e4eca1b76e8199e407e6c52fdacd6` (Safe; also 12 vault requests) | 4 | 8,374,291 | 24,442,660 – 25,280,196 |
| `0x5d2c428e04f3a10b963f5c8f216071ddcdbdd03b` (got 231M JAAA from 4 issues) | 2 | 152,317,169 | 26,033,982 – 26,041,623 |

These are very likely redemptions settled off-chain by the pool manager (about 170M USD). Centrifuge does not
document this path, so the collector does not make rows for it. The manifest repeats the gap in `checks.not_collected`.
The Centrifuge indexer marks these revokes `isManual`.

## 7. Keeping the integration current (checklist)

1. **New vault.** `checks.unconfigured_vaults` lists any vault linked by `VaultUpdate` that is not in the config. Add it under `vaults` with `params.token_out` and its own `pricePerShare` price source. If the asset is new, add it to `chains.yaml`.
2. **Cancellations.** `checks.cancellations`. If they are not 0, decide whether to subtract cancelled shares.
3. **Completeness.** Repeat the burn reconciliation (§6). A burn group without vault requests is a new path.
4. **Protocol upgrade.** Registry v3.3 has new Spoke / AsyncRequestManager addresses; pools migrate one at a time. If JAAA gets new vaults, they show up in `VaultUpdate`.

## 8. Out of scope (on purpose)

- Manual BalanceSheet revokes (§6), until Centrifuge documents them as redemptions.
- Claims and payouts (`RedeemClaimable`, `Withdraw`): counted only.
- Crosschain share transfers.
- JAAA on other chains (Base, Arbitrum, Avalanche, BNB, Plume, ...).
