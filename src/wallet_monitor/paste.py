"""Pull wallet addresses out of pasted text.

Copying a column straight out of a Notion table gives you addresses tangled up
with explorer links, ranks and tags. This finds the addresses in that mess so a
watchlist can be built without an API token.
"""

from __future__ import annotations

import re
from typing import Iterable

from . import chains
from .models import Wallet

_URL_RE = re.compile(r"https?://\S+")
_TOKEN_RE = re.compile(r"[^0-9A-Za-z]+")
_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*\.?\d*")


def _looks_like_base58_address(token: str) -> bool:
    """Guard against ordinary words matching the base58 alphabet.

    A real Solana address mixes cases and digits; an English word of the same
    length almost never does.
    """
    return any(c.isdigit() for c in token) and any(c.isupper() for c in token)


def addresses_in(line: str, chain_hint: str | None = None) -> list[tuple[str, str]]:
    """Return (address, chain) pairs found on one line, in order."""
    urls = _URL_RE.findall(line)
    chain = next((c for c in (chains.chain_from_url(u) for u in urls) if c), None)
    chain = chain or chain_hint

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    # Splitting on non-alphanumerics also breaks URLs into path segments, so an
    # address that only appears inside an explorer link is still picked up.
    for token in _TOKEN_RE.split(line):
        parsed = chains.normalise_address(token)
        if not parsed:
            continue
        address, kind = parsed
        if kind == "svm" and not _looks_like_base58_address(token):
            continue
        if address in seen:
            continue
        seen.add(address)
        resolved = chain or ("solana" if kind == "svm" else chains.DEFAULT_CHAIN)
        if resolved not in chains.CHAINS:
            resolved = chains.DEFAULT_CHAIN
        found.append((address, resolved))
    return found


def _rank_and_pnl(line: str, address: str) -> tuple[float | None, float | None]:
    """Read a leading rank and a dollar amount off a pasted table row."""
    without_address = line.replace(address, " ")
    rank: float | None = None
    pnl: float | None = None
    for match in _NUMBER_RE.finditer(without_address):
        raw = match.group()
        value = float(raw.lstrip("$").replace(",", "") or 0)
        if raw.startswith("$") or abs(value) >= 1000:
            if pnl is None or abs(value) > abs(pnl):
                pnl = value
        elif rank is None and 0 < value < 1000 and "." not in raw:
            rank = value
    return rank, pnl


def parse(
    text: str,
    *,
    source: str = "pasted",
    tag: str = "",
    chain: str | None = None,
) -> list[Wallet]:
    """Build wallets from pasted text, one line at a time."""
    wallets: list[Wallet] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        urls = _URL_RE.findall(line)
        for address, resolved in addresses_in(line, chain):
            rank, pnl = _rank_and_pnl(line, address)
            wallets.append(
                Wallet(
                    address=address,
                    chain=chain if chain in chains.CHAINS else resolved,
                    source=source,
                    tag=tag,
                    rank=rank,
                    pnl_usd=pnl,
                    url=urls[0] if urls else "",
                )
            )
    return wallets


def parse_many(lines: Iterable[str], **kwargs: object) -> list[Wallet]:
    return parse("\n".join(lines), **kwargs)  # type: ignore[arg-type]
