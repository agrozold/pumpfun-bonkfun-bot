"""
Abstract interface for holder analysis providers.
Allows easy switching between Birdeye, RPC, or custom indexer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum


class ProviderType(Enum):
    RPC = "rpc"              # Native Solana RPC (always available)
    BIRDEYE = "birdeye"      # Birdeye API (temporary)
    INDEXER = "indexer"      # Custom indexer (future)


@dataclass
class HolderAnalysis:
    """Universal holder analysis result."""
    mint: str
    total_holders: int
    top_10_concentration: float
    top_holder_pct: float
    risk_level: str  # LOW, MEDIUM, HIGH
    is_concentrated: bool
    holders: List[dict]
    source: str  # Which provider returned this


@dataclass
class TokenSecurityInfo:
    """Token security information."""
    mint: str
    is_safe: bool
    risk_score: int  # 0-100
    warnings: List[str]
    details: dict
    source: str


class HolderProvider(ABC):
    """Abstract base class for holder data providers."""

    @property
    @abstractmethod
    def provider_type(self) -> ProviderType:
        """Return provider type."""
        pass

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider is available/configured."""
        pass

    @abstractmethod
    async def get_holders(self, mint: str, limit: int = 20) -> Optional[HolderAnalysis]:
        """Get holder distribution for a token."""
        pass

    @abstractmethod
    async def get_security(self, mint: str) -> Optional[TokenSecurityInfo]:
        """Get security info for a token."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Cleanup resources."""
        pass


def calculate_risk_level(top_10_pct: float, top_holder_pct: float) -> str:
    """Calculate risk level from holder concentration."""
    if top_10_pct > 70 or top_holder_pct > 30:
        return "HIGH"
    elif top_10_pct > 50 or top_holder_pct > 20:
        return "MEDIUM"
    return "LOW"
