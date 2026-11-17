# USDY redemptions: how the collector works and what it does not cover

Notes for humans and agents who work on the Ondo USDY integration.
Facts were checked on-chain on 2026-11-18 (data up to block 26,437,822) unless marked **[doc]**.

- Code: `src/rwa_redeems/protocols/ondo.py` (the module docstring is the short version of this file)
- Config: `config/tokens.yaml` (USDY entry)
- ABIs: `abis/ondo/` (USDY_InstantManager, USDYManager, USDYOracle, USDYPricer)
- Tests: `tests/test_ondo.py` (real mainnet logs in `tests/fixtures/logs.jsonl`)
- Run: `uv run rwa-redeems collect --tokens USDY --output-dir <dir>` (use a separate dir: a single-token run overwrites `redemptions.csv` with that token only)

## Important: USDY request data is not complete

The collector reads only documented redemption contracts.
Holders also send USDY to Ondo-controlled Gnosis Safes, which burn it later in batches.
Ondo does not document this path, and the Safes are not in Ondo's address list, so the collector does not count these transfers as redemptions.
This path is large: it is about 84% of all USDY burned on Ethereum (§4).
Thus the `request` rows for USDY show only the legacy `USDYManager` requests, not all request-type redemptions.
Do not compare USDY request totals with other tokens without this note.

## 1. Token facts

| Item | Value |
|---|---|
| Token | USDY, Ondo US Dollar Yield. Issuer: Ondo Global Markets (BVI) Limited **[doc]** |
| Address (Ethereum) | `0x96f6ef951840721adbf46ac996b59e0235cb985c` (TokenProxy, deploy block 17,672,244 = `start_block`) |
| Decimals | 18 |
| Value accrual | The price goes up; the balance stays the same. rUSDY `0xaf37c1167910ebc994e266949387d2c7c326b879` is the rebasing wrapper (1 rUSDY ≈ 1 USD). |
| Eligibility | Non-US persons after KYC; the instant manager also needs the address in `OndoIDRegistry` **[doc]** |

Sources: <https://docs.ondo.finance/addresses.md>, <https://docs.ondo.finance/developer-guides/usdy-instant-manager-integration.md>, GitHub `ondoprotocol/rwa-contracts`.

## 2. Collected redemption paths

| Type in CSV | Contract (config name) | Call | Row source (`method`) | Rows | USDY |
|---|---|---|---|---|---|
| `instant` | `USDY_InstantManager` `0xa42613c243b67bf6194ac327795b926b4b491f15` (`instant_manager`), from 2025-12-15 | `redeem(rwaAmount, receivingToken, minimumTokenReceived)` | `Redemption` (`redeem`) | 904 | 90.56M |
| `instant` | same | `redeemRebasingUSDY(rusdyAmount, receivingToken, minimumTokenReceived)` | `Redemption` + `InstantRedemptionRebasingUSDY` in the same tx (`redeem_rebasing_usdy`) | 14 | 0.91M (1.05M rUSDY) |
| `request` | Legacy `USDYManager` `0x25a103a1d6aec5967c1a4fe2039cdc514886b97e` (`usdy_manager`), 2023-10-02 → 2026-04-07 | `requestRedemptionServicedOffchain(amount, offChainDestination)` | `RedemptionRequestedServicedOffChain` (`request_redemption_serviced_offchain`) | 81 | 87.60M |
| `request` | same | `requestRedemption(amount)`, then `claimRedemption` | `RedemptionRequested` (`request_redemption`) | 0 | 0 |

Instant details:
- The manager burns the USDY and pays the stablecoin in the same tx. On Ethereum the payout is always USDC.
- `Redemption(redeemer, redeemerId, rwaAmount, receivingToken, receiveTokenAmount, redemptionUSDValue, fee)`: `fee` and `redemptionUSDValue` are USD with 18 decimals. All fees so far are 0.
- The USDC comes from `OndoTokenRouter` `0x99b8d1d1c17a10cd1a878d1a44c11fd7e4dad7bc` (`TokenWithdrawn`).
- `redeemer` is `msg.sender`. Many calls come through routers and smart wallets (CoW Protocol, 0x Settler, ERC-4337 EntryPoint), so `holder` can be a contract.

Request details:
- The USDY is burned when the request is made. Ondo pays USD off-chain (bank wire to non-US accounts **[doc]**), so `token_out` is `0x0` / `FIAT`.
- `request_id` is the hub's `redemptionId` (a counter).
- Ondo's docs list `USDYManager` as legacy (deprecated).

## 3. rUSDY

rUSDY has no redemption function of its own, so the collector reads no rUSDY logs.

| rUSDY function | What happens | Rows |
|---|---|---|
| `redeemRebasingUSDY` on the instant manager | rUSDY → USDY → burned → USDC | Yes, `redeem_rebasing_usdy` (§2). The manifest check has `rebasing_wrapper_in` (rUSDY) and `rebasing_token_in` (USDY). |
| `unwrap` / `unwrapShares` | rUSDY → USDY to the holder (conversion) | No. A later redemption of that USDY is a normal `redeem` row. |
| `burnShares` (admin, `BURNER_ROLE`) | Burns rUSDY of any account; the USDY goes to the admin | No. Never used on Ethereum. |

Check on 2026-11-18: all 933 rUSDY share burns are 919 unwraps (~32.1M USDY) and 14 `redeemRebasingUSDY` (~0.91M USDY).

## 4. Reconciliation: all USDY burns

All 1,687 USDY burns since deploy (1,236.5M USDY), grouped by the event in the same tx:

| Group | Burns | USDY | Collected |
|---|---|---|---|
| Instant manager `Redemption` | 918 | 91.48M | Yes (`instant`) |
| `USDYManager` `RedemptionRequestedServicedOffChain` | 81 | 87.60M | Yes (`request`) |
| Safe `0x505ff4462ba5e62ed529fa836d768ecd7b85439c` burns its own balance (2025-03-30 → now) | 32 | 926.44M | **No** (undocumented) |
| Safe `0x1a694a09494e214a3be3652e4b343b7b81a73ad7` (2024-03-05 → 2024-11-21) | 11 | 112.46M | **No** (undocumented) |
| Safe `0x7fbe0de6ffa86f4b9528aa27029595429b0c74a9` | 4 | 3.29M | **No** (undocumented) |
| LayerZero OFT adapter `0xa6275720b3fb1efe3e6ef2b5bf2293148852307d` (`OFTSent`) | 496 | 13.48M | No: bridging |
| Axelar `SourceBridge` `0xd89655ecf4800251880f8f6ba9038970ad9813db` (2023-10-26 → 2024-10-24) | 144 | 1.76M | No: bridging |
| Direct `burn` on the token | 1 | 1 | No |

About the Safes:
- A few large holders, most of them contracts, send USDY to the Safe. The Safe burns its balance later. 25 different addresses sent to `0x505f…`; the largest sent 364M.
- Most senders got no USDC on-chain in the 8 days after the transfer.
- Some senders (for example `0xc392749b6ff2cd95e5a4e3ed396c93f813395041`) also used the `USDYManager` off-chain request. This suggests a request-type redemption.
- `0x1a69…` also received 44.4M newly minted USDY and sent 44.4M out. Thus part of the Safe activity can be Ondo treasury operations, not redemptions.
- Ondo's address list has "USDY Redemptions Account" entries for Sui, Aptos, Noble, Arbitrum and Stellar, but none for Ethereum.

To add this path later, confirm it with Ondo first. Then it can work like BUIDL's redemption wallet: `Transfer(from, to = Safe)` on the token, without mints and Ondo-internal senders.

## 5. Price

The `price_sources` list (first source that gives a price wins):

| Source | Address | From block | Notes |
|---|---|---|---|
| `usdy_oracle_wrapper` (`getPrice`) | `0x87b126e5518b6a1bb8465779b4607c45c643df90` | 23,949,241 | `USDYOracleWrapper`: the canonical oracle (`ContinuousPriceOracle` `0xbaec2b9fe27cd054e1c0c2b720af117d9d7ec871` since 2026-11-01 **[doc]**). Same value as `OndoOracle.getAssetPrice(USDY)`, which the instant manager uses. |
| `usdy_rwa_dynamic_oracle` (`getPrice`) | `0xa0219aa5b31e65bc920b5b6dfb8edf0988121de0` | 18,485,028 | `RWADynamicOracle` (daily rate). Equal to the wrapper until 2026-11-01. |
| `usdy_pricer` (`getLatestPrice`) | `0x7fb0228c6338da4ec948df7b6a8e22ad2bb2bfb5` | 17,672,215 | `USDYPricer` of `USDYManager`: the last price set by Ondo. Used for 1 request (block 18,263,212). |

`price_consistency` for the instant manager: median difference 1e-11, maximum 9e-7. The oracle price is the price that the manager pays.

## 6. Keeping the integration current (checklist)

1. **New instant manager.** Look in <https://docs.ondo.finance/addresses.md>. Add it under `instant_managers`.
2. **New payout token.** Add it to `assets` in `config/chains.yaml`. The adapter raises an error for an unknown payout token.
3. **Fees.** `checks.instant_managers.*.fees_usd` is 0 so far. A non-zero value goes to `token_out_fee_amount`.
4. **Completeness.** Repeat the burn reconciliation (§4). A new group with a redemption event means a new path.

## 7. Out of scope (on purpose)

- Transfers to Ondo Safes (§4), until Ondo confirms them as redemptions.
- Bridge burns and rUSDY unwraps.
- USDY on other chains (BNB Chain and Tempo have their own instant managers **[doc]**).
- Request payout (off-chain).
