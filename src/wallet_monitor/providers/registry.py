"""Pick a provider per chain, honouring which credentials are actually set."""

from __future__ import annotations

from ..config import Config
from .alchemy import AlchemyProvider
from .base import MintProvider
from .etherscan import EtherscanProvider
from .helius import HeliusProvider
from .rpc import RpcProvider


def build_providers(cfg: Config) -> list[MintProvider]:
    """Alchemy first (one key, richest data), then public RPC, then Etherscan.

    Public RPC outranks Etherscan because Etherscan V2 only indexes a fixed
    list of chain ids: asking it about Robinhood Chain (4663) fails rather than
    falling through, and the RPC is free and authoritative anyway.
    """
    providers: list[MintProvider] = []
    if cfg.alchemy_key:
        providers.append(AlchemyProvider(cfg))
    providers.append(RpcProvider(cfg))
    if cfg.etherscan_key:
        providers.append(EtherscanProvider(cfg))
    if cfg.helius_key:
        providers.append(HeliusProvider(cfg))
    return providers


def provider_for(providers: list[MintProvider], chain: str) -> MintProvider | None:
    for provider in providers:
        if provider.supports(chain):
            return provider
    return None


def rpc_provider(providers: list[MintProvider]) -> RpcProvider | None:
    """The sweep provider, which the scanner drives differently."""
    for provider in providers:
        if isinstance(provider, RpcProvider):
            return provider
    return None


def missing_credentials(cfg: Config) -> list[str]:
    """Keys that would widen coverage. Chains with a public RPC need none."""
    missing = []
    if not cfg.alchemy_key:
        missing.append("ALCHEMY_API_KEY (Ethereum, Base, Arbitrum, Abstract, Zora and friends)")
    if not cfg.helius_key:
        missing.append("HELIUS_API_KEY (Solana)")
    return missing
