"""
Fetch price for BAGS platform tokens.

BAGS tokens are identified by mint addresses ending with "bags".
BAGS uses Meteora DBC (Dynamic Bonding Curve) for token trading:
- DBC Program ID: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
- VirtualPool structure with baseReserve, quoteReserve, sqrtPrice

This example demonstrates how to:
1. Identify BAGS tokens by address suffix
2. Fetch VirtualPool state from Meteora DBC
3. Calculate token price from reserves
"""

import asyncio
import os
import struct
import sys
from typing import Final

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from platforms.bags.address_provider import (
    BagsAddresses,
    BagsAddressProvider,
    is_bags_token,
)

LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS: Final[int] = 6

# Meteora DBC Program ID (used by BAGS)
BAGS_DBC_PROGRAM_ID: Final[str] = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"

# Example BAGS token mint address (replace with actual)
# BAGS tokens end with "bags" suffix
EXAMPLE_BAGS_MINT: Final[str] = "ExampleTokenMintAddressEndingWithbags"

RPC_ENDPOINT = os.environ.get("SOLANA_NODE_RPC_ENDPOINT")


class MeteoraDBCVirtualPool:
    """Parse Meteora DBC VirtualPool account data.
    
    VirtualPool structure from IDL:
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
    """

    DISCRIMINATOR_SIZE = 8
    PUBKEY_SIZE = 32
    U128_SIZE = 16
    U64_SIZE = 8
    I32_SIZE = 4
    U8_SIZE = 1
    I64_SIZE = 8

    def __init__(self, data: bytes) -> None:
        """Parse VirtualPool data."""
        offset = self.DISCRIMINATOR_SIZE
        
        # Read config (publicKey - 32 bytes)
        self.config = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
        offset += self.PUBKEY_SIZE
        
        # Read creator (publicKey - 32 bytes)
        self.creator = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
        offset += self.PUBKEY_SIZE
        
        # Read baseMint (publicKey - 32 bytes)
        self.base_mint = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
        offset += self.PUBKEY_SIZE
        
        # Read quoteMint (publicKey - 32 bytes)
        self.quote_mint = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
        offset += self.PUBKEY_SIZE
        
        # Read baseVault (publicKey - 32 bytes)
        self.base_vault = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
        offset += self.PUBKEY_SIZE
        
        # Read quoteVault (publicKey - 32 bytes)
        self.quote_vault = Pubkey.from_bytes(data[offset:offset + self.PUBKEY_SIZE])
        offset += self.PUBKEY_SIZE
        
        # Read sqrtPrice (u128 - 16 bytes, little-endian)
        self.sqrt_price = int.from_bytes(data[offset:offset + self.U128_SIZE], "little")
        offset += self.U128_SIZE
        
        # Read baseReserve (u64 - 8 bytes)
        self.base_reserve = struct.unpack_from("<Q", data, offset)[0]
        offset += self.U64_SIZE
        
        # Read quoteReserve (u64 - 8 bytes)
        self.quote_reserve = struct.unpack_from("<Q", data, offset)[0]
        offset += self.U64_SIZE
        
        # Read activeBin (i32 - 4 bytes)
        self.active_bin = struct.unpack_from("<i", data, offset)[0]
        offset += self.I32_SIZE
        
        # Read status (u8 - 1 byte) - 0 = active, other = migrated/closed
        self.status = data[offset]
        offset += self.U8_SIZE
        
        # Read bump (u8 - 1 byte)
        self.bump = data[offset]
        offset += self.U8_SIZE
        
        # Read createdAt (i64 - 8 bytes)
        self.created_at = struct.unpack_from("<q", data, offset)[0]
        offset += self.I64_SIZE
        
        # Read migrationThreshold (u64 - 8 bytes)
        self.migration_threshold = struct.unpack_from("<Q", data, offset)[0]

    @property
    def is_active(self) -> bool:
        """Check if pool is active (not migrated)."""
        return self.status == 0

    @property
    def is_migrated(self) -> bool:
        """Check if pool has migrated to DAMM v2."""
        return self.status != 0


async def get_bags_pool_state(
    conn: AsyncClient, pool_address: Pubkey
) -> MeteoraDBCVirtualPool:
    """Fetch and parse BAGS VirtualPool state.
    
    Args:
        conn: Solana RPC client
        pool_address: VirtualPool account address
        
    Returns:
        Parsed VirtualPool state
    """
    response = await conn.get_account_info(pool_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError(f"Invalid pool state: No data for {pool_address}")

    data = response.value.data
    return MeteoraDBCVirtualPool(data)


def calculate_bags_price(pool_state: MeteoraDBCVirtualPool) -> float:
    """Calculate token price from VirtualPool reserves.
    
    Price = quoteReserve / baseReserve (SOL per token)
    
    Args:
        pool_state: Parsed VirtualPool state
        
    Returns:
        Token price in SOL
    """
    if pool_state.base_reserve <= 0 or pool_state.quote_reserve <= 0:
        raise ValueError("Invalid reserve state")

    # Price in lamports per raw token
    price_lamports = pool_state.quote_reserve / pool_state.base_reserve
    # Convert to SOL per token (with decimals)
    price_sol = price_lamports * (10**TOKEN_DECIMALS) / LAMPORTS_PER_SOL
    
    return price_sol


def check_is_bags_token(mint_address: str) -> bool:
    """Check if a token is a BAGS token by address suffix.
    
    Args:
        mint_address: Token mint address string
        
    Returns:
        True if token ends with "bags"
    """
    return is_bags_token(mint_address)


async def main() -> None:
    """Main entry point - demonstrates BAGS price fetching."""
    if not RPC_ENDPOINT:
        print("Error: SOLANA_NODE_RPC_ENDPOINT environment variable not set")
        return

    print("=" * 60)
    print("BAGS Token Price Fetcher")
    print("=" * 60)
    print(f"Meteora DBC Program ID: {BAGS_DBC_PROGRAM_ID}")
    print(f"DAMM v2 Program ID: cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG")
    print()

    # Check if example mint is a BAGS token
    print(f"Checking if '{EXAMPLE_BAGS_MINT}' is a BAGS token...")
    is_bags = check_is_bags_token(EXAMPLE_BAGS_MINT)
    print(f"  Is BAGS token: {is_bags}")
    print()

    # Example: Check some addresses
    test_addresses = [
        "SomeRandomTokenMintAddress123456789",
        "AnotherTokenEndingWithbags",
        "NotABagsToken",
        "TestTokenMintBags",  # Case insensitive
    ]
    
    print("Testing BAGS token identification:")
    for addr in test_addresses:
        result = check_is_bags_token(addr)
        suffix = addr[-10:] if len(addr) > 10 else addr
        print(f"  ...{suffix}: {result}")
    print()

    # To fetch actual price, you need a real BAGS token mint address
    # Uncomment and modify the following when you have a real token:
    #
    # try:
    #     async with AsyncClient(RPC_ENDPOINT) as conn:
    #         mint = Pubkey.from_string(EXAMPLE_BAGS_MINT)
    #         
    #         # Derive pool address (requires config from token creation event)
    #         address_provider = BagsAddressProvider()
    #         pool_address = address_provider.derive_pool_address(mint)
    #         
    #         print(f"Fetching price for BAGS token: {mint}")
    #         print(f"VirtualPool address: {pool_address}")
    #         
    #         pool_state = await get_bags_pool_state(conn, pool_address)
    #         price = calculate_bags_price(pool_state)
    #         
    #         print(f"\nVirtualPool State:")
    #         print(f"  Base Reserve:  {pool_state.base_reserve:,}")
    #         print(f"  Quote Reserve: {pool_state.quote_reserve:,}")
    #         print(f"  Status:        {'Active' if pool_state.is_active else 'Migrated'}")
    #         print(f"  Created At:    {pool_state.created_at}")
    #         print(f"\nToken price: {price:.10f} SOL")
    #         
    # except ValueError as e:
    #     print(f"Error: {e}")
    # except Exception as e:
    #     print(f"An unexpected error occurred: {e}")

    print("Note: To fetch actual prices, provide a real BAGS token mint address")
    print("BAGS tokens are identified by mint addresses ending with 'bags'")
    print()
    print("Pool PDA derivation requires 'config' from token creation event:")
    print("  seeds = [baseMint, quoteMint, config]")


if __name__ == "__main__":
    asyncio.run(main())
