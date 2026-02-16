"""
Vault resolver — finds PumpSwap pool vault addresses for a token mint.

Used after Jupiter buy to resolve pool_base_vault / pool_quote_vault
for gRPC price stream subscription (Phase 4).

Logic reused from buy.py: get_program_accounts + DexScreener fallback.
"""

import asyncio
import logging
import os
import struct
from typing import Optional, Tuple

import aiohttp
import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import MemcmpOpts
from solders.pubkey import Pubkey

logger = logging.getLogger(__name__)

# PumpSwap constants
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
POOL_BASE_MINT_OFFSET = 43


def _parse_pool_data(data: bytes) -> dict:
    """Parse PumpSwap pool account data to extract vault addresses."""
    parsed = {}
    offset = 8  # Skip discriminator
    fields = [
        ("pool_bump", "u8"), ("index", "u16"), ("creator", "pubkey"),
        ("base_mint", "pubkey"), ("quote_mint", "pubkey"), ("lp_mint", "pubkey"),
        ("pool_base_token_account", "pubkey"), ("pool_quote_token_account", "pubkey"),
        ("lp_supply", "u64"), ("coin_creator", "pubkey"),
    ]
    for field_name, field_type in fields:
        if field_type == "pubkey":
            parsed[field_name] = base58.b58encode(data[offset:offset + 32]).decode("utf-8")
            offset += 32
        elif field_type in ("u64", "i64"):
            parsed[field_name] = struct.unpack("<Q", data[offset:offset + 8])[0]
            offset += 8
        elif field_type == "u16":
            parsed[field_name] = struct.unpack("<H", data[offset:offset + 2])[0]
            offset += 2
        elif field_type == "u8":
            parsed[field_name] = data[offset]
            offset += 1
    return parsed


async def _resolve_via_rpc(mint_str: str, rpc_url: str) -> Optional[Tuple[str, str, str]]:
    """Find PumpSwap pool via get_program_accounts.
    
    Returns (pool_base_vault, pool_quote_vault, pool_address) or None.
    """
    try:
        mint = Pubkey.from_string(mint_str)
        filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(mint))]
        
        async with AsyncClient(rpc_url, timeout=15) as client:
            response = await client.get_program_accounts(
                PUMP_AMM_PROGRAM_ID, encoding="base64",
                filters=filters
            )
            if not response.value:
                return None
            
            pool_pubkey = response.value[0].pubkey
            pool_data = response.value[0].account.data
            
            # Handle base64 tuple
            if isinstance(pool_data, tuple):
                import base64
                pool_data = base64.b64decode(pool_data[0])
            elif isinstance(pool_data, str):
                import base64
                pool_data = base64.b64decode(pool_data)
            
            parsed = _parse_pool_data(pool_data)
            base_vault = parsed.get("pool_base_token_account")
            quote_vault = parsed.get("pool_quote_token_account")
            
            if base_vault and quote_vault:
                logger.info(
                    f"[VAULT_RESOLVER] RPC found PumpSwap pool for {mint_str[:12]}...: "
                    f"pool={pool_pubkey}, base={base_vault[:12]}..., quote={quote_vault[:12]}..."
                )
                return base_vault, quote_vault, str(pool_pubkey)
    except Exception as e:
        logger.warning(f"[VAULT_RESOLVER] RPC resolve failed for {mint_str[:12]}...: {e}")
    return None


async def _resolve_via_dexscreener(mint_str: str, rpc_url: str) -> Optional[Tuple[str, str, str]]:
    """Find pool via DexScreener, then fetch vault data from RPC.
    
    Returns (pool_base_vault, pool_quote_vault, pool_address) or None.
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_str}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        
        pairs = data.get("pairs", [])
        if not pairs:
            return None
        
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return None
        
        # Prefer pumpswap
        pumpswap_pairs = [p for p in solana_pairs if "pumpswap" in p.get("dexId", "").lower()]
        if pumpswap_pairs:
            best = max(pumpswap_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
            pair_address = best.get("pairAddress")
            if not pair_address:
                return None
            
            logger.info(f"[VAULT_RESOLVER] DexScreener found PumpSwap pool: {pair_address}")
            
            # Fetch pool account data from RPC to get vault addresses
            pool_pubkey = Pubkey.from_string(pair_address)
            async with AsyncClient(rpc_url, timeout=15) as client:
                response = await client.get_account_info(pool_pubkey, encoding="base64")
                if not response.value or not response.value.data:
                    return None
                
                pool_data = response.value.data
                if isinstance(pool_data, tuple):
                    import base64
                    pool_data = base64.b64decode(pool_data[0])
                elif isinstance(pool_data, str):
                    import base64
                    pool_data = base64.b64decode(pool_data)
                
                parsed = _parse_pool_data(pool_data)
                base_vault = parsed.get("pool_base_token_account")
                quote_vault = parsed.get("pool_quote_token_account")
                
                if base_vault and quote_vault:
                    logger.info(
                        f"[VAULT_RESOLVER] DexScreener+RPC resolved: "
                        f"base={base_vault[:12]}..., quote={quote_vault[:12]}..."
                    )
                    return base_vault, quote_vault, pair_address
    except Exception as e:
        logger.warning(f"[VAULT_RESOLVER] DexScreener resolve failed for {mint_str[:12]}...: {e}")
    return None


async def resolve_vaults(mint_str: str) -> Optional[Tuple[str, str, str]]:
    """Resolve pool vault addresses for a token.
    
    Tries:
    1. RPC get_program_accounts (PumpSwap)
    2. DexScreener + RPC fallback
    
    Returns (pool_base_vault, pool_quote_vault, pool_address) or None.
    """
    # Pick best RPC (avoid Helius to save credits)
    rpc_url = (
        os.getenv("DRPC_RPC_ENDPOINT")
        or os.getenv("SOLANA_NODE_RPC_ENDPOINT")
        or os.getenv("CHAINSTACK_RPC_ENDPOINT")
        or os.getenv("ALCHEMY_RPC_ENDPOINT")
        or "https://api.mainnet-beta.solana.com"
    )
    
    # Method 1: Direct RPC lookup
    result = await _resolve_via_rpc(mint_str, rpc_url)
    if result:
        return result
    
    # Method 2: DexScreener fallback
    logger.info(f"[VAULT_RESOLVER] RPC found nothing, trying DexScreener for {mint_str[:12]}...")
    result = await _resolve_via_dexscreener(mint_str, rpc_url)
    if result:
        return result
    
    logger.warning(f"[VAULT_RESOLVER] Could not resolve vaults for {mint_str[:12]}...")
    return None
