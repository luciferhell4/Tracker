"""Scan pass: walk every watched wallet and record the mints we hadn't seen."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import chains
from .config import Config
from .models import MintEvent, Wallet
from .providers import build_providers, provider_for
from .store import Store


@dataclass
class ScanResult:
    new_events: list[MintEvent] = field(default_factory=list)
    scanned_wallets: int = 0
    per_chain: dict[str, int] = field(default_factory=dict)
    skipped_chains: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        chains_seen = ", ".join(f"{k}:{v}" for k, v in sorted(self.per_chain.items())) or "none"
        return (
            f"{len(self.new_events)} new mints across {self.scanned_wallets} wallets "
            f"({chains_seen}); {len(self.errors)} errors"
        )


def _cursor_value(chain: str, event: MintEvent) -> int:
    """EVM wallets are cursored by block, Solana wallets by unix time."""
    return event.timestamp if chains.get(chain).kind == "svm" else event.block_number


def scan(cfg: Config, store: Store, only_chains: list[str] | None = None) -> ScanResult:
    providers = build_providers(cfg)
    result = ScanResult()
    if not providers:
        result.errors.append(
            "No chain provider credentials found. Set ALCHEMY_API_KEY, ETHERSCAN_API_KEY "
            "or HELIUS_API_KEY."
        )
        return result

    wanted = set(only_chains or cfg.scan.chains or [])
    cutoff = int(time.time()) - cfg.scan.lookback_hours * 3600

    for chain, wallets in store.iter_wallets_by_chain():
        if wanted and chain not in wanted:
            continue
        if chain not in chains.CHAINS:
            result.skipped_chains[chain] = "unknown chain"
            continue
        provider = provider_for(providers, chain)
        if provider is None:
            result.skipped_chains[chain] = "no provider with credentials for this chain"
            continue
        if cfg.scan.max_wallets_per_chain:
            wallets = wallets[: cfg.scan.max_wallets_per_chain]

        for wallet in wallets:
            result.scanned_wallets += 1
            try:
                events = provider.fetch_mints(
                    chain,
                    wallet.address,
                    since_block=store.cursor(chain, wallet.address),
                    lookback_hours=cfg.scan.lookback_hours,
                )
            except Exception as exc:
                result.errors.append(f"{chain}/{wallet.address[:10]}…: {exc}")
                continue

            recent = [e for e in events if not e.timestamp or e.timestamp >= cutoff]
            fresh = store.record_mints(recent)
            if fresh:
                result.new_events.extend(fresh)
                result.per_chain[chain] = result.per_chain.get(chain, 0) + len(fresh)
            if events:
                store.set_cursor(
                    chain, wallet.address, max(_cursor_value(chain, e) for e in events)
                )
    return result


def wallet_index(wallets: list[Wallet]) -> dict[tuple[str, str], Wallet]:
    return {w.key(): w for w in wallets}
