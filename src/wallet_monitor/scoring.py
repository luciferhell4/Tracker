"""Turn raw mints into ranked signals.

The premise: one smart wallet minting something is noise, several of them
minting the same contract inside a short window is the signal. Score rewards
how many tracked wallets piled in, how good those wallets are, how tightly
clustered the mints were, how recent they are, and how young the contract is.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Callable, Iterable, Mapping

from .config import SignalRules
from .models import MintEvent, Signal, Wallet

RECENCY_HALF_LIFE_HOURS = 12.0
MAX_SPEED_BONUS = 3.0


def wallet_weight(wallet: Wallet | None) -> float:
    """How much one wallet's participation counts. Baseline 1.0."""
    if wallet is None:
        return 1.0
    weight = 1.0
    if wallet.rank is not None and wallet.rank > 0:
        # Rank 1 is worth +1.5, decaying to nothing by rank ~75.
        weight += max(0.0, 1.5 - 0.02 * (wallet.rank - 1))
    if wallet.pnl_usd and wallet.pnl_usd > 0:
        # $10k realised is worth +1.0, $1M +1.5, and it stops there.
        weight += min(1.5, math.log10(wallet.pnl_usd) / 4.0)
    return round(weight, 4)


def best_window(
    timestamps: list[tuple[int, str]], window_seconds: int
) -> tuple[list[str], int, int]:
    """Densest set of distinct wallets inside any `window_seconds` slice.

    Returns (wallets, window_start, window_end). Input is (timestamp, wallet)
    and need not be sorted.
    """
    if not timestamps:
        return ([], 0, 0)
    ordered = sorted(timestamps)
    best: tuple[int, list[str], int, int] = (0, [], ordered[0][0], ordered[0][0])
    left = 0
    seen: dict[str, int] = defaultdict(int)
    for right, (ts, wallet) in enumerate(ordered):
        seen[wallet] += 1
        while ts - ordered[left][0] > window_seconds:
            drop = ordered[left][1]
            seen[drop] -= 1
            if seen[drop] == 0:
                del seen[drop]
            left += 1
        if len(seen) > best[0]:
            best = (len(seen), sorted(seen), ordered[left][0], ts)
    return (best[1], best[2], best[3])


def _recency_factor(last_mint_at: int, now: int) -> float:
    hours = max(0.0, (now - last_mint_at) / 3600.0)
    return 0.5 ** (hours / RECENCY_HALF_LIFE_HOURS)


def _speed_factor(window_seconds: int, rule_window_seconds: int) -> float:
    if window_seconds <= 0:
        return MAX_SPEED_BONUS
    return min(MAX_SPEED_BONUS, max(1.0, rule_window_seconds / window_seconds))


def _earliness_factor(first_seen: int, now: int, max_age_hours: float) -> float:
    if max_age_hours <= 0:
        return 1.0
    age_hours = max(0.0, (now - first_seen) / 3600.0)
    return 1.0 + max(0.0, (max_age_hours - age_hours) / max_age_hours)


def build_signals(
    events: Iterable[MintEvent],
    wallets: Mapping[tuple[str, str], Wallet],
    rules: SignalRules,
    *,
    now: int | None = None,
    first_seen: Callable[[str, str], int | None] | None = None,
) -> list[Signal]:
    """Group events by contract and score each group. Highest score first."""
    now = now or int(time.time())
    window_seconds = rules.window_minutes * 60

    grouped: dict[tuple[str, str], list[MintEvent]] = defaultdict(list)
    for event in events:
        grouped[(event.chain, event.contract)].append(event)

    signals: list[Signal] = []
    for (chain, contract), group in grouped.items():
        members, start, end = best_window(
            [(e.timestamp, e.wallet) for e in group], window_seconds
        )
        if len(members) < rules.min_wallets:
            continue

        earliest = first_seen(chain, contract) if first_seen else None
        earliest = earliest or min(e.timestamp for e in group)
        age_hours = (now - earliest) / 3600.0
        if rules.max_contract_age_hours and age_hours > rules.max_contract_age_hours:
            continue

        member_wallets = [wallets.get((chain, addr)) for addr in members]
        base = sum(wallet_weight(w) for w in member_wallets)
        score = (
            base
            * _speed_factor(end - start, window_seconds)
            * _recency_factor(end, now)
            * _earliness_factor(earliest, now, rules.max_contract_age_hours)
        )
        if score < rules.min_score:
            continue

        named = next((e.collection_name for e in group if e.collection_name), "")
        in_window = [e for e in group if start <= e.timestamp <= end]
        signals.append(
            Signal(
                chain=chain,
                contract=contract,
                collection_name=named,
                score=round(score, 3),
                wallet_count=len(members),
                mint_count=len(in_window),
                first_mint_at=earliest,
                last_mint_at=end,
                wallets=[
                    w or Wallet(address=addr, chain=chain)
                    for addr, w in zip(members, member_wallets)
                ],
                sample_tx=in_window[0].tx_hash if in_window else group[0].tx_hash,
            )
        )

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals[: rules.top_n] if rules.top_n else signals
