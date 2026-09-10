"""Provider interface.

A provider answers one question for one chain: which tokens did this wallet
mint since block N?
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..models import MintEvent


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot serve a chain (no key, unsupported net)."""


@runtime_checkable
class MintProvider(Protocol):
    name: str

    def supports(self, chain: str) -> bool: ...

    def fetch_mints(
        self, chain: str, wallet: str, *, since_block: int = 0, lookback_hours: int = 24
    ) -> list[MintEvent]: ...


def iso_to_unix(value: str | None) -> int:
    """Parse the ISO-8601 timestamps providers return; 0 when absent."""
    if not value:
        return 0
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def hex_to_int(value: str | int | None, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = value.strip()
    try:
        return int(text, 16) if text.startswith("0x") else int(text)
    except ValueError:
        return default
