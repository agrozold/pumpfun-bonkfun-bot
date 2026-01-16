"""
BAGS Migration Tracker - monitors DBC pool migrations to DAMM v2.

BAGS tokens use Meteora DBC (Dynamic Bonding Curve) which automatically
migrates to DAMM v2 when the bonding curve reaches its threshold.

This module:
1. Monitors migration events from Meteora DBC program
2. Tracks old pool -> new pool mappings
3. Triggers fallback to Jupiter/DAMM v2 for migrated tokens
"""

import asyncio
import base64
import json
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import aiohttp
from solders.pubkey import Pubkey

from utils.logger import get_logger

logger = get_logger(__name__)

# Meteora DBC Program ID
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
# Meteora DAMM v2 Program ID
METEORA_DAMM_V2_PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"

# Migration event discriminators (Anchor convention)
# EvtMigrateMeteoraDamm and EvtMigrateMeteoraDammV2
MIGRATE_DAMM_DISCRIMINATOR = b"\x00"  # Placeholder - update with actual
MIGRATE_DAMM_V2_DISCRIMINATOR = b"\x00"  # Placeholder - update with actual


@dataclass
class MigrationInfo:
    """Information about a migrated BAGS token."""
    
    base_mint: Pubkey
    quote_mint: Pubkey
    old_pool: Pubkey  # DBC virtual pool
    new_pool: Pubkey  # DAMM v2 pool
    migration_timestamp: datetime
    migration_tx: str | None = None


@dataclass
class BagsMigrationTracker:
    """Tracks BAGS token migrations from DBC to DAMM v2.
    
    When a BAGS token's bonding curve reaches its threshold, Meteora's
    migration keepers automatically migrate it to DAMM v2. This tracker
    monitors these migrations and maintains a mapping for fallback trading.
    """
    
    wss_endpoint: str
    rpc_endpoint: str | None = None
    on_migration_callback: Callable[[MigrationInfo], None] | None = None
    
    # Migration mappings: base_mint -> MigrationInfo
    migrations: dict[str, MigrationInfo] = field(default_factory=dict)
    
    # Track pools we're monitoring
    _monitored_pools: set[str] = field(default_factory=set)
    _running: bool = False
    _ws_task: asyncio.Task | None = None
    
    def __post_init__(self):
        """Initialize the tracker."""
        logger.info(f"BagsMigrationTracker initialized")
        logger.info(f"Monitoring DBC program: {METEORA_DBC_PROGRAM}")
        logger.info(f"Target DAMM v2 program: {METEORA_DAMM_V2_PROGRAM}")
    
    def add_pool_to_monitor(self, pool_address: str, base_mint: str) -> None:
        """Add a DBC pool to monitor for migration.
        
        Args:
            pool_address: DBC virtual pool address
            base_mint: Base token mint address
        """
        self._monitored_pools.add(pool_address)
        logger.info(f"Monitoring pool {pool_address[:8]}... for migration (mint: {base_mint[:8]}...)")
    
    def is_token_migrated(self, base_mint: str) -> bool:
        """Check if a token has been migrated.
        
        Args:
            base_mint: Base token mint address
            
        Returns:
            True if token has migrated to DAMM v2
        """
        return base_mint in self.migrations
    
    def get_migration_info(self, base_mint: str) -> MigrationInfo | None:
        """Get migration info for a token.
        
        Args:
            base_mint: Base token mint address
            
        Returns:
            MigrationInfo if migrated, None otherwise
        """
        return self.migrations.get(base_mint)
    
    def get_new_pool_address(self, base_mint: str) -> Pubkey | None:
        """Get the new DAMM v2 pool address for a migrated token.
        
        Args:
            base_mint: Base token mint address
            
        Returns:
            New pool address if migrated, None otherwise
        """
        info = self.migrations.get(base_mint)
        return info.new_pool if info else None
    
    async def start(self) -> None:
        """Start monitoring for migrations."""
        if self._running:
            logger.warning("Migration tracker already running")
            return
        
        self._running = True
        self._ws_task = asyncio.create_task(self._monitor_migrations())
        logger.info("Migration tracker started")
    
    async def stop(self) -> None:
        """Stop monitoring for migrations."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        logger.info("Migration tracker stopped")
    
    async def _monitor_migrations(self) -> None:
        """Monitor DBC program for migration events via WebSocket."""
        import websockets
        
        while self._running:
            try:
                async with websockets.connect(
                    self.wss_endpoint,
                    ping_interval=30,
                    ping_timeout=60,
                    close_timeout=10,
                ) as ws:
                    # Subscribe to DBC program logs
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [METEORA_DBC_PROGRAM]},
                            {"commitment": "confirmed"}
                        ]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"Subscribed to DBC program logs for migration events")
                    
                    while self._running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(message)
                            
                            if "method" in data and data["method"] == "logsNotification":
                                await self._process_log_notification(data)
                                
                        except asyncio.TimeoutError:
                            # Send ping to keep connection alive
                            await ws.ping()
                        except Exception as e:
                            logger.warning(f"Error processing migration log: {e}")
                            
            except Exception as e:
                logger.error(f"Migration tracker WebSocket error: {e}")
                if self._running:
                    await asyncio.sleep(5)  # Reconnect delay
    
    async def _process_log_notification(self, data: dict) -> None:
        """Process a log notification for migration events.
        
        Args:
            data: WebSocket notification data
        """
        try:
            result = data.get("params", {}).get("result", {})
            value = result.get("value", {})
            logs = value.get("logs", [])
            signature = value.get("signature", "")
            
            # Look for migration event indicators in logs
            is_migration = False
            for log in logs:
                if "migrate" in log.lower() or "MigrateMeteoraDamm" in log:
                    is_migration = True
                    break
            
            if not is_migration:
                return
            
            logger.info(f"[MIGRATE] Potential migration detected: {signature}")
            
            # Parse migration event from Program data logs
            for log in logs:
                if "Program data:" in log:
                    await self._parse_migration_event(log, signature)
                    
        except Exception as e:
            logger.debug(f"Error processing log notification: {e}")
    
    async def _parse_migration_event(self, log: str, signature: str) -> None:
        """Parse migration event from Program data log.
        
        Args:
            log: Log line containing Program data
            signature: Transaction signature
        """
        try:
            # Extract base64 encoded data
            encoded_data = log.split("Program data: ")[1].strip()
            decoded_data = base64.b64decode(encoded_data)
            
            if len(decoded_data) < 8:
                return
            
            # Check discriminator (first 8 bytes)
            discriminator = decoded_data[:8]
            
            # Parse based on event type
            # EvtMigrateMeteoraDammV2 structure:
            # - virtualPool: Pubkey (32 bytes)
            # - migrationMetadata: Pubkey (32 bytes)
            # - pool: Pubkey (32 bytes) - new DAMM v2 pool
            # - baseMint: Pubkey (32 bytes)
            # - quoteMint: Pubkey (32 bytes)
            
            if len(decoded_data) >= 8 + 32 * 5:
                offset = 8
                virtual_pool = Pubkey.from_bytes(decoded_data[offset:offset+32])
                offset += 32
                migration_metadata = Pubkey.from_bytes(decoded_data[offset:offset+32])
                offset += 32
                new_pool = Pubkey.from_bytes(decoded_data[offset:offset+32])
                offset += 32
                base_mint = Pubkey.from_bytes(decoded_data[offset:offset+32])
                offset += 32
                quote_mint = Pubkey.from_bytes(decoded_data[offset:offset+32])
                
                # Create migration info
                migration_info = MigrationInfo(
                    base_mint=base_mint,
                    quote_mint=quote_mint,
                    old_pool=virtual_pool,
                    new_pool=new_pool,
                    migration_timestamp=datetime.utcnow(),
                    migration_tx=signature,
                )
                
                # Store migration
                base_mint_str = str(base_mint)
                self.migrations[base_mint_str] = migration_info
                
                logger.info(f"[OK] Migration recorded:")
                logger.info(f"   Base mint: {base_mint}")
                logger.info(f"   Old pool (DBC): {virtual_pool}")
                logger.info(f"   New pool (DAMM v2): {new_pool}")
                logger.info(f"   TX: {signature}")
                
                # Trigger callback
                if self.on_migration_callback:
                    try:
                        self.on_migration_callback(migration_info)
                    except Exception as e:
                        logger.error(f"Migration callback error: {e}")
                        
        except Exception as e:
            logger.debug(f"Failed to parse migration event: {e}")
    
    async def check_pool_status(self, pool_address: str) -> dict | None:
        """Check if a DBC pool has migrated by fetching its status.
        
        Args:
            pool_address: DBC virtual pool address
            
        Returns:
            Pool status dict or None if error
        """
        if not self.rpc_endpoint:
            return None
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getAccountInfo",
                    "params": [
                        pool_address,
                        {"encoding": "base64"}
                    ]
                }
                
                async with session.post(self.rpc_endpoint, json=payload) as resp:
                    if resp.status != 200:
                        return None
                    
                    result = await resp.json()
                    account_data = result.get("result", {}).get("value")
                    
                    if not account_data:
                        # Account doesn't exist - pool may have migrated
                        return {"status": "migrated_or_closed"}
                    
                    # Parse pool status from account data
                    data = base64.b64decode(account_data["data"][0])
                    
                    # Status byte is typically at a specific offset
                    # For Meteora DBC, status 0 = active, other values = migrated/closed
                    # This offset may need adjustment based on actual pool structure
                    if len(data) > 100:
                        status_byte = data[100]  # Approximate offset
                        return {
                            "status": "active" if status_byte == 0 else "migrated",
                            "status_byte": status_byte
                        }
                    
                    return {"status": "unknown"}
                    
        except Exception as e:
            logger.debug(f"Error checking pool status: {e}")
            return None
    
    def clear_migrations(self) -> None:
        """Clear all stored migrations."""
        self.migrations.clear()
        logger.info("Migration cache cleared")
