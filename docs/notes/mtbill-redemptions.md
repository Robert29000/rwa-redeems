# mTBILL redemptions: how the collector works and how to keep it current

Notes for humans and agents who work on the Midas mTBILL integration.
Facts were checked on-chain on 2026-11-18 (data up to block 26,437,591) unless marked **[doc]**.

- Code: `src/rwa_redeems/protocols/midas.py` (shared with mWIN; the module docstring is the short version)
- Config: `config/tokens.yaml` (mTBILL entry)
- ABIs: `abis/midas/` (RedemptionVault, RedemptionVaultLegacy, DataFeed)
- Tests: `tests/test_midas.py` (real mainnet logs and receipts in `tests/fixtures/`)
- Run: `uv run rwa-redeems collect --tokens mTBILL --output-dir <dir>` (use a separate dir: a single-token run overwrites `redemptions.csv` with that token only)

## 1. Token facts

| Item | Value |
|---|---|
| Token | mTBILL, Midas tokenized US Treasury bills |
| Address (Ethereum) | `0xdd629e5241cbc5919847783e6c96b2de4754e438` (deploy block 18,691,255) |
| Decimals | 18 |
| Price | Custom aggregator `0x056339c044055819e8db84e71f5f2e1f536b2e5b` (8 dp, `latestRoundData`). The vaults read it through DataFeed `0xfcee9754e8c375e145303b7ce7beca3201734a2b` (`getDataInBase18`). `DataFeed.aggregator()` is the same address at every block since the vaults were deployed. |
| `start_block` | 20,826,541 (first v2 redemption vault) |
| Request redeemer | `0x8c89ddb8e8c64325ea2728828e7b1185c89efe9c` (`requestRedeemer()` on both vaults; holds the payout for approved requests) |

Address source: `config/constants/addresses.ts` → `main.mTBILL` in <https://github.com/midas-apps/contracts> **[doc]**.
The docs registry (<https://docs.midas.app/resources/smart-contracts-registry>) refuses automated fetches.

## 2. Vaults

| Name in config | Address | Contract | From block | Rows |
|---|---|---|---|---|
| `main` | `0xf6e51d24f4793ac5e71e0502213a9bbe3a6d4517` | `RedemptionVault` | 20,826,541 | Requests only so far (38) |
| `ustb` | `0x569d7dccbf6923350521ecbc28a555a500c4f0ec` | `RedemptionVaultWithBUIDL` until block 22,674,825, then `RedemptionVaultWithUSTB` | 20,826,724 | All instants (5,398), 61 requests incl. 2 fiat |

Both are ERC-1967 proxies. Upgrades (`Upgraded` events): `main` at 24,190,670; `ustb` at 22,674,825 (BUIDL → USTB) and 24,190,674.
The events are the same in every version, so one ABI (`midas/RedemptionVault`) reads all of them, with one exception (§4).

Why two vaults: the Midas deploy config (`scripts/deploy/configs/mTBILL.ts`) defines a `REGULAR` vault (`rv`) and a `BUIDL` vault (`rvBuidl`) for each mToken **[doc]**.
The `REGULAR` vault can pay an instant redemption only from USDC that it already holds.
On mainnet the `main` vault is almost unused:

| Setting (block 26,437,591) | `main` | `ustb` |
|---|---|---|
| `instantDailyLimit()` | 1 wei (set at 20,917,460, 2024-10-08): instant is effectively off | 10M mTBILL |
| `paused()` | true (since 25,775,438, 2026-08-17) | false |
| USDC balance | 0 | 0 (holds about 6.03M USTB) |
| Requests | 38, $11.8k in total (2025-05-11 → 2026-08-03) | 59 ($6.2M) + 2 fiat ($0.67M) |

The `main` vault stays in the config: its requests are real redemptions, and all mTBILL burns since `start_block` reconcile only with both vaults (§7, item 5).

## 3. Redemption types

| Type in CSV | Call | Event (`method`) |
|---|---|---|
| `instant` | `redeemInstant(tokenOut, amount, minReceive)` | `RedeemInstant` (`redeem_instant`) |
| `instant` | `redeemInstant(..., recipient)` | `RedeemInstantWithCustomRecipient` (none for mTBILL so far) |
| `request` | `redeemRequest(tokenOut, amount)` / with recipient | `RedeemRequest` / `RedeemRequestWithCustomRecipient` |
| `fiat_request` | `redeemFiatRequest(amount)` | `RedeemRequest` with `tokenOut = 0x0` |

Approvals and rejections are not collected (same as mWIN).
Payout is USDC in every row so far. The `main` vault also lists M by M^0 (`0x866a2bf4e572cbcf37d5071a7a58503bfb36be1b`, stable) as a payment token, with no redemptions.

## 4. Payment tokens: legacy event

Vault code before the 2025 upgrades emitted `AddPaymentToken(caller, token, dataFeed, fee, stable)`.
The current code adds `allowance`, so topic0 changed (`0x619139d1…` → `0x049000a9…`).
mTBILL added all of its payment tokens with the old version.
The adapter reads both versions: the old one from `abis/midas/RedemptionVaultLegacy.json`.
Without it, `checks.vaults[*].payment_tokens` is empty and a non-stable payout token would not get a price.

## 5. Flow of funds and liquidity sources

```
INSTANT (ustb vault)
  holder ──redeemInstant()──► vault: burn mTBILL (fee in mTBILL to feeReceiver)
     if vault USDC < payout:
        until 22,674,825  vault ──BUIDL──► BUIDL Redemption 0x31d3… ──► Settlement 0x57dd…   event SettlementCompleted(redeemer = vault)
        after             vault ──USTB───► Superstate RedemptionIdle 0x4c21…              event RedeemV2(redeemer = vault)
     vault ──USDC──► holder                                                         event RedeemInstant
REQUEST
  holder ──redeemRequest()──► vault: mTBILL moves to the vault; admin approves later, USDC from the request redeemer
```

`params.liquidity_sources` on the `ustb` vault lists both external contracts.
The adapter sets `via_fallback = true` when the receipt has an event from one of them with topic1 = the vault.

| Phase | Instant rows | `via_fallback` true |
|---|---|---|
| BUIDL (to 22,674,825) | 988 | 66 |
| USTB (after) | 4,410 | 4,410 (every instant redeemed USTB) |

Cross-check: the BUIDL rows with holder = the `ustb` vault (66) and the USTB rows with holder = the `ustb` vault (4,410) match these counts exactly.

## 6. Double counting across tokens

The same economic redemption can appear on more than one token. Do not add USD across tokens without a filter.

1. **Downstream (mTBILL → BUIDL/USTB).** Each `via_fallback` mTBILL instant has a BUIDL or USTB row with `holder` = `0x569d…`. These are 66 BUIDL rows ($4.4M) and 4,410 USTB rows ($119.0M; about 40% of all USTB instant rows).
2. **Upstream (other Midas tokens → mTBILL).** 4,813 of 5,398 mTBILL instants ($114.8M of $136.4M) are made by another Midas vault in the same tx that funds its own payout. The holder emits its own `RedeemInstant` in that tx. The top holders are the `RedemptionVaultWithSwapper` vaults of mEDGE `0x9b2c…`, mMEV `0xac14…`, mBASIS `0x0d89…`, mAPOLLO `0x5aea…`, mHYPER `0x6be2…`, mFARM `0xf4f0…`, and mWIN `main` `0x6057…` (5 rows, $1.3M; those mWIN rows have `via_fallback = true`).
   Only mWIN is collected of these, so the other nested rows have no matching row in the output.
   The collector has no column for "this row funds a redemption of another token". To find them, check the holder's own `RedeemInstant` in the cached receipt (`data/raw/receipts/ethereum.logs.jsonl`).

## 7. Keeping the integration current (checklist)

1. **New vault.** Compare `main.mTBILL` in the Midas `addresses.ts` with the config. Add the vault under `vaults` with its `from_block`.
2. **Vault upgrade to a new liquidity source** (e.g. a new RedemptionVaultWithX). Read the vault's source on Etherscan. Add the external contract and its event to `params.liquidity_sources`. Check: `via_fallback` share per phase, and holder = vault rows on the other token.
3. **New aggregator.** Call `aggregator()` on the DataFeed. If it changed, add the new aggregator as a price source with `from_block`. `price_consistency` in the manifest shows a mismatch first.
4. **New payout token.** Add it to `assets` in `config/chains.yaml`. A non-stable token without a fixed price gets its price from its DataFeed.
5. **Burn reconciliation (completeness check).** Get all token `Transfer(from, to = 0x0)` logs since `start_block` from Etherscan (`topic2` = zero, `topic0_2_opr=and`). Each burn must be in a collected instant tx or come from a configured vault (request settlement). Result at block 26,437,591:

   | Burns | Count | mTBILL |
   |---|---|---|
   | In a collected instant tx | 5,398 | 130,831,065 (rows: 130,840,023; the difference is the fee in mTBILL, which is not burned) |
   | From `ustb` (request settlement) | 61 | 6,549,400 |
   | From `main` (request settlement) | 36 | 11,487 |
   | Other | 0 | 0 |

   A burn in the "Other" line means a redemption path that is not collected.

## 8. Out of scope (on purpose)

- **Pre-v2 redemptions.** From 18,691,432 to 20,693,403 there were 8 redemptions done as admin `burn(holder, amount)` calls on the token (35,192 mTBILL in total). There is no redemption event and no price feed before block 20,578,232.
- mTBILL on other chains.
- Request approval and rejection, and the payout of requests.
