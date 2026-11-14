# BUIDL redemptions: how the collector works and how to keep it current

Notes for humans and agents who work on the Securitize BUIDL integration.
Facts were checked on-chain on 2026-11-14 (data up to block 26,415,381) unless marked **[doc]**.

- Code: `src/rwa_redeems/protocols/securitize.py` (the module docstring is the short version of this file)
- Config: `config/tokens.yaml` (BUIDL entry), `config/chains.yaml` (payout assets USDC, USDtb, RLUSD)
- ABIs: `abis/securitize/` (DSToken, Redemption, Settlement, SecuritizeOffRamp)
- Tests: `tests/test_securitize.py` (real mainnet logs and receipts in `tests/fixtures/`)
- Run: `uv run rwa-redeems collect --tokens BUIDL --output-dir <dir>` (use a separate dir: a single-token run overwrites `redemptions.csv` with that token only)

## 1. Token facts

| Item | Value |
|---|---|
| Token | BUIDL, BlackRock USD Institutional Digital Liquidity Fund |
| Address (Ethereum) | `0x7712c34205737192402172409a8f7ccef8aa2aec` |
| Contract | Securitize `Proxy` (not EIP-1967; read the implementation with `target()`) → `DSToken` `0x603bb6909be14f83282e03632280d91be7fb83b2` |
| Deploy block | 19,343,293 (= `start_block`) |
| Decimals | 6 |
| Price | Always $1.00 (stable NAV). The collector uses a `constant` 1.0 price source. Every instant path pays out 1:1. |
| Dividends | Paid as newly minted BUIDL (`Transfer` from `0x0`) via BulkOperator `0x103b907c2278cc0a51ac5e700b84da9bfd65b1d1`, called by issuer EOA `0x5072ed40eba6be38c2370cad1cb1df0202924e53`. |

## 2. Redemption types

| Type in CSV | Path | Contract(s) | Row source (one row per event) | `method` |
|---|---|---|---|---|
| `request` | Holder transfers BUIDL to the redemption wallet. The issuer burns it later and settles off-chain (USD wire per [doc]). | Token `0x7712…2aec`, redemption wallet `0x8780dd016171b91e4df47075da0a947959c34200` | `Transfer(from, to = wallet)` on the token, filtered on `topic2` at Etherscan | `transfer_to_redemption_wallet` |
| `instant` | Redemption.sol + Settlement. Holder calls `redeem(amount)`; BUIDL goes to `settlement.recipient()`; stablecoin is paid 1:1 in the same tx. | Redemption USDC `0x31d3f59ad4aac0eee2247c65ebe8bf6e9e470a53` → Settlement `0x57dd4e92712b0fbc8d3f3e3645eebcf2600acef0` (paused since block 22,439,269). Redemption USDtb `0x4580d3c5fff079bde60af9e98b62d4fb031ee0a0` → Settlement `0x6c97c9754880e569c8f098b3cc654089f7ac5fbb` | `SettlementCompleted(redeemer, amount)` on the **settlement** (Redemption.sol emits no redemption event) | `settlement_completed` |
| `instant` | SecuritizeOffRamp. Holder calls `redeem`; BUIDL goes holder → off-ramp → LP wallet (two-step mode); payout = amount × NAV − fee in the same tx. | RLUSD off-ramp `0x04fb23ef3cb3eb559d0ba096fbfda90241ff8e66`; USDC off-ramp `0x6a1705b528c35d479f45418ef77007b28e02b09f` (ExternalLiquidityProviderOffRamp, 0 redemptions so far, not in the public feed) | `RedemptionCompleted(redeemer, dsTokenValue, liquidityValue, rate, fee, liquidityToken)` on the off-ramp | `redemption_completed` |

Counts on 2026-11-14: 173 requests (1,079.06M BUIDL), 1,297 USDC + 1 USDtb settlement redemptions, 12 RLUSD off-ramp redemptions.

### Event reference

| Event | Emitter | topic0 | Notes |
|---|---|---|---|
| `Transfer(address indexed from, address indexed to, uint256 value)` | token | `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef` | `to` is topic2 |
| `Burn(address indexed burner, uint256 value, string reason)` | token | `0x47e772fda56eb54ab211642ce5421882c49fc2b7033455982af14588ae4207ff` | Not a row source (see §4) |
| `SettlementUpdated(address indexed oldSettlement, address indexed newSettlement)` | Redemption.sol | `0xde376263f280bf4785b7a082ee48e81332c8647dbab924f4a09bd21868d74aac` | Complete history: the constructor sets no settlement |
| `SettlementCompleted(address indexed redeemer, uint256 amount)` | Settlement | `0x1762c954954f2abac7c28caee2ab4e282c064cbca83bb0e3e51d43af6cccef85` | `amount` is in **payout-token decimals** (USDC 6, USDtb 18 = BUIDL × 10^12) |
| `RecipientUpdated(address indexed oldRecipient, address indexed newRecipient)` | Settlement | `0x62e69886a5df0ba8ffcacbfc1388754e7abd9bde24b036354c561f1acd4e4593` | Generic signature: many unrelated contracts emit it |
| `RedemptionCompleted(address indexed redeemer, uint256 dsTokenValue, uint256 liquidityValue, uint256 rate, uint256 fee, address indexed liquidityToken)` | Off-ramp | `0x4034628961f2fe706ac3ef107584fe70ec7e098dd2814d73bbab589db225b0e5` | `rate` = NAV in 6 dp; `fee` and `liquidityValue` in payout-token units |
| `LiquidityProviderUpdated(address indexed oldProvider, address indexed newProvider)` | Off-ramp | `0xc9c8e8c3c0b36c9ad1dac9b1ec6ebbb5c43b302c59a7ba55ff508431a5ebf807` | Set once at block 23,233,087 for the RLUSD off-ramp |

Always filter these events by emitter address. Unrelated contracts use the same signatures (for example `SettlementUpdated` on `0x600d4d0f…`, `SettlementCompleted` on `0xac36dd64…`).

## 3. Flow of funds

```
REQUEST
  holder ──BUIDL──► redemption wallet 0x8780 ──(hours later)──► issuer burn(wallet, amount, "")   USD paid off-chain

INSTANT A: Redemption.sol + Settlement (no NAV, no fee, 1:1)
  holder ──redeem()──► Redemption.sol ──► Settlement.redeem()
     BUIDL:      holder ──► settlement.recipient()                     (LP wallet)
     stablecoin: liquidity source ──► holder                           event SettlementCompleted

INSTANT B: SecuritizeOffRamp (NAV from RedStone, fee from FeeManager; both give $1.00 and 0 today)
  holder ──redeem()──► off-ramp
     BUIDL:      holder ──► off-ramp ──► liquidityProvider().recipient() (LP wallet)
     stablecoin: LP wallet ──► off-ramp ──► holder                     event RedemptionCompleted

LATER, NOT A REDEMPTION
  LP wallets accumulate BUIDL (plus dividends earned while holding) and sweep it in batches ──► redemption wallet 0x8780
```

LP wallets so far:

| LP wallet | Path | Instant BUIDL in | Sweeps to 0x8780 | Pattern |
|---|---|---|---|---|
| `0xcf64a444c3aaa68d7bcbf353c7f3f654b139729e` (BitGo `WalletSimple` multisig) | USDC settlement recipient | 488.73M (1,296 redemptions) | 44 sweeps, 497.38M | About weekly, round lots of 4–25M; ~2 days median holding |
| `0x9d9affc9b217ef2276d3d1781dbda02fcb1c4582` (EOA that deployed Redemption `0x31d3`) | USDC settlement recipient during launch week | 25 | 1 sweep, 25 | Same day |
| `0x0cb4a346edd1b0c1f7b3ee8b1879630a1cc0b78b` | RLUSD off-ramp LP recipient | 8.65M (12) | 4 sweeps, 8.79M | About every 6 months |
| `0xc704beced0c5ac8844ca0c42738a5efb932e16bb` | USDtb settlement recipient (set in constructor, no event) | 1 | none | Still held |

## 4. Double counting and how the collector avoids it

1. **One row source per path.** Rows come only from `Transfer` to the redemption wallet, `SettlementCompleted` and `RedemptionCompleted`. The collector does not make rows from `Transfer` logs inside instant txs, `LiquiditySupplied` on the liquidity sources, or `Burn`.
2. **LP sweeps are excluded by sender.** Any transfer to the redemption wallet whose `from` is an LP wallet is dropped as a whole, however many instant redemptions it bundles. Each of those redemptions already has its own instant row with the correct timestamp. The LP wallet set is the union of:
   - **Receipt trace:** for every instant row, follow the BUIDL `Transfer` of the redeemed amount from the redeemer, through the instant contract itself for the two-step off-ramp. The final `to` is the LP wallet. This picks up recipient and LP changes automatically.
   - **Settlement recipients:** `RecipientUpdated` (old and new values) plus `recipient()` at the last block. This covers a recipient set in the constructor without an event and a recipient that has not received a redemption yet.
3. **Mints are excluded.** A transfer to the redemption wallet with `from = 0x0` is a mint, not a request. None so far.
4. **Burns are never rows.** All 179 burns of the redemption wallet repeat the inflows. Other burns are `"dstoken-swap"` (BUIDL → BUIDL-I share-class swaps via DSTokenClassSwap `0x1e5d83ff1a6d32c53f25937664c53e9b57e37021`), `"bridge"` (cross-chain via SecuritizeBridge) or direct issuer burns.
5. **Settlements are discovered, not configured.** The collector reads `SettlementUpdated` on each Redemption.sol, then reads `SettlementCompleted` on every settlement from the block it was set.

### Manifest checks to read after each run (`output/manifest.json` → `tokens["ethereum:BUIDL"].checks`)

| Check | Expected | If not |
|---|---|---|
| `instant_untraced` | 0 | An instant tx has no BUIDL transfer of the redeemed amount from the redeemer. Decode the receipt; a new routing (more hops, an integrator, burn instead of transfer) may need a change in `_trace_liquidity_wallets`. |
| `redemption_wallet.token_in` vs `token_burned` | Equal, or `token_in` slightly higher while a burn is pending (hours) | A large, lasting gap means tokens left the wallet some other way. Check its outgoing transfers. |
| `liquidity_wallets[*].token_amount` (swept) vs `instant_token_in` | Swept ≈ instant in + dividends earned while held (a few % more) | Swept much higher than instant in: the wallet may have made a genuine request of its own, or it receives BUIDL from another source. |
| `excluded_mints` | 0 | Look at the tx; mints to the redemption wallet are unexpected. |
| `settlements[*]` | One settlement per Redemption.sol; `recipient_now` known | A new settlement or recipient appears here automatically. Check that the payout asset is still the configured `params.token_out`. |
| `price_consistency` | All differences 0 | The payout no longer matches $1 × amount: a fee or a NAV ≠ 1 appeared. |

## 5. Keeping the integration current (checklist)

Things the collector picks up by itself: new settlements on a configured Redemption.sol, settlement recipient changes, off-ramp LP changes (via the receipt trace), new LP wallets.

Things that need a config change:

1. **A new off-ramp or Redemption.sol for BUIDL.** Check the public feed (§6). Add it under `offramps` or `redemption` in `config/tokens.yaml` with its `from_block`. A new Redemption.sol needs `params.token_out`.
2. **A new payout asset.** Add it to `assets` in `config/chains.yaml` (symbol, decimals, `usd_price: 1.0` for stablecoins). The adapter raises an error for an unknown payout token, so a run fails loudly instead of losing rows.
3. **A new redemption wallet.** Check `RedemptionWalletAddress` in the public feed and the token's WalletManager special wallets. Update `params.redemption_wallet`.
4. **A new contract type** with different events: add its ABI to `abis/securitize/` and a reader in the adapter.

How to look for new off-ramps on-chain: query Etherscan `getLogs` for the `RedemptionCompleted` topic0 without an address, then keep emitters whose `asset()` is BUIDL. The research also found off-ramps for other tokens (VBILL, HLSCOPE, FGNX) and for BUIDL-I. Those are not BUIDL.

## 6. Resources

### Securitize sources

| Resource | Use | Caveats |
|---|---|---|
| Securitize agent skill doc: <https://github.com/securitize-io/securitize-dapp-sample/blob/main/docs/SECURITIZE_SKILL.md> (raw: `https://raw.githubusercontent.com/securitize-io/securitize-dapp-sample/main/docs/SECURITIZE_SKILL.md`) | Describes the two paths (off-ramp vs transfer to `RedemptionWalletAddress`), how to read addresses from the public feed, ABIs | Known errors: the off-ramp has no `liquidity()` / `liquidityToken()` (use `liquidityProvider()`, and `liquidityToken()` on the LP); `getDSService(1)` is the Trust service, the registry is id 4; it lists `0x4580` as an "offramp" but it is a Redemption.sol |
| Public asset feed: `https://public-feed.securitize.io/asset-info?symbol=BUIDL` | Current off-ramps and `RedemptionWalletAddress` per chain | Lists only current contracts (`0x31D3` is gone). Misses `0x6a17`. The redemption wallet is shared with BUIDL on other chains, BUIDL-I and BRSRV, so always filter by token address. Other Securitize funds use `0xbb543C77436645C8b95B64eEc39E3C0d48D4842b`. |
| Fund page: `https://id.securitize.io/primary-market/opportunities/341` | Fund terms | Needs a Securitize login; agents cannot read it |
| rwa.xyz BUIDL page | [doc] terms: $250k minimum redemption, 0% fee, 3:00 PM ET cut-off, USD wire settlement | Not enforced on-chain (25-BUIDL test transfers exist) |

### On-chain sources

| Resource | Use |
|---|---|
| Etherscan API V2 `module=contract&action=getsourcecode` | Verified source, ABI, proxy implementation |
| Etherscan API V2 `module=logs&action=getLogs` with `topic0`, `topic2`, `topic0_2_opr=and` | All transfers to the redemption wallet in one query (what the collector does) |
| Etherscan `getcontractcreation` / `txlist` of deployers `0xd69fefe5df62373dcbde3e1f9625cf334a2dae78` and `0x0c81357a047581bf078a323821226698d39618b8` | Find new Securitize contracts |
| JSON-RPC `eth_call` | `Redemption.settlement()`, `Settlement.recipient()`, `Settlement.paused()`, `OffRamp.liquidityProvider()`, `LP.recipient()`, `OffRamp.asset()`, `OffRamp.navProvider()`, `OffRamp.feeManager()`, token `getDSService(uint256)` |
| JSON-RPC `eth_getTransactionReceipt` | Decode a full redemption tx; the collector caches receipts in `data/raw/receipts/` |
| RedStone feed `0xb9bd795bb71012c0f3cd1d9c9a4c686f2d3524a4` ("BUIDL_FUNDAMENTAL", 8 dp, `latestRoundData`) | BUIDL NAV, read by the off-ramp NAV provider `0x8d26470937a7d63cb9384c3788a5d708da35b4b3` (`rate()`, 6 dp). Not used by the collector (price is a constant 1.0) |
| FeeManager `0xd73c20c4ed7bb93ce61ab6a977b98c3616d2268d` (`feePercentageMBPS`) | Off-ramp fee, 0 so far |

### Project tooling for agents

- Set `ETHERSCAN_API_KEY` and `DRPC_API_KEY` in `.env` (see `.env.example`).
- dRPC: `https://lb.drpc.live/ethereum/<key>`. `eth_getLogs` is limited to 100 blocks there, so use Etherscan for logs. Archive `eth_call` and receipts work.
- Etherscan free plan: 3 requests/s shared by everything that runs at the same time.
- For ad hoc checks, `uv run python` has web3 and eth_abi.
- `EventFetcher.fetch(..., where={"to": wallet})` / `ctx.fetch(..., where=...)` filters on indexed event arguments at the source.

## 7. Out of scope (on purpose)

- **BUIDL-I** (`0x6a9da2d710bb9b700acde7cb81f10f1ff8c89041`, separate share class) and its USDC off-ramp `0x6a4d6578f92d864d6d6d5977faba4911bc2067ca`. BUIDL that goes BUIDL → BUIDL-I via DSTokenClassSwap and is then redeemed through that off-ramp appears on BUIDL only as a `"dstoken-swap"` burn.
- BUIDL on other chains (Arbitrum, Avalanche, BNB, Optimism, Polygon, Aptos, Solana, ...). They have no off-ramps.
- Request settlement (when and how the USD is paid). It happens off-chain.
- End users behind integrators. About 90% of USDC instant redemptions were called by contracts such as Ondo `OUSGInstantManager` (`0x2826989983e3a66f0622132d019c2ae173eb6a43`), `BuidlUSDCSource` and other vaults. `holder` is the contract that called `redeem`, which is what the chain records.
