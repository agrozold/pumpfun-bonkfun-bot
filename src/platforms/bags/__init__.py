"""
BAGS platform exports.

This module provides convenient imports for the BAGS platform implementations.
Platform registration is now handled by the main platform factory.

BAGS uses Meteora DBC (Dynamic Bonding Curve) for token trading:
- DBC Program ID: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
- DAMM v2 Program ID: cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG (post-migration)
- Token characteristic: mint addresses end with "bags"
"""

from .address_provider import BagsAddressProvider
from .curve_manager import BagsCurveManager
from .event_parser import BagsEventParser
from .instruction_builder import BagsInstructionBuilder
from .pumpportal_processor import BagsPumpPortalProcessor

# Export implementations for direct use if needed
__all__ = [
    "BagsAddressProvider",
    "BagsCurveManager",
    "BagsEventParser",
    "BagsInstructionBuilder",
    "BagsPumpPortalProcessor",
]
