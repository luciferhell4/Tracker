# Environment variables

Every variable the application reads, in one place. **None is required**:
Robinhood Chain, HyperEVM and Ink are read over public JSON-RPC, and the
watchlist imports from a published Notion site, so a default install runs with
an empty environment. Everything below either widens coverage or overrides a
setting from `config.toml`.

Secrets are read from the environment only. They are never written to
`config.toml`, never stored in the database, and never logged.

## Chain data

Each key adds chains. Without any of them the monitor still covers 763 of the
815 imported wallets.

| Variable | Default | Effect when set |
|---|---|---|
| `ALCHEMY_API_KEY` | unset | Adds Ethereum, Base, Arbitrum, OP Mainnet, Polygon, Abstract, Shape, Zora, Berachain, ApeChain and Blast. Preferred on any chain it serves. |
| `HELIUS_API_KEY` | unset | Adds Solana: NFT mints and first swaps into a token. |
| `ETHERSCAN_API_KEY` | unset | A fallback on chains Etherscan V2 indexes. Never consulted for Robinhood Chain or Arc, whose chain ids it does not know. |

When a chain has no key and no public RPC, `doctor` prints `NO PROVIDER` for it
and scans skip it with a named error rather than failing silently.

## Watchlist import

| Variable | Default | Effect when set |
|---|---|---|
| `NOTION_TOKEN` | unset | Only needed for Notion tables that are **not** published, reached with `wallet-monitor sync --use-token`. The five configured tables are published, so plain `sync` needs nothing. |

## Alerts

Both sinks are optional. With neither set, signals are printed and stored but
not pushed anywhere.

| Variable | Default | Effect when set |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | unset | Posts each new signal batch to that webhook. |
| `TELEGRAM_BOT_TOKEN` | unset | Sends signals to Telegram. Needs `TELEGRAM_CHAT_ID` as well; either alone does nothing. |
| `TELEGRAM_CHAT_ID` | unset | The chat that receives them. |

## Runtime

| Variable | Default | Effect when set |
|---|---|---|
| `TRACKER_CONFIG` | `config.toml` | Path to the config file. Set it when the working directory is not the app directory. |
| `TRACKER_DB` | `data/tracker.sqlite` | Path to the SQLite database. The only file that needs backing up. |
| `TRACKER_HOST` | `127.0.0.1` | Address the dashboard binds. **Leave it on loopback in production** and put a reverse proxy in front; the app has no authentication of its own. |
| `TRACKER_PORT` | `8787` | Port the dashboard binds. |

## Tuning

These override `[signal]` and `[scan]` in `config.toml`, for changing thresholds
without editing a file.

| Variable | Default | Effect when set |
|---|---|---|
| `TRACKER_MIN_WALLETS` | `2` | Distinct tracked wallets needed in one contract before it counts as a signal. Raise it for less noise. |
| `TRACKER_WINDOW_MINUTES` | `180` | How close together those mints must land. |
| `TRACKER_LOOKBACK_HOURS` | `24` | How far back a scan pass looks. |

## File format

`deploy/install.sh` writes `/etc/wallet-monitor.env`, read by both systemd units
through `EnvironmentFile=`. That format is **bare `KEY=value` lines with no
`export`** — systemd treats `export ALCHEMY_API_KEY=x` as a variable literally
named `export ALCHEMY_API_KEY`, which silently does nothing.

To load the same file into an interactive shell instead:

```bash
set -a && . /etc/wallet-monitor.env && set +a
```

Keep the file at mode `640`, owned `root:wallet`, so only root and the service
account can read it.
