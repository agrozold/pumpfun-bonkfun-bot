"""
Holder Provider Factory - creates appropriate provider with fallback chain.

Priority:
1. Custom Indexer (future) - when INDEXER_ENDPOINT is set
2. Birdeye - when BIRDEYE_API_KEY is set
3. RPC - always available (fallback)

To switch to your indexer later, just set INDEXER_ENDPOINT in .env
"""

import os
import logging
from typing import Optional, List

from .holder_provider import HolderProvider, HolderAnalysis, TokenSecurityInfo, ProviderType
from .rpc_holder_provider import RPCHolderProvider
from .birdeye_provider import BirdeyeProvider

logger = logging.getLogger(__name__)


class HolderProviderChain:
    """
    Chain of holder providers with automatic fallback.
    Tries providers in order until one succeeds.
    """

    def __init__(self, providers: List[HolderProvider]):
        self.providers = [p for p in providers if p.is_available]
        if not self.providers:
            # Always have RPC as last resort
            self.providers = [RPCHolderProvider()]

        provider_names = [p.provider_type.value for p in self.providers]
        logger.info(f"[HolderChain] Active providers: {provider_names}")

    async def get_holders(self, mint: str, limit: int = 20) -> Optional[HolderAnalysis]:
        """Try each provider until one succeeds."""
        for provider in self.providers:
            try:
                result = await provider.get_holders(mint, limit)
                if result:
                    logger.debug(f"[HolderChain] {mint[:8]}... -> {result.source}")
                    return result
            except Exception as e:
                logger.debug(f"[HolderChain] {provider.provider_type.value} failed: {e}")
                continue
        return None

    async def get_security(self, mint: str) -> Optional[TokenSecurityInfo]:
        """Try each provider until one succeeds."""
        for provider in self.providers:
            try:
                result = await provider.get_security(mint)
                if result:
                    return result
            except Exception as e:
                logger.debug(f"[HolderChain] {provider.provider_type.value} security failed: {e}")
                continue
        return None

    async def close(self) -> None:
        for provider in self.providers:
            await provider.close()


def create_holder_provider() -> HolderProviderChain:
    """
    Factory function - creates provider chain based on available config.
    """
    providers: List[HolderProvider] = []

    # Birdeye (if API key available)
    if os.getenv("BIRDEYE_API_KEY"):
        providers.append(BirdeyeProvider())
        logger.info("[HolderFactory] Birdeye enabled")

    # RPC fallback (always available)
    providers.append(RPCHolderProvider())
    logger.info("[HolderFactory] RPC fallback enabled")

    return HolderProviderChain(providers)


# Singleton instance
_holder_provider: Optional[HolderProviderChain] = None


def get_holder_provider() -> HolderProviderChain:
    """Get singleton holder provider chain (sync version)."""
    global _holder_provider
    if _holder_provider is None:
        _holder_provider = create_holder_provider()
    return _holder_provider


async def get_holder_provider_async() -> HolderProviderChain:
    """Get singleton holder provider chain (async version)."""
    return get_holder_provider()


# Backward compatibility alias
HolderProviderFactory = create_holder_provider
