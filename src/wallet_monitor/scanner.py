"""Scan pass: walk every watched wallet and record the mints we hadn't seen."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import chains
from .config import Config
from .models import MintEvent, Wallet
from .providers import RpcProvider, build_providers, provider_for, rpc_provider
from .store import Store


# scan_cursors row that tracks a whole chain rather than one wallet.
CHAIN_CURSOR = "__chain__"


@dataclass
class ScanResult:
    new_events: list[MintEvent] = field(default_factory=list)
    scanned_wallets: int = 0
    per_chain: dict[str, int] = field(default_factory=dict)
    swept_blocks: dict[str, int] = field(default_factory=dict)
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
    sweeper = rpc_provider(providers)
    result = ScanResult()
    if not providers:
        result.errors.append(
            "No chain provider is available. Set ALCHEMY_API_KEY or HELIUS_API_KEY, "
            "or watch a chain that has a public RPC."
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
            result.skipped_chains[chain] = "no provider covers this chain"
            continue

        # Chains without a commercial provider are swept whole: one range
        # query covers every watched wallet at once, where querying them one
        # by one would mean hundreds of requests per pass.
        if sweeper is not None and provider is sweeper:
            _sweep_chain(cfg, store, sweeper, chain, wallets, result)
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

            # A mint whose time is unknown cannot be placed in a cluster
            # window, so it is dropped rather than stamped with "now".
            recent = [e for e in events if e.timestamp >= cutoff]
            fresh = store.record_mints(recent)
            if fresh:
                result.new_events.extend(fresh)
                result.per_chain[chain] = result.per_chain.get(chain, 0) + len(fresh)
            if events:
                store.set_cursor(
                    chain, wallet.address, max(_cursor_value(chain, e) for e in events)
                )
    return result


def _sweep_chain(
    cfg: Config,
    store: Store,
    sweeper: RpcProvider,
    chain: str,
    wallets: list[Wallet],
    result: ScanResult,
) -> None:
    """One chain-wide pass over the next block window."""
    result.scanned_wallets += len(wallets)
    try:
        head = sweeper.head_block(chain)
        stored = store.cursor(chain, CHAIN_CURSOR)
        # A first pass starts one window back rather than at genesis.
        start = stored if stored > 0 else max(0, head - sweeper.window_for(chain))

        events, reached = sweeper.sweep(chain, start, head, [w.address for w in wallets])
        placed = [e for e in events if e.timestamp > 0]
        fresh = store.record_mints(placed)
        if fresh:
            result.new_events.extend(fresh)
            result.per_chain[chain] = result.per_chain.get(chain, 0) + len(fresh)
        store.set_cursor(chain, CHAIN_CURSOR, reached)
        result.swept_blocks[chain] = max(0, reached - start)
    except Exception as exc:
        result.errors.append(f"{chain} sweep: {exc}")


def wallet_index(wallets: list[Wallet]) -> dict[tuple[str, str], Wallet]:
    return {w.key(): w for w in wallets}
