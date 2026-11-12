# RWA Redeems Research

This tool collects the on-chain redemption events of RWA tokens: Superstate USTB, Superstate USCC and Midas mWIN.
It puts each redemption into one of three types: `instant`, `request` or `fiat_request`.
It gets the USD value of each redemption from the price oracle at the block of the event.
It writes the results to CSV files.

## Usage

1. Copy `.env.example` to `.env`. The file holds two API keys: `ETHERSCAN_API_KEY` and `DRPC_API_KEY`.
2. Run the commands below.

```bash
uv run rwa-redeems tokens         # show the tokens in the configuration
uv run rwa-redeems collect        # collect all tokens
uv run rwa-redeems collect --tokens USTB,mWIN --to-block 26000000
uv run rwa-redeems collect --refresh   # delete the cache and get all data again
uv run pytest                     # run the tests (offline, with real mainnet logs as fixtures)
```

## Output

The tool writes three files to `output/`. Git tracks this folder, so the notebook can use the data without a new run.

| File | Contents |
|---|---|
| `redemptions.csv` | One row for each redemption. `src/rwa_redeems/schema.py` describes each column. |
| `prices.csv` | Each oracle answer and each NAV value that the tool used. |
| `manifest.json` | The block range, the counts for each type, method and price source, and the results of the cross-checks. |

The tool keeps a cache of the raw data in `data/raw/`.
The cache holds event logs, oracle answers for each block, and transaction receipts.
When you run the tool again, it gets data only for the new blocks.

## Price sources

Each token has a list of price sources in `config/tokens.yaml`.
For each event, the tool uses the first source in the list that gives a price.

| Type | What it does |
|---|---|
| `rpc_call` | Calls a view function of the oracle at the event block (archive `eth_call`). The default function is `latestRoundData`. |
| `http_nav` | Reads the daily NAV from an HTTP API. It uses the last NAV with a date on or before the event date. |
| `constant` | Uses a fixed price. |

If no source gives a price for an event, the tool uses the first price that it found after that event.
The tool sets `price_is_backfilled` to `true` for these rows.

## Layout

| Path | Contents |
|---|---|
| `config/chains.yaml` | Chains: chain ID, RPC URL and known payout tokens. The tool replaces `${VAR}` with the value of the environment variable. |
| `config/tokens.yaml` | Tokens: protocol, addresses, start block, contracts for each role and price sources. |
| `abis/` | ABI files in JSON format. The configuration refers to each file by its name. |
| `src/rwa_redeems/sources/` | Clients for the Etherscan V2 log API and for JSON-RPC, and the disk cache. |
| `src/rwa_redeems/events.py` | Gets and decodes events for all protocols. |
| `src/rwa_redeems/pricing/` | The price source types and the fallback logic. |
| `src/rwa_redeems/protocols/` | One adapter for each protocol. An adapter only classifies events and joins them. |
| `src/rwa_redeems/pipeline.py` | Runs the steps in sequence: configuration, adapter, prices, flags, CSV files. |

## Extend the tool

To add a token on a protocol that the tool supports:

1. Add the token to `config/tokens.yaml`.
2. If the token pays out in a new asset, add the asset to `config/chains.yaml`.

To add a new protocol:

1. Add the ABI files to `abis/<protocol>/`.
2. Add a module to `src/rwa_redeems/protocols/`. In the module, write a subclass of `ProtocolAdapter` and add the decorator `@register("<protocol>")`. The adapter must return rows in the canonical schema.
3. Import the module in `src/rwa_redeems/protocols/__init__.py`.
4. Add the tokens of the protocol to `config/tokens.yaml`.

To add a new price source type:

1. Write a subclass of `PriceSource` and add the decorator `@register("<type>")`.
2. Add the configuration model of the source to the `PriceSourceConfig` union in `config.py`.

To add a new chain:

1. Add the chain to `config/chains.yaml`.
2. The tool gets logs from Etherscan V2 by `chain_id`. If your Etherscan plan does not include the chain, write a new `LogSource` class for a different log API.
