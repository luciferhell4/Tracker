"""Pick a provider per chain, honouring which credentials are actually set."""

from __future__ import annotations

from ..config import Config
from .alchemy import AlchemyProvider
from .base import MintProvider
from .etherscan import EtherscanProvider
from .helius import HeliusProvider


def build_providers(cfg: Config) -> list[MintProvider]:
    """Alchemy first (one key, richest data), Etherscan for the gaps, Helius for Solana."""
    providers: list[MintProvider] = []
    if cfg.alchemy_key:
        providers.append(AlchemyProvider(cfg))
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


def missing_credentials(cfg: Config) -> list[str]:
    missing = []
    if not cfg.alchemy_key and not cfg.etherscan_key:
        missing.append("ALCHEMY_API_KEY or ETHERSCAN_API_KEY (EVM chains)")
    if not cfg.helius_key:
        missing.append("HELIUS_API_KEY (Solana)")
    return missing
