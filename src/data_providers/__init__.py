"""
Data providers for token analysis.

Usage:
    from data_providers import get_holder_provider, HolderAnalysis
    
    provider = await get_holder_provider()
    holders = await provider.get_holders(mint)
    security = await provider.get_security(mint)

Provider priority:
1. Custom Indexer (future) - set INDEXER_ENDPOINT
2. Birdeye API - set BIRDEYE_API_KEY
3. RPC (always available)
"""

from .holder_provider import (
    HolderProvider,
    HolderAnalysis, 
    TokenSecurityInfo,
    ProviderType,
)
from .holder_factory import (
    get_holder_provider,
    create_holder_provider,
    HolderProviderChain,
)
from .rpc_holder_provider import RPCHolderProvider
from .birdeye_provider import BirdeyeProvider

# Backward compatibility alias
from .birdeye_provider import BirdeyeProvider as HeliusHolderClient

__all__ = [
    "HolderProvider",
    "HolderAnalysis",
    "TokenSecurityInfo", 
    "ProviderType",
    "get_holder_provider",
    "create_holder_provider",
    "HolderProviderChain",
    "RPCHolderProvider",
    "BirdeyeProvider",
    "HeliusHolderClient",  # backward compat
]
