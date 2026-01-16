"""
BAGS implementation of CurveManager interface.

This module handles BAGS specific pool operations using Meteora DBC.

BAGS uses Meteora DBC (Dynamic Bonding Curve) for token trading:
- DBC Program ID: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
- VirtualPool state contains sqrtPrice, baseReserve, quoteReserve for price calculation
"""

import struct
from typing import Any

from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.pubkeys import LAMPORTS_PER_SOL, TOKEN_DECIMALS
from interfaces.core import CurveManager, Platform
from platforms.bags.address_provider import BagsAddressProvider
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class BagsCurveManager(CurveManager):
    """BAGS implementation of CurveManager interface using Meteora DBC VirtualPool."""

    # VirtualPool account structure offsets (based on IDL)
    # 8 bytes discriminator + fields
    DISCRIMINATOR_SIZE = 8
    PUBKEY_SIZE = 32
    U128_SIZE = 16
    U64_SIZE = 8
    I32_SIZE = 4
    U8_SIZE = 1
    I64_SIZE = 8

    def __init__(self, client: SolanaClient, idl_parser: IDLParser | None = None):
        """Initialize BAGS curve manager with optional IDL parser.

        Args:
            client: Solana RPC client
            idl_parser: Pre-loaded IDL parser for BAGS platform (optional)
        """
        self.client = client
        self.address_provider = BagsAddressProvider()
        self._idl_parser = idl_parser

        if idl_parser:
            logger.info("BAGS curve manager initialized with IDL parser")
        else:
            logger.info("BAGS curve manager initialized without IDL (using manual decoding)")

    @property
    def platform(self) -> Platform:
        """Get the platform this manager serves."""
        return Platform.BAGS

    async def get_pool_state(self, pool_address: Pubkey) -> dict[str, Any]:
        """Get the current state of a BAGS VirtualPool.

        Args:
            pool_address: Address of the VirtualPool account

        Returns:
            Dictionary containing pool state data
        """
        try:
            account = await self.client.get_account_info(pool_address)
            if not account.data:
                raise ValueError(f"No data in pool state account {pool_address}")

            # Decode pool state
            if self._idl_parser:
                pool_state_data = self._decode_pool_state_with_idl(account.data)
            else:
                pool_state_data = self._decode_pool_state_manual(account.data)

            return pool_state_data

        except Exception as e:
            logger.exception("Failed to get pool state")
            raise ValueError(f"Invalid pool state: {e!s}")

    async def calculate_price(self, pool_address: Pubkey) -> float:
        """Calculate current token price from VirtualPool state.

        Args:
            pool_address: Address of the VirtualPool

        Returns:
            Current token price in SOL
        """
        pool_state = await self.get_pool_state(pool_address)

        base_reserve = pool_state["base_reserve"]
        quote_reserve = pool_state["quote_reserve"]

        if base_reserve <= 0 or quote_reserve <= 0:
            raise ValueError("Invalid reserve state")

        # Price = quote_reserves / base_reserves (how much SOL per token)
        price_lamports = quote_reserve / base_reserve
        price_sol = price_lamports * (10**TOKEN_DECIMALS) / LAMPORTS_PER_SOL

        return price_sol

    async def calculate_buy_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate expected tokens received for a buy operation.

        Uses the constant product AMM formula.

        Args:
            pool_address: Address of the VirtualPool
            amount_in: Amount of SOL to spend (in lamports)

        Returns:
            Expected amount of tokens to receive (in raw token units)
        """
        pool_state = await self.get_pool_state(pool_address)

        base_reserve = pool_state["base_reserve"]
        quote_reserve = pool_state["quote_reserve"]

        # Constant product formula: tokens_out = (amount_in * base_reserve) / (quote_reserve + amount_in)
        numerator = amount_in * base_reserve
        denominator = quote_reserve + amount_in

        if denominator == 0:
            return 0

        tokens_out = numerator // denominator
        return tokens_out

    async def calculate_sell_amount_out(
        self, pool_address: Pubkey, amount_in: int
    ) -> int:
        """Calculate expected SOL received for a sell operation.

        Uses the constant product AMM formula.

        Args:
            pool_address: Address of the VirtualPool
            amount_in: Amount of tokens to sell (in raw token units)

        Returns:
            Expected amount of SOL to receive (in lamports)
        """
        pool_state = await self.get_pool_state(pool_address)

        base_reserve = pool_state["base_reserve"]
        quote_reserve = pool_state["quote_reserve"]

        # Constant product formula: sol_out = (amount_in * quote_reserve) / (base_reserve + amount_in)
        numerator = amount_in * quote_reserve
        denominator = base_reserve + amount_in

        if denominator == 0:
            return 0

        sol_out = numerator // denominator
        return sol_out

    async def get_reserves(self, pool_address: Pubkey) -> tuple[int, int]:
        """Get current pool reserves.

        Args:
            pool_address: Address of the VirtualPool

        Returns:
            Tuple of (base_reserve, quote_reserve) in raw units
        """
        pool_state = await self.get_pool_state(pool_address)
        return (pool_state["base_reserve"], pool_state["quote_reserve"])

    def _decode_pool_state_with_idl(self, data: bytes) -> dict[str, Any]:
        """Decode VirtualPool state data using injected IDL parser.

        Args:
            data: Raw account data

        Returns:
            Dictionary with decoded pool state

        Raises:
            ValueError: If IDL parsing fails
        """
        # Use injected IDL parser to decode VirtualPool account data
        decoded_pool_state = self._idl_parser.decode_account_data(
            data, "VirtualPool", skip_discriminator=True
        )

        if not decoded_pool_state:
            raise ValueError("Failed to decode pool state with IDL parser")

        # Extract the fields we need for trading calculations
        pool_data = {
            "config": decoded_pool_state.get("config"),
            "creator": decoded_pool_state.get("creator"),
            "base_mint": decoded_pool_state.get("baseMint"),
            "quote_mint": decoded_pool_state.get("quoteMint"),
            "base_vault": decoded_pool_state.get("baseVault"),
            "quote_vault": decoded_pool_state.get("quoteVault"),
            "sqrt_price": decoded_pool_state.get("sqrtPrice", 0),
            "base_reserve": decoded_pool_state.get("baseReserve", 0),
            "quote_reserve": decoded_pool_state.get("quoteReserve", 0),
            "active_bin": decoded_pool_state.get("activeBin", 0),
            "status": decoded_pool_state.get("status", 0),
            "bump": decoded_pool_state.get("bump", 0),
            "created_at": decoded_pool_state.get("createdAt", 0),
            "migration_threshold": decoded_pool_state.get("migrationThreshold", 0),
        }

        # Validate reserves are positive before calculating price
        if pool_data["base_reserve"] <= 0:
            raise ValueError(
                f"Invalid base_reserve: {pool_data['base_reserve']} - cannot calculate price"
            )
        if pool_data["quote_reserve"] <= 0:
            raise ValueError(
                f"Invalid quote_reserve: {pool_data['quote_reserve']} - cannot calculate price"
            )

        pool_data["price_per_token"] = (
            (pool_data["quote_reserve"] / pool_data["base_reserve"])
            * (10**TOKEN_DECIMALS)
            / LAMPORTS_PER_SOL
        )

        logger.debug(
            f"Decoded VirtualPool: base_reserve={pool_data['base_reserve']}, "
            f"quote_reserve={pool_data['quote_reserve']}, "
            f"price={pool_data['price_per_token']:.8f} SOL, "
            f"status={pool_data['status']}"
        )

        return pool_data

    def _decode_pool_state_manual(self, data: bytes) -> dict[str, Any]:
        """Decode VirtualPool state data manually without IDL.

        Based on IDL VirtualPool structure:
        - config: publicKey (32 bytes)
        - creator: publicKey (32 bytes)
        - baseMint: publicKey (32 bytes)
        - quoteMint: publicKey (32 bytes)
        - baseVault: publicKey (32 bytes)
        - quoteVault: publicKey (32 bytes)
        - sqrtPrice: u128 (16 bytes)
        - baseReserve: u64 (8 bytes)
        - quoteReserve: u64 (8 bytes)
        - activeBin: i32 (4 bytes)
        - status: u8 (1 byte)
        - bump: u8 (1 byte)
        - createdAt: i64 (8 bytes)
        - migrationThreshold: u64 (8 bytes)

        Args:
            data: Raw account data

        Returns:
            Dictionary with decoded pool state

        Raises:
            ValueError: If manual parsing fails
        """
        try:
            # Skip 8-byte discriminator
            offset = self.DISCRIMINATOR_SIZE

            # Read config (publicKey - 32 bytes)
            config = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
            offset += self.PUBKEY_SIZE

            # Read creator (publicKey - 32 bytes)
            creator = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
            offset += self.PUBKEY_SIZE

            # Read baseMint (publicKey - 32 bytes)
            base_mint = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
            offset += self.PUBKEY_SIZE

            # Read quoteMint (publicKey - 32 bytes)
            quote_mint = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
            offset += self.PUBKEY_SIZE

            # Read baseVault (publicKey - 32 bytes)
            base_vault = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
            offset += self.PUBKEY_SIZE

            # Read quoteVault (publicKey - 32 bytes)
            quote_vault = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
            offset += self.PUBKEY_SIZE

            # Read sqrtPrice (u128 - 16 bytes, little-endian)
            sqrt_price = int.from_bytes(data[offset:offset + self.U128_SIZE], "little")
            offset += self.U128_SIZE

            # Read baseReserve (u64 - 8 bytes)
            base_reserve = struct.unpack_from("<Q", data, offset)[0]
            offset += self.U64_SIZE

            # Read quoteReserve (u64 - 8 bytes)
            quote_reserve = struct.unpack_from("<Q", data, offset)[0]
            offset += self.U64_SIZE

            # Read activeBin (i32 - 4 bytes)
            active_bin = struct.unpack_from("<i", data, offset)[0]
            offset += self.I32_SIZE

            # Read status (u8 - 1 byte)
            status = data[offset]
            offset += self.U8_SIZE

            # Read bump (u8 - 1 byte)
            bump = data[offset]
            offset += self.U8_SIZE

            # Read createdAt (i64 - 8 bytes)
            created_at = struct.unpack_from("<q", data, offset)[0]
            offset += self.I64_SIZE

            # Read migrationThreshold (u64 - 8 bytes)
            migration_threshold = struct.unpack_from("<Q", data, offset)[0]

            pool_data = {
                "config": config,
                "creator": creator,
                "base_mint": base_mint,
                "quote_mint": quote_mint,
                "base_vault": base_vault,
                "quote_vault": quote_vault,
                "sqrt_price": sqrt_price,
                "base_reserve": base_reserve,
                "quote_reserve": quote_reserve,
                "active_bin": active_bin,
                "status": status,
                "bump": bump,
                "created_at": created_at,
                "migration_threshold": migration_threshold,
            }

            # Validate reserves are positive
            if pool_data["base_reserve"] <= 0:
                raise ValueError(
                    f"Invalid base_reserve: {pool_data['base_reserve']} - cannot calculate price"
                )
            if pool_data["quote_reserve"] <= 0:
                raise ValueError(
                    f"Invalid quote_reserve: {pool_data['quote_reserve']} - cannot calculate price"
                )

            pool_data["price_per_token"] = (
                (pool_data["quote_reserve"] / pool_data["base_reserve"])
                * (10**TOKEN_DECIMALS)
                / LAMPORTS_PER_SOL
            )

            logger.debug(
                f"Manually decoded VirtualPool: base_reserve={pool_data['base_reserve']}, "
                f"quote_reserve={pool_data['quote_reserve']}, "
                f"price={pool_data['price_per_token']:.8f} SOL, "
                f"status={pool_data['status']}"
            )

            return pool_data

        except Exception as e:
            raise ValueError(f"Failed to manually decode VirtualPool state: {e}")

    async def is_pool_migrated(self, pool_address: Pubkey) -> bool:
        """Check if pool has migrated to DAMM v2.
        
        Status values:
        - 0: Active (trading on DBC)
        - 1+: Migrated or closed
        
        Args:
            pool_address: Address of the VirtualPool
            
        Returns:
            True if pool has migrated
        """
        try:
            pool_state = await self.get_pool_state(pool_address)
            return pool_state.get("status", 0) != 0
        except Exception:
            # If we can't read pool, assume it might be migrated
            return True

    async def get_migration_threshold(self, pool_address: Pubkey) -> int:
        """Get the migration threshold for a pool.
        
        Args:
            pool_address: Address of the VirtualPool
            
        Returns:
            Migration threshold in quote token units (lamports)
        """
        pool_state = await self.get_pool_state(pool_address)
        return pool_state.get("migration_threshold", 0)

    async def validate_pool_state_structure(self, pool_address: Pubkey) -> bool:
        """Validate that the VirtualPool state structure matches expectations.

        Args:
            pool_address: Address of the VirtualPool

        Returns:
            True if structure is valid, False otherwise
        """
        try:
            pool_state = await self.get_pool_state(pool_address)

            required_fields = [
                "base_reserve",
                "quote_reserve",
                "status",
            ]

            for field in required_fields:
                if field not in pool_state:
                    logger.error(f"Missing required field: {field}")
                    return False

            return True

        except Exception:
            logger.exception("VirtualPool state validation failed")
            return False
