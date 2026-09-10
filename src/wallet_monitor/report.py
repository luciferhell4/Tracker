"""Human-readable rendering of signals."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import chains
from .models import Signal


def _ago(ts: int, now: int | None = None) -> str:
    now = now or int(time.time())
    seconds = max(0, now - ts)
    if seconds < 90:
        return f"{seconds}s ago"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m ago"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def _stamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def signal_lines(signal: Signal, now: int | None = None) -> list[str]:
    chain = chains.CHAINS.get(signal.chain)
    name = signal.collection_name or signal.contract[:10] + "…"
    header = (
        f"[{signal.score:6.2f}] {signal.chain:<9} {name}  "
        f"{signal.wallet_count} wallets / {signal.mint_count} mints "
        f"in {signal.window_minutes:.0f}m, first seen {_ago(signal.first_mint_at, now)}"
    )
    lines = [header, f"         contract {signal.contract}"]
    if chain:
        lines.append(f"         {chain.token_url(signal.contract)}")
        if signal.sample_tx:
            lines.append(f"         tx {chain.tx_url(signal.sample_tx)}")
    for wallet in signal.wallets[:8]:
        bits = [wallet.address]
        if wallet.tag:
            bits.append(wallet.tag)
        if wallet.rank is not None:
            bits.append(f"rank {wallet.rank:g}")
        if wallet.pnl_usd:
            bits.append(f"${wallet.pnl_usd:,.0f} pnl")
        lines.append("         - " + "  ".join(bits))
    if len(signal.wallets) > 8:
        lines.append(f"         … and {len(signal.wallets) - 8} more")
    return lines


def render(signals: list[Signal], now: int | None = None) -> str:
    if not signals:
        return "No clustered mints cleared the current thresholds."
    now = now or int(time.time())
    out = [f"Early-mint signals as of {_stamp(now)}", "=" * 78]
    for signal in signals:
        out.extend(signal_lines(signal, now))
        out.append("")
    return "\n".join(out).rstrip()


def render_markdown(signals: list[Signal], now: int | None = None) -> str:
    if not signals:
        return "_No clustered mints cleared the current thresholds._"
    now = now or int(time.time())
    rows = [
        "| Score | Chain | Collection | Wallets | Window | Age | Contract |",
        "|------:|-------|------------|--------:|-------:|-----|----------|",
    ]
    for s in signals:
        chain = chains.CHAINS.get(s.chain)
        link = chain.token_url(s.contract) if chain else ""
        name = s.collection_name or s.contract[:10] + "…"
        contract = f"[{s.contract[:10]}…]({link})" if link else s.contract
        rows.append(
            f"| {s.score:.2f} | {s.chain} | {name} | {s.wallet_count} | "
            f"{s.window_minutes:.0f}m | {_ago(s.first_mint_at, now)} | {contract} |"
        )
    return "\n".join(rows)
