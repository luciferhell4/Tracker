# Wallet Monitor

Watches the smart-money wallets listed in the Notion tables and flags a mint the
moment several of them pile into the same contract. One good wallet minting
something is noise. Four of them minting the same contract inside twenty minutes
is the thing you wanted to see early.

No third-party Python packages. Python 3.11+ and a couple of API keys.

## What it does

1. **Imports the watchlist** from the five Notion databases (or a CSV), keeping
   each wallet's rank, realised PnL and tag.
2. **Scans** every wallet for freshly minted tokens, incrementally, one cursor
   per wallet so repeat passes are cheap.
3. **Clusters** those mints by contract and scores each cluster.
4. **Alerts** on stdout, Discord or Telegram, once per contract until more
   tracked wallets join it.

## Setup

```bash
git clone https://github.com/luciferhell4/Tracker.git && cd Tracker
cp .env.example .env      # fill in the keys, then: source .env
pip install -e .          # optional, gives you the `wallet-monitor` command
```

Without installing, run it as `PYTHONPATH=src python3 -m wallet_monitor <command>`.

### Keys

| Variable | Needed for | Where |
|---|---|---|
| `NOTION_TOKEN` | importing the watchlist | notion.so/my-integrations |
| `ALCHEMY_API_KEY` | EVM mints (preferred, one key covers every network) | alchemy.com |
| `ETHERSCAN_API_KEY` | EVM fallback and HyperEVM | etherscan.io/apis |
| `HELIUS_API_KEY` | Solana mints and first buys | helius.dev |

`wallet-monitor doctor` prints what is set, what is missing, and which provider
each chain in your watchlist will use.

## Loading the wallets

The five Notion databases are already listed in `config.toml`. They live in a
workspace that is not yours, so the Notion API will only return their rows to a
token that can see them:

```bash
export NOTION_TOKEN=ntn_...
wallet-monitor sync
```

Each database has to be shared with your integration first (open it in Notion →
`...` → Connections → add your integration). If a table is not shared, `sync`
reports that one as failed and still imports the rest.

### Without an integration

Two other ways in, either of which gets you running in under a minute.

**Paste the table.** Select the rows in Notion, copy, and pipe them in. Explorer
links, ranks and dollar amounts on the line are all read, so a copied table keeps
its wallet quality data:

```bash
pbpaste | wallet-monitor add --tag "Project Mars Land"
wallet-monitor add 0xabc... 0xdef... --chain base --tag Inks
```

**Export to CSV.** Only an `address` column is required. `chain`, `rank`,
`pnl_usd`, `tag` and `url` are used when present, and the chain is inferred from
the explorer link or the address shape when the column is missing:

```bash
wallet-monitor import-csv path/to/export.csv
wallet-monitor wallets
```

`data/wallets.example.csv` shows the shape. Imports merge rather than replace, so
you can mix all three sources; the richest record for each address wins.

## Running

```bash
wallet-monitor scan                  # one pass, alerts on anything new
wallet-monitor watch --interval 180  # same, on a loop
wallet-monitor top --hours 48        # re-report from stored data, no API calls
wallet-monitor top --markdown        # table for pasting into Notion or Discord
```

Limit a pass to one chain with `--chain base --chain abstract`. Re-alert on
contracts you have already been told about with `--no-dedupe`.

### On a schedule, without your machine

`.github/workflows/scan.yml` runs a pass every 15 minutes and posts to Discord.
Add whichever of these you use as repository secrets: `NOTION_TOKEN`,
`ALCHEMY_API_KEY`, `ETHERSCAN_API_KEY`, `HELIUS_API_KEY`, `DISCORD_WEBHOOK_URL`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Without `NOTION_TOKEN` the workflow reads `data/watchlist.csv`, so commit that
file if you built your list by pasting. The mint history and per-wallet cursors
ride between runs in the Actions cache, which keeps each pass incremental and
stops a contract alerting twice. Every run also writes the current top signals
to its job summary. GitHub's scheduler is best-effort and frequently runs late,
so treat 15 minutes as a floor.

## How the score works

For each contract, the scanner finds the densest slice of `window_minutes` and
counts the distinct tracked wallets that minted inside it. Below `min_wallets`,
nothing is reported. Above it:

```
score = sum(wallet weights) x speed x recency x earliness
```

- **wallet weight** starts at 1.0, up to +1.5 for rank (rank 1 is worth the
  most, decaying to nothing by rank 75) and up to +1.5 for realised PnL
  ($10k is worth +1.0, $1M +1.5).
- **speed** rewards a tight cluster: five wallets in ten minutes outranks five
  wallets in three hours, capped at 3x.
- **recency** halves every 12 hours since the last mint in the cluster.
- **earliness** doubles the score for a contract first seen just now, falling to
  1x at `max_contract_age_hours`.

Tune all of it in `config.toml` under `[signal]`, or per-run with
`TRACKER_MIN_WALLETS`, `TRACKER_WINDOW_MINUTES` and `TRACKER_LOOKBACK_HOURS`.

## Chains

Ethereum, Base, Arbitrum, OP Mainnet, Polygon, Abstract, Shape, Zora, Ink,
Berachain, ApeChain, Blast, HyperEVM and Solana. The chain for each wallet comes
from its explorer or gmgn link in Notion, so a multichain table sorts itself out.

Add a chain by adding one `Chain(...)` row in `src/wallet_monitor/chains.py`; if
Alchemy has no network slug for it, leave that field `None` and Etherscan V2
picks it up through its chain id.

## What counts as a mint

On EVM chains, an ERC-721 or ERC-1155 transfer whose sender is the zero address.
That covers ordinary mints and ERC721A batches, and deliberately excludes
secondary buys.

On Solana, an NFT or token mint, plus swaps into a token that is not SOL or a
stablecoin. Those swaps are tagged `spl-buy` so you can tell a first buy from a
real mint in the report.

## Layout

```
src/wallet_monitor/
  chains.py       chain registry, explorer links, address parsing
  config.py       config.toml + environment
  notion_sync.py  Notion import, CSV import/export
  paste.py        address extraction from pasted table rows
  providers/      alchemy, etherscan, helius, and the picker between them
  scanner.py      one incremental pass over every wallet
  scoring.py      clustering and the score
  store.py        sqlite: wallets, mints, cursors, alert history
  report.py       text and markdown output
  notify.py       stdout, Discord, Telegram
  cli.py          commands
```

## Tests

```bash
pip install pytest && python3 -m pytest
```

The suite covers the Notion property parsing, provider response parsing, the
clustering maths, the store's incremental behaviour and the CLI end to end,
using fixtures rather than live APIs. Nothing in it touches the network.

## Limits worth knowing

- A wallet's first scan is bounded by `lookback_hours`, not by history. Raise it
  for the first pass if you want more backfill.
- `max_contract_age_hours` is measured from the first mint *this tool has seen*,
  so a contract that was already old when you started tracking looks young for
  one pass. The alert dedupe means you only ever see that once.
- Etherscan's free tier is 5 calls/second and each wallet costs two of them, so
  a large watchlist on the Etherscan path takes a while. Alchemy is one call per
  wallet and much faster.
