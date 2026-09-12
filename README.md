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

**Nothing is required.** Robinhood Chain, HyperEVM and Ink are read over their
own public JSON-RPC, and the watchlist is imported from a published Notion site,
so a fresh install scans without a single credential. Keys only widen coverage:

| Variable | Adds | Where |
|---|---|---|
| `ALCHEMY_API_KEY` | Ethereum, Base, Arbitrum, OP, Polygon, Abstract, Shape, Zora, Berachain, ApeChain, Blast | alchemy.com |
| `HELIUS_API_KEY` | Solana mints and first buys | helius.dev |
| `ETHERSCAN_API_KEY` | a fallback on chains Etherscan V2 indexes | etherscan.io/apis |
| `NOTION_TOKEN` | only for Notion tables that are *not* published | notion.so/my-integrations |

`wallet-monitor doctor` prints what is set, what is missing, and which provider
each chain in your watchlist will use.

## Loading the wallets

The five tables are already listed in `config.toml`, and they are published as a
Notion Site. A published site answers the same endpoint the Notion web app uses,
so no token, no sharing step and no workspace membership are involved:

```bash
wallet-monitor sync
```

That pulls 815 wallets with their ranks, PnL and tags, and works out each
wallet's chain from its explorer link. One table carries no explorer column; its
addresses show heavy transaction counts on Robinhood Chain and next to none
anywhere else, so `config.toml` pins it there.

If you point the tool at Notion tables that are *not* published, use
`wallet-monitor sync --use-token` with `NOTION_TOKEN` set and each database
shared with your integration. If a table fails either way, `sync` reports that
one and still imports the rest.

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

## The dashboard

```bash
wallet-monitor serve --open      # http://127.0.0.1:8787
```

A local web app with the same engine behind it: ranked signals with the wallets
that triggered each one, a mint-activity chart and feed, a watchlist you can
paste into, and a setup tab that tells you which credentials are missing and
which provider each of your chains will use. Scans run in the background from
the button in the header, and the page refreshes itself every 30 seconds.

It binds to localhost only. That is deliberate, since it runs with your API keys
in its environment. A hosted page could not do this job at all: browsers cannot
reach Alchemy, Etherscan or Helius from a page you did not serve yourself.

## Production deployment

Three artifacts, each usable on its own or together by `deploy/install.sh`.

**Build command.** Idempotent, safe to re-run on every deploy:

```bash
./deploy/build.sh                       # builds ./.venv
APP_DIR=/opt/wallet-monitor ./deploy/build.sh
```

It pins the Python floor at 3.11 (the config loader uses `tomllib`), creates the
virtualenv, installs the package, then verifies the result: the console script
runs and the dashboard's assets are present in the installed package. A build
that ships without those assets starts cleanly and then serves a blank page, so
it is checked before any process manager is pointed at it. There are no
third-party runtime dependencies, so nothing is fetched beyond the package
itself.

**Environment variables.** The full list, with defaults and effects, is
`deploy/environment.md`; `.env.example` mirrors it. Nothing is required — the
chains the watchlist actually lives on need no credentials. Secrets are read
from the environment only, never written to `config.toml` or the database.

One format trap worth repeating: `EnvironmentFile=` takes bare `KEY=value` lines.
systemd does not parse `export`, and would create a variable literally named
`export ALCHEMY_API_KEY`. Both the example file and the one the installer writes
are already in the right format.

**Startup configuration.** `deploy/` carries the units:

| File | Role |
|---|---|
| `wallet-monitor.service` | the dashboard, restarted on failure |
| `wallet-monitor-scan.service` | one scan pass |
| `wallet-monitor-scan.timer` | runs that pass every three minutes |
| `nginx.conf` | TLS termination and basic auth in front |

Start command: `wallet-monitor serve`. Host and port come from `[server]` in
`config.toml`, overridable with `TRACKER_HOST` and `TRACKER_PORT`, so the unit
carries no hardcoded address. The service binds loopback and waits on a health
check before systemd calls the start successful, so a broken build or an
unreadable database fails the unit instead of flapping in a restart loop.

**Health check.** `GET /api/health` returns 200 with the version and row counts,
or 503 when the database cannot be opened. Point an uptime monitor at it:

```bash
curl -fsS http://127.0.0.1:8787/api/health
```

**Before going live:** keep the bind on loopback, put authentication in front
(the installer generates a basic-auth password), get a certificate with
`certbot --nginx`, and back up `data/tracker.sqlite` — it is the entire state.

## Deploying on a Hostinger VPS

One command on a fresh Ubuntu or Debian VPS:

```bash
git clone -b claude/wallet-monitor-early-mints-nmi5z3 https://github.com/luciferhell4/Tracker.git
sudo ./Tracker/deploy/install.sh monitor.example.com
```

It installs the app under `/opt/wallet-monitor`, creates an unprivileged
`wallet` user, imports the watchlist from Notion, and starts two systemd units:

- **`wallet-monitor.service`** serves the dashboard on `127.0.0.1:8787`.
- **`wallet-monitor-scan.timer`** runs a scan every three minutes. This is the
  part that makes it a monitor: Robinhood Chain moves about 3,000 blocks
  (roughly five minutes) per sweep, so a scan that only runs when you click a
  button falls permanently behind.

nginx proxies the dashboard and the installer prints a generated password for
it. **The dashboard is never exposed unauthenticated** — it can add and remove
wallets and spend your API credits — so basic auth is set up before the port is
opened. Point the domain at the VPS in Hostinger's DNS panel with an A record,
then get a certificate:

```bash
certbot --nginx -d monitor.example.com
```

Leave the domain off (`sudo ./deploy/install.sh`) and nothing is exposed: the
app stays on loopback and you reach it over an SSH tunnel.

Shared hosting will not work for this. It needs a long-lived process, so it has
to be a VPS.

Day to day:

```bash
journalctl -u wallet-monitor -f              # dashboard logs
journalctl -u wallet-monitor-scan -f         # what each scan found
systemctl start wallet-monitor-scan.service  # scan right now
systemctl list-timers wallet-monitor-scan    # when the next pass runs
```

To update: `git -C /opt/wallet-monitor pull && systemctl restart wallet-monitor`.

Secrets live in `/etc/wallet-monitor.env`, outside the repo and readable only by
root and the service. Add keys there and restart. The database is a single
SQLite file at `/opt/wallet-monitor/data/tracker.sqlite`; back that up and you
have backed up everything.

## Running from the command line

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

The watchlist is mostly not on the chains a commercial provider serves:

| Chain | Wallets | Read through |
|---|--:|---|
| Robinhood Chain | 584 | public RPC, no key |
| HyperEVM | 119 | public RPC, no key |
| Ink | 60 | Alchemy, or public RPC without a key |
| Arc | 52 | nothing yet |

Ethereum, Base, Arbitrum, OP Mainnet, Polygon, Abstract, Shape, Zora, Berachain,
ApeChain, Blast and Solana are supported too. Each wallet's chain comes from its
explorer or gmgn link, so a multichain table sorts itself out.

Arc is the one gap: its mainnet RPC is not published and no provider indexes it,
so those 52 wallets are imported and labelled but not scannable.

Add a chain with one `Chain(...)` row in `src/wallet_monitor/chains.py`. Give it
`rpc_urls` and it needs no key at all; set `log_range` to the widest span that
RPC accepts, and `max_blocks_per_scan` to about five minutes of its blocks.

### Why RPC chains are swept whole

A chain with no provider is read with `eth_getLogs` filtered to transfers *from*
the zero address, across the whole chain, and matched against the watchlist
locally. One request covers all 584 Robinhood wallets; asking per wallet would
be 584 requests a pass.

Robinhood Chain produces a block every 0.1 seconds, so a day is about 850,000
blocks and a full backfill is impossible. Each pass advances a per-chain cursor
by roughly five minutes of blocks and catches up over successive passes — which
is why the scan wants to run continuously rather than by hand.

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
  providers/      alchemy, etherscan, helius, public RPC, and the picker
  notion_public.py  tokenless import from a published Notion site
  scanner.py      one incremental pass over every wallet
  scoring.py      clustering and the score
  store.py        sqlite: wallets, mints, cursors, alert history
  report.py       text and markdown output
  notify.py       stdout, Discord, Telegram
  web.py          local HTTP server and JSON API
  web_assets/     the dashboard: one HTML page, one stylesheet, one script
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
- Public RPCs throttle hard. Requests are spaced and retried with backoff, and
  each chain advances a bounded window per pass. If a chain reports
  `RPC failed`, the error names every endpoint that refused and why.
- A mint whose block time cannot be read is **dropped, never stamped with the
  current time**. Alchemy ignores its own metadata flag on some networks, and
  guessing would make unrelated mints look simultaneous and manufacture signals
  out of nothing.
