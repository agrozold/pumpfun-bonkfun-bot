"""
Pump.Fun platform exports.

This module provides convenient imports for the pump.fun platform implementations.
Platform registration is now handled by the main platform factory.
"""

from .address_provider import PumpFunAddressProvider
from .curve_manager import PumpFunCurveManager
from .event_parser import PumpFunEventParser
from .instruction_builder import PumpFunInstructionBuilder
from .pumpportal_processor import PumpFunPumpPortalProcessor

# Backward compatibility aliases
PumpFunAddresses = PumpFunAddressProvider

# Export implementations for direct use if needed
__all__ = [
    "PumpFunAddressProvider",
    "PumpFunAddresses",  # Alias
    "PumpFunCurveManager",
    "PumpFunEventParser",
    "PumpFunInstructionBuilder",
    "PumpFunPumpPortalProcessor",
]
