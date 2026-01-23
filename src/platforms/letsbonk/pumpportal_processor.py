"""
LetsBonk-specific PumpPortal event processor.
File: src/platforms/letsbonk/pumpportal_processor.py

FIXED: Now uses bondingCurveKey from PumpPortal as pool_state.
"""

from solders.pubkey import Pubkey

from interfaces.core import Platform, TokenInfo
from platforms.letsbonk.address_provider import LetsBonkAddressProvider
from utils.logger import get_logger

logger = get_logger(__name__)


class LetsBonkPumpPortalProcessor:
    """PumpPortal processor for LetsBonk tokens."""

    def __init__(self):
        """Initialize the processor with address provider."""
        self.address_provider = LetsBonkAddressProvider()

    @property
    def platform(self) -> Platform:
        """Get the platform this processor handles."""
        return Platform.LETS_BONK

    @property
    def supported_pool_names(self) -> list[str]:
        """Get the pool names this processor supports from PumpPortal."""
        return ["bonk"]

    def can_process(self, token_data: dict) -> bool:
        """Check if this processor can handle the given token data."""
        mint = token_data.get("mint", "").lower()
        if mint.endswith("bonk"):
            return True
        pool = token_data.get("pool", "").lower()
        return pool in self.supported_pool_names

    def process_token_data(self, token_data: dict) -> TokenInfo | None:
        """Process LetsBonk token data from PumpPortal.

        Args:
            token_data: Token data from PumpPortal WebSocket

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            # Log incoming data for debugging
            logger.info(f"[BONK] Processing token, keys: {list(token_data.keys())}")
            
            # Extract required fields - same structure as pump.fun!
            name = token_data.get("name", "")
            symbol = token_data.get("symbol", "")
            mint_str = token_data.get("mint")
            # CRITICAL FIX: Use bondingCurveKey as pool_state
            # PumpPortal sends 'pool' for bonk tokens (not bondingCurveKey like pump.fun)
            pool_raw = token_data.get("pool")
            bonding_key = token_data.get("bondingCurveKey")
            logger.info(f"[BONK-DEBUG] pool={pool_raw}, bondingCurveKey={bonding_key}")
            
            # pool field might be "bonk" string, not an address!
            # If pool is not a valid pubkey (32+ chars), derive it
            pool_state_str = None
            if bonding_key and len(str(bonding_key)) >= 32:
                pool_state_str = bonding_key
            elif pool_raw and len(str(pool_raw)) >= 32:
                pool_state_str = pool_raw
            creator_str = token_data.get("traderPublicKey")
            uri = token_data.get("uri", "")

            if not all([name, symbol, mint_str, creator_str]):
                logger.warning(
                    f"[BONK] Missing required fields: name={name}, symbol={symbol}, "
                    f"mint={mint_str}, creator={creator_str}"
                )
                if not creator_str:
                    creator_str = token_data.get("creator") or token_data.get("user")
                if not all([name, symbol, mint_str, creator_str]):
                    return None

            # Convert string addresses to Pubkey objects
            mint = Pubkey.from_string(mint_str)
            user = Pubkey.from_string(creator_str)
            creator = user

            # CRITICAL: Use pool_state from PumpPortal if available
            if pool_state_str:
                pool_state = Pubkey.from_string(pool_state_str)
                logger.info(f"[BONK] Using bondingCurveKey as pool_state: {pool_state_str[:20]}...")
            else:
                # Fallback to derivation (may be wrong for some tokens)
                pool_state = self.address_provider.derive_pool_address(mint)
                logger.warning(f"[BONK] No pool/bondingCurveKey, deriving pool_state (may fail!)")

            # Create temp TokenInfo to get additional accounts
            token_info_temp = TokenInfo(
                name=name,
                symbol=symbol,
                uri=uri,
                mint=mint,
                platform=Platform.LETS_BONK,
                pool_state=pool_state,
                user=user,
                creator=creator,
                base_vault=None,
                quote_vault=None,
            )
            
            additional_accounts = self.address_provider.get_additional_accounts(token_info_temp)
            base_vault = additional_accounts.get("base_vault")
            quote_vault = additional_accounts.get("quote_vault")

            return TokenInfo(
                name=name,
                symbol=symbol,
                uri=uri,
                mint=mint,
                platform=Platform.LETS_BONK,
                pool_state=pool_state,
                base_vault=base_vault,
                quote_vault=quote_vault,
                user=user,
                creator=creator,
            )

        except Exception:
            logger.exception("[BONK] Failed to process PumpPortal token data")
            return None
