# OUSG redemptions: how the collector works and how to keep it current

Notes for humans and agents who work on the Ondo OUSG integration.
Facts were checked on-chain on 2026-11-18 (data up to block 26,438,072) unless marked **[doc]**.

- Code: `src/rwa_redeems/protocols/ondo.py` (shared with USDY; the module docstring is the short version)
- Config: `config/tokens.yaml` (OUSG entry)
- ABIs: `abis/ondo/` (OUSG_InstantManager, OUSGInstantManager, OUSGManager, CashManager, OUSG, OUSGOracle)
- Tests: `tests/test_ondo.py` (real mainnet logs in `tests/fixtures/logs.jsonl`)
- Run: `uv run rwa-redeems collect --tokens OUSG --output-dir <dir>` (use a separate dir: a single-token run overwrites `redemptions.csv` with that token only)

## 1. Token facts

| Item | Value |
|---|---|
| Token | OUSG, Ondo Short-Term US Government Treasuries (qualified-access product **[doc]**) |
| Address (Ethereum) | `0x1b19c19393e2d034d8ff31ff34c81252fcbbee92` (TokenProxy, deploy block 16,234,210 = `start_block`) |
| Decimals | 18 |
| Value accrual | The price goes up (about 100 USD at launch, 117.30 USD now). rOUSG `0x54043c656f0fad0652d9ae2603cdf347c5578d00` is the rebasing wrapper. |
| Rules **[doc]** | Instant: minimum 5k USD, 50M USD per 24 h globally, 25M USD per investor. Non-instant: minimum 50k USD, paid in USDC the next business day if sent before 4 pm ET. |

Sources: <https://docs.ondo.finance/addresses.md>, <https://docs.ondo.finance/qualified-access-products/ousg/redeeming.md>, <https://docs.ondo.finance/qualified-access-products/ousg/instant-limits.md>, GitHub `ondoprotocol/rwa-contracts`, verified sources on Etherscan.

## 2. Collected redemption paths

| Type | Config name | Contract | Period | Row source (`method`) | Rows | OUSG | USD |
|---|---|---|---|---|---|---|---|
| `instant` | `instant_manager` | `OUSG_InstantManager` `0x93358db73b6cd4b98d89c8f5f230e81a95c2643a` (xManager) | 2025-04-03 → now | `Redemption` (`redeem`); with `InstantRedemptionRebasingOUSG` in the same tx (`redeem_rebasing_ousg`) | 312 + 2 | 4,640,804 | 531.0M |
| `instant` | `instant_manager_legacy` | `OUSGInstantManager` `0x2826989983e3a66f0622132d019c2ae173eb6a43` (docs: deprecated 2025-04-07) | 2024-04-24 → 2025-04-04 | `InstantRedemptionOUSG` (`redeem`), `InstantRedemptionRebasingOUSG` (`redeem_rebasing_ousg`) | 192 + 11 | 2,098,685 | 228.6M |
| `instant` | `instant_manager_v0` | First `OUSGInstantManager` `0x1189bacb100676db9a495eec04a730a53a8ad97c` (**not in Ondo's docs**) | 2024-04-20 → 2024-04-22 | same as legacy | 2 + 1 | 5,667 | 0.6M |
| `request` | `cash_manager` | `CashManager` `0x3501883a646f1f8417bcb62162372550954d618f` (docs: deprecated 2023-12-13) | 2023-02-23 → 2023-12-13 | `RedemptionRequested(user, cashAmountIn, epoch)` (`request_redemption`) | 100 | 1,546,223 | 157.9M |
| `request` | `cash_manager_v0` | Original `CashManager` `0x70b45a68ca257db49dec455af6c725f7f1c904e8` (**not in Ondo's docs**) | 2023-01-26 | same | 1 | 0.15 | unpriced |
| `request` | `ousg_manager` | `OUSGManager` `0xf16c188c2d411627d39655a60409ec6707d3d5e8` (RWAHub; docs: deprecated 2024-04-15) | 2023-12-13 → 2025-03-30 | `RedemptionRequested(user, redemptionId, rwaAmountIn)` (`request_redemption`) | 80 | 820,896 | 86.8M |
| `request` | `redemption_wallet` | "OUSG Recipient" `0x72be8c14b7564f7a61ba2f6b7e50d18dc1d4b63d` (docs: "for manual redemptions") | 2025-03-28 → now | token `Transfer(from, to = recipient)`, `from ≠ 0x0` (`transfer_to_redemption_wallet`) | 41 | 5,989,395 | 683.6M |

Instant details:
- All managers burn the OUSG and pay USDC in the same tx.
- xManager `Redemption`: `fee` and `redemptionUSDValue` are USD with 18 decimals.
- Legacy/v0: `usdcAmountOut` is after the fee. The fee comes from `RedeemFeesDeducted(sender, feeReceiver, usdcFees, usdcAmountOut)` in the same tx, joined on (tx, sender, usdcAmountOut). All fees so far are 0.
- Legacy/v0 managers redeem BUIDL for the USDC unless they hold sufficient USDC (`BUIDLRedemptionSkipped`). Those BUIDL redemptions are also BUIDL rows (double counting across tokens). The collector does not flag them.

Request details:
- `OUSGManager` and `CashManager` burn the OUSG when the request is made. USDC is paid later (`collateral()` = USDC).
- `CashManager` has no request id, only an `epoch`, so `request_id` is empty for its rows.
- The OUSG Recipient is a Gnosis Safe. Holders transfer OUSG to it, and it burns the OUSG later. The collector reads the transfers at the source with an `eth_getLogs` filter on the indexed `to` (topic2). Mints to the wallet (from `0x0`) are dropped; there are none so far. All 5,989,395 OUSG received have been burned (36 burns).

## 3. rOUSG

rOUSG needs no separate logs. Its only redemption path is the managers' `redeemRebasingOUSG`. The manager pulls the rOUSG, unwraps it, burns the OUSG and pays USDC. The OUSG amount is the row's `token_amount`. The rOUSG amount is in the manifest (`rebasing_wrapper_in`, xManager only).

| rOUSG function | Rows |
|---|---|
| Manager `redeemRebasingOUSG` | Yes: 14 in total (1 v0, 11 legacy, 2 current) |
| `unwrap` / `unwrapShares` (rOUSG → OUSG to the holder) | No: a conversion (7 so far) |
| `burnShares` (admin `BURNER_ROLE`, held by `0xaed4…`) | No: never used |

## 4. Reconciliation: all OUSG burns

All 745 OUSG burns since deploy (15,322,585 OUSG):

| Group | Burns | OUSG | Collected as |
|---|---|---|---|
| `OUSG_InstantManager` | 314 | 4,640,804 | `instant` |
| Legacy `OUSGInstantManager` | 203 | 2,098,685 | `instant` |
| First `OUSGInstantManager` (v0) | 3 | 5,667 | `instant` |
| `CashManager` | 100 | 1,546,223 | `request` |
| `OUSGManager` | 80 | 820,896 | `request` |
| Original `CashManager` (v0) | 1 | 0.15 | `request` |
| OUSG Recipient burns its received OUSG | 36 | 5,989,395 | `request` (rows are the 41 transfers in) |
| Admin Safe `0xaed4caf2e535d964165b4392342f71bac77e8367` burns its received OUSG | 6 | 220,906 | **Not collected** (see below) |
| Ondo Safe `0x3d85c41e…`: dust `burn` and a Flux fOUSG withdraw + burn | 2 | 6.95 | No: not redemptions |

About `0xaed4…`:
- It is Ondo's OUSG admin Safe: `DEFAULT_ADMIN` of the token, rOUSG and the legacy managers.
- It received OUSG 6 times (5 investors, 1 Ondo test), and each time it burned the OUSG 1–8 days later. Two of the senders were paid in USDC by `0x72be…` some days later.
- Its last inflow was at block 22,009,462. The OUSG Recipient `0x72be…` received its first OUSG at 22,147,005.
- Thus it was very likely the manual-redemption wallet before `0x72be…`. But Ondo does not document it, so it is not collected. The manifest repeats this gap in `checks.not_collected`.
- The largest transfer was 194,036 OUSG (about 21M USD) at block 20,874,975.

## 5. Price

| Source | Address | From block | Notes |
|---|---|---|---|
| `ousg_rwa_oracle` (`rwaPrice()`, int256, 18 dp) | `0x0502c5ae08e7cd64fe1aeda7d6e229413ecc6abe` | 17,131,761 | `RWAOracleExternalComparisonCheck` (docs: "Oracle (Legacy)"). It is still the source behind `OUSGOracleWrapper` `0xadc4966e…`, `OndoOracle.getAssetPrice(OUSG)` and the legacy managers' `getOUSGPrice()`. Updated daily. |
| `ousg_cash_manager_rate` (`lastSetMintExchangeRate()`) | `CashManager` `0x3501…` | 16,735,625 | Before that block it returns the initial 100.0, so it is not used before. Agrees with the oracles at 17.2M. |

- The price check for every instant manager has a median difference of about 1e-12.
- One `cash_manager` request (block 16,693,119) is earlier than both sources. It gets the next CashManager rate (`price_is_backfilled`).
- The `cash_manager_v0` request (0.15 OUSG, block 16,493,844) has no price: there is no later row from that contract to backfill from.
- Earlier prices exist only with an argument: Flux `0xba9b10f9….getUnderlyingPrice(fOUSG)` from 16,521,307. `rpc_call` does not support arguments.

## 6. Keeping the integration current (checklist)

1. **New manager or recipient.** Look in <https://docs.ondo.finance/addresses.md>. Add it under the role, with `params.kind` for a new event layout.
2. **Completeness.** Repeat the burn reconciliation (§4). Any burn group without a manager event and not from a configured wallet is a new path.
3. **Fees.** `fees_usd` (xManager) and `fees_usdc` (legacy) are 0 so far.
4. **Recipient senders.** `checks.redemption_wallet.senders` and `excluded_mints`. A transfer from an Ondo contract would be internal, not a request; none so far.

## 7. Out of scope (on purpose)

- Transfers to the admin Safe `0xaed4…` (§4), until Ondo confirms them as redemptions.
- rOUSG unwraps; Flux fOUSG.
- Request payouts (USDC paid later; `RedemptionCompleted` events).
- OUSG on Polygon, Solana and XRP Ledger.
