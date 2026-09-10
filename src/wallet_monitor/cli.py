"""Command line entry point for the wallet monitor."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import chains, notify, notion_sync, paste, report
from .config import Config
from .providers.registry import build_providers, missing_credentials, provider_for
from .scanner import scan, wallet_index
from .scoring import build_signals
from .store import Store


def _store(cfg: Config) -> Store:
    return Store(cfg.store_path)


# ------------------------------------------------------------------ commands

def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    wallets, notes = notion_sync.sync(cfg)
    for note in notes:
        print(f"  {note}")
    if not wallets:
        print("No wallets imported. Check that each database is shared with your integration.")
        return 1
    with _store(cfg) as store:
        store.upsert_wallets(wallets)
    written = notion_sync.write_csv(wallets, cfg.wallets_csv)
    by_chain: dict[str, int] = {}
    for w in wallets:
        by_chain[w.chain] = by_chain.get(w.chain, 0) + 1
    print(f"\n{len(wallets)} unique wallets -> {cfg.store_path} and {cfg.wallets_csv} ({written} rows)")
    for chain, count in sorted(by_chain.items(), key=lambda kv: -kv[1]):
        print(f"  {chain:<10} {count}")
    return 0


def cmd_import_csv(cfg: Config, args: argparse.Namespace) -> int:
    path = Path(args.path or cfg.wallets_csv)
    wallets = notion_sync.read_csv(path)
    if not wallets:
        print(f"No usable rows in {path}. Expected at least an `address` column.")
        return 1
    with _store(cfg) as store:
        store.upsert_wallets(wallets)
    print(f"Imported {len(wallets)} wallets from {path}")
    return 0


def cmd_add(cfg: Config, args: argparse.Namespace) -> int:
    """Add wallets from arguments, or from text pasted on stdin."""
    if args.addresses:
        text = "\n".join(args.addresses)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        print("Pass addresses as arguments, or pipe a pasted Notion column on stdin.")
        return 1

    wallets = notion_sync.dedupe(
        paste.parse(text, source=args.source, tag=args.tag, chain=args.chain)
    )
    if not wallets:
        print("No addresses found in that input.")
        return 1
    with _store(cfg) as store:
        store.upsert_wallets(wallets)
    by_chain: dict[str, int] = {}
    for w in wallets:
        by_chain[w.chain] = by_chain.get(w.chain, 0) + 1
    print(f"Added {len(wallets)} wallets:")
    for chain, count in sorted(by_chain.items(), key=lambda kv: -kv[1]):
        print(f"  {chain:<10} {count}")
    return 0


def cmd_wallets(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as store:
        wallets = store.wallets(args.chain)
    if not wallets:
        print("No wallets yet. Run `sync` (Notion) or `import-csv`.")
        return 1
    for w in wallets:
        rank = f"#{w.rank:g}" if w.rank is not None else ""
        pnl = f"${w.pnl_usd:,.0f}" if w.pnl_usd else ""
        print(f"{w.chain:<10} {w.address:<45} {rank:<6} {pnl:<12} {w.tag or w.source}")
    print(f"\n{len(wallets)} wallets")
    return 0


def _alertable(cfg: Config, store: Store, since_hours: int, respect_alerted: bool):
    events = store.mints_since(int(time.time()) - since_hours * 3600)
    index = wallet_index(store.wallets())
    signals = build_signals(
        events, index, cfg.signal, first_seen=store.contract_first_seen
    )
    if not respect_alerted:
        return signals
    return [s for s in signals if store.should_alert(s.chain, s.contract, s.wallet_count)]


def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as store:
        if not store.wallets():
            print("No wallets to watch. Run `sync` or `import-csv` first.")
            return 1
        result = scan(cfg, store, args.chain)
        print(result.summary())
        for chain, reason in result.skipped_chains.items():
            print(f"  skipped {chain}: {reason}")
        for err in result.errors[:10]:
            print(f"  error: {err}")
        if not result.scanned_wallets and result.errors:
            return 1

        signals = _alertable(cfg, store, cfg.scan.lookback_hours, respect_alerted=not args.no_dedupe)
        if signals:
            print()
            notify.send(cfg, signals)
            for s in signals:
                store.mark_alerted(s.chain, s.contract, s.wallet_count)
        else:
            print("\nNo new clustered mints cleared the thresholds.")
    return 0


def cmd_watch(cfg: Config, args: argparse.Namespace) -> int:
    interval = args.interval or cfg.scan.interval_seconds
    print(f"Watching every {interval}s. Ctrl-C to stop.")
    while True:
        started = time.time()
        try:
            cmd_scan(cfg, args)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as exc:
            print(f"scan failed: {exc}", file=sys.stderr)
        try:
            time.sleep(max(5.0, interval - (time.time() - started)))
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def cmd_top(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as store:
        signals = _alertable(cfg, store, args.hours, respect_alerted=False)
        print(report.render_markdown(signals) if args.markdown else report.render(signals))
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    print(f"config store       : {cfg.store_path}")
    print(f"wallet csv         : {cfg.wallets_csv}")
    print(f"notion databases   : {len(cfg.notion_sources)} configured")
    print(f"NOTION_TOKEN       : {'set' if cfg.notion_token else 'MISSING'}")
    print(f"ALCHEMY_API_KEY    : {'set' if cfg.alchemy_key else 'missing'}")
    print(f"ETHERSCAN_API_KEY  : {'set' if cfg.etherscan_key else 'missing'}")
    print(f"HELIUS_API_KEY     : {'set' if cfg.helius_key else 'missing'}")
    print(
        f"rules              : >= {cfg.signal.min_wallets} wallets within "
        f"{cfg.signal.window_minutes}m, contract younger than "
        f"{cfg.signal.max_contract_age_hours}h"
    )
    for note in missing_credentials(cfg):
        print(f"  ! {note}")

    providers = build_providers(cfg)
    with _store(cfg) as store:
        stats = store.stats()
        print(f"stored             : {stats}")
        for chain in store.wallet_chains():
            provider = provider_for(providers, chain) if chain in chains.CHAINS else None
            print(f"  {chain:<10} -> {provider.name if provider else 'NO PROVIDER'}")
    return 0


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wallet-monitor",
        description="Watch smart-money wallets and surface the mints they cluster into early.",
    )
    parser.add_argument("-c", "--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="import wallets from the configured Notion databases")
    sync.set_defaults(func=cmd_sync)

    imp = sub.add_parser("import-csv", help="import wallets from a CSV file")
    imp.add_argument("path", nargs="?", help="defaults to the configured wallets csv")
    imp.set_defaults(func=cmd_import_csv)

    add = sub.add_parser("add", help="add wallets from arguments or pasted text on stdin")
    add.add_argument("addresses", nargs="*", help="addresses, or nothing to read stdin")
    add.add_argument("--chain", help="force a chain instead of inferring one")
    add.add_argument("--tag", default="", help="label these wallets, e.g. 'HYPE TERMINAL'")
    add.add_argument("--source", default="pasted")
    add.set_defaults(func=cmd_add)

    wallets = sub.add_parser("wallets", help="list the wallets being watched")
    wallets.add_argument("--chain")
    wallets.set_defaults(func=cmd_wallets)

    scan_cmd = sub.add_parser("scan", help="run one scan pass and alert on new signals")
    scan_cmd.add_argument("--chain", action="append", help="limit to a chain (repeatable)")
    scan_cmd.add_argument("--no-dedupe", action="store_true", help="re-alert on known contracts")
    scan_cmd.set_defaults(func=cmd_scan)

    watch = sub.add_parser("watch", help="scan on a loop")
    watch.add_argument("--interval", type=int, help="seconds between passes")
    watch.add_argument("--chain", action="append")
    watch.add_argument("--no-dedupe", action="store_true")
    watch.set_defaults(func=cmd_watch)

    top = sub.add_parser("top", help="report the current best signals without scanning")
    top.add_argument("--hours", type=int, default=48)
    top.add_argument("--markdown", action="store_true")
    top.set_defaults(func=cmd_top)

    doctor = sub.add_parser("doctor", help="show configuration and credential status")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    try:
        return int(args.func(cfg, args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
