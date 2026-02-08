"""
Helius Webhook Synchronization - ensures whale addresses are always up-to-date.
Automatically syncs webhook on bot startup.
"""

import json
import logging
import os
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

HELIUS_API_BASE = "https://api.helius.xyz/v0/webhooks"


async def sync_helius_webhook(
    wallets_file: str = "smart_money_wallets.json",
    helius_api_key: str | None = None,
) -> bool:
    api_key = helius_api_key or os.getenv("HELIUS_API_KEY")
    if not api_key:
        logger.error("[HELIUS_SYNC] No HELIUS_API_KEY found!")
        return False
    
    wallets_path = Path(wallets_file)
    if not wallets_path.exists():
        logger.error(f"[HELIUS_SYNC] Wallets file not found: {wallets_path}")
        return False
    
    try:
        with open(wallets_path) as f:
            data = json.load(f)
        
        whale_addresses = [
            w.get("wallet") for w in data.get("whales", [])
            if w.get("wallet") and len(w.get("wallet", "")) > 30
        ]
        
        if not whale_addresses:
            logger.error("[HELIUS_SYNC] No valid whale addresses found!")
            return False
            
        logger.info(f"[HELIUS_SYNC] Loaded {len(whale_addresses)} whale addresses")
        
    except Exception as e:
        logger.exception(f"[HELIUS_SYNC] Error loading wallets: {e}")
        return False
    
    async with aiohttp.ClientSession() as session:
        try:
            url = f"{HELIUS_API_BASE}?api-key={api_key}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"[HELIUS_SYNC] GET webhooks failed: {error}")
                    return False
                webhooks = await resp.json()
                
            if not webhooks:
                logger.warning("[HELIUS_SYNC] No webhooks found!")
                return False
                
            webhook = webhooks[0]
            webhook_id = webhook.get("webhookID")
            current_addresses = set(webhook.get("accountAddresses", []))
            
            logger.info(f"[HELIUS_SYNC] Current webhook: {webhook_id}")
            logger.info(f"[HELIUS_SYNC] Current addresses: {len(current_addresses)}")
            
        except Exception as e:
            logger.exception(f"[HELIUS_SYNC] Error getting webhooks: {e}")
            return False
        
        expected_addresses = set(whale_addresses)
        
        if current_addresses == expected_addresses:
            logger.info(f"[HELIUS_SYNC] Webhook already synced with {len(expected_addresses)} addresses")
            return True
        
        logger.warning(f"[HELIUS_SYNC] ADDRESSES MISMATCH! Expected: {len(expected_addresses)}, Got: {len(current_addresses)}")
        
        try:
            update_url = f"{HELIUS_API_BASE}/{webhook_id}?api-key={api_key}"
            
            update_body = {
                "webhookURL": "http://212.113.112.103:8000/webhook",
                "transactionTypes": webhook.get("transactionTypes", ["SWAP"]),
                "accountAddresses": list(expected_addresses),
                "webhookType": webhook.get("webhookType", "enhanced"),
                "txnStatus": webhook.get("txnStatus", "all"),
            }
            
            logger.info(f"[HELIUS_SYNC] Updating webhook with {len(expected_addresses)} addresses...")
            
            async with session.put(update_url, json=update_body) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"[HELIUS_SYNC] PUT update failed: {error}")
                    return False
                    
                result = await resp.json()
                new_count = len(result.get("accountAddresses", []))
                
                logger.warning(f"[HELIUS_SYNC] WEBHOOK UPDATED! Now tracking {new_count} addresses")
                return True
                
        except Exception as e:
            logger.exception(f"[HELIUS_SYNC] Error updating webhook: {e}")
            return False


async def verify_webhook_addresses(helius_api_key: str | None = None, expected_count: int = 99):
    api_key = helius_api_key or os.getenv("HELIUS_API_KEY")
    if not api_key:
        return False, 0
    
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{HELIUS_API_BASE}?api-key={api_key}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, 0
                webhooks = await resp.json()
                
            if not webhooks:
                return False, 0
                
            actual = len(webhooks[0].get("accountAddresses", []))
            return actual >= expected_count, actual
            
    except Exception:
        return False, 0
