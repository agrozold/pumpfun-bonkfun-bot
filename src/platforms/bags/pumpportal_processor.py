"""
BAGS-specific PumpPortal event processor.
File: src/platforms/bags/pumpportal_processor.py
"""

from solders.pubkey import Pubkey

from interfaces.core import Platform, TokenInfo
from platforms.bags.address_provider import BagsAddressProvider
from utils.logger import get_logger

logger = get_logger(__name__)


class BagsPumpPortalProcessor:
    """PumpPortal processor for BAGS tokens."""

    def __init__(self):
        """Initialize the processor with address provider."""
        self.address_provider = BagsAddressProvider()

    @property
    def platform(self) -> Platform:
        """Get the platform this processor handles."""
        return Platform.BAGS

    @property
    def supported_pool_names(self) -> list[str]:
        """Get the pool names this processor supports from PumpPortal."""
        return ["bags"]  # PumpPortal pool name for BAGS tokens

    def can_process(self, token_data: dict) -> bool:
        """Check if this processor can handle the given token data.

        Args:
            token_data: Token data from PumpPortal

        Returns:
            True if this processor can handle the token data
            
        Note:
            PumpPortal does NOT currently support bags.fm tokens directly.
            bags.fm tokens should be detected via logsSubscribe on Meteora DBC program.
            This processor is kept for potential future PumpPortal support.
        """
        # Check pool field from PumpPortal
        pool = token_data.get("pool", "").lower()
        if pool in self.supported_pool_names:
            return True
        
        # Check mint suffix as fallback (some bags tokens end with "bags")
        mint = token_data.get("mint", "").lower()
        if mint.endswith("bags"):
            return True
        
        return False

    def process_token_data(self, token_data: dict) -> TokenInfo | None:
        """Process BAGS token data from PumpPortal.

        Args:
            token_data: Token data from PumpPortal WebSocket

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            # Extract required fields for BAGS
            name = token_data.get("name", "")
            symbol = token_data.get("symbol", "")
            mint_str = token_data.get("mint")
            creator_str = token_data.get("traderPublicKey")
            uri = token_data.get("uri", "")

            if not all([name, symbol, mint_str, creator_str]):
                logger.warning(
                    "Missing required fields in PumpPortal BAGS token data"
                )
                return None

            # Convert string addresses to Pubkey objects
            mint = Pubkey.from_string(mint_str)
            user = Pubkey.from_string(creator_str)
            creator = user

            # Derive BAGS-specific addresses
            pool_state = self.address_provider.derive_pool_address(mint)

            # Get additional accounts
            additional_accounts = self.address_provider.get_additional_accounts(
                TokenInfo(
                    name=name,
                    symbol=symbol,
                    uri=uri,
                    mint=mint,
                    platform=Platform.BAGS,
                    pool_state=pool_state,
                    user=user,
                    creator=creator,
                    base_vault=None,
                    quote_vault=None,
                )
            )

            # Extract vault addresses
            base_vault = additional_accounts.get("base_vault")
            quote_vault = additional_accounts.get("quote_vault")

            return TokenInfo(
                name=name,
                symbol=symbol,
                uri=uri,
                mint=mint,
                platform=Platform.BAGS,
                pool_state=pool_state,
                base_vault=base_vault,
                quote_vault=quote_vault,
                user=user,
                creator=creator,
            )

        except Exception:
            logger.exception("Failed to process PumpPortal BAGS token data")
            return None
