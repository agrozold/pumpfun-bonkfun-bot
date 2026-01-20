"""
Specialized listener for bonk.fun (Raydium LaunchLab) tokens.

PumpPortal NOW supports bonk.fun (Jan 2026), but this listener provides direct RPC access
to Raydium LaunchLab program logs and parse the transactions.

This listener:
1. Subscribes to logsSubscribe for Raydium LaunchLab program
2. When a log mentions the program, fetches the full transaction
3. Parses the transaction to extract token creation data
"""

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable

import websockets
from solana.rpc.async_api import AsyncClient
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from interfaces.core import Platform, TokenInfo
from monitoring.base_listener import BaseTokenListener
from platforms.letsbonk.address_provider import LetsBonkAddresses
from utils.logger import get_logger

logger = get_logger(__name__)


class BonkLogsListener(BaseTokenListener):
    """Specialized listener for bonk.fun tokens via logsSubscribe + getTransaction."""

    def __init__(
        self,
        wss_endpoint: str,
        rpc_endpoint: str,
        raise_on_max_errors: bool = False,
        max_consecutive_errors: int = 5,
    ):
        """Initialize bonk.fun listener.

        Args:
            wss_endpoint: Solana WebSocket endpoint
            rpc_endpoint: Solana RPC HTTP endpoint for getTransaction
            raise_on_max_errors: If True, raise exception after max errors
            max_consecutive_errors: Max errors before raising/resetting
        """
        super().__init__()
        self.wss_endpoint = wss_endpoint
        self.rpc_endpoint = rpc_endpoint
        self.ping_interval = 20
        self.raise_on_max_errors = raise_on_max_errors
        self.max_consecutive_errors = max_consecutive_errors

        # Raydium LaunchLab program ID
        self.program_id = str(LetsBonkAddresses.PROGRAM)

        # Get event parser for LetsBonk
        from platforms import platform_factory
        from core.client import SolanaClient

        # Create minimal client for parser initialization
        class MinimalClient(SolanaClient):
            def __init__(self):
                self.rpc_endpoint = "http://dummy"
                self._client = None
                self._cached_blockhash = None
                self._blockhash_lock = None
                self._blockhash_updater_task = None

        implementations = platform_factory.create_for_platform(
            Platform.LETS_BONK, MinimalClient()
        )
        self.event_parser = implementations.event_parser

        logger.info(
            f"BonkLogsListener initialized for program: {self.program_id}"
        )

    async def listen_for_tokens(
        self,
        token_callback: Callable[[TokenInfo], Awaitable[None]],
        match_string: str | None = None,
        creator_address: str | None = None,
    ) -> None:
        """Listen for bonk.fun token creations.

        Args:
            token_callback: Callback for new tokens
            match_string: Optional filter string
            creator_address: Optional creator filter
        """
        consecutive_errors = 0

        while True:
            try:
                async with websockets.connect(
                    self.wss_endpoint,
                    ping_interval=30,  # Send ping every 30s
                    ping_timeout=60,   # Wait 60s for pong (public Solana is slow)
                    close_timeout=10,
                ) as websocket:
                    await self._subscribe_to_logs(websocket)
                    ping_task = asyncio.create_task(self._ping_loop(websocket))
                    consecutive_errors = 0

                    try:
                        while True:
                            token_info = await self._wait_for_token_creation(websocket)
                            if not token_info:
                                continue

                            logger.info(
                                f"🔥 BONK token detected: {token_info.name} ({token_info.symbol})"
                            )

                            # Apply filters
                            if match_string and not (
                                match_string.lower() in token_info.name.lower()
                                or match_string.lower() in token_info.symbol.lower()
                            ):
                                logger.info(f"Token doesn't match '{match_string}', skipping")
                                continue

                            if creator_address:
                                creator_str = str(token_info.creator) if token_info.creator else ""
                                user_str = str(token_info.user) if token_info.user else ""
                                if creator_address not in [creator_str, user_str]:
                                    logger.info(f"Token not by {creator_address}, skipping")
                                    continue

                            try:
                                await asyncio.wait_for(
                                    token_callback(token_info),
                                    timeout=30
                                )
                            except asyncio.TimeoutError:
                                logger.warning(f"Callback timeout for {token_info.symbol}")

                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("WebSocket closed, reconnecting...")
                    except asyncio.CancelledError:
                        raise
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                consecutive_errors += 1
                logger.warning(f"Timeout (error {consecutive_errors}/{self.max_consecutive_errors})")
            except Exception as e:
                consecutive_errors += 1
                logger.exception(f"Connection error ({consecutive_errors}/{self.max_consecutive_errors})")

            if consecutive_errors >= self.max_consecutive_errors:
                if self.raise_on_max_errors:
                    raise ConnectionError(f"BonkLogsListener failed after {consecutive_errors} errors")
                logger.error(f"Too many errors ({consecutive_errors}), waiting 30s...")
                await asyncio.sleep(30)
                consecutive_errors = 0
            else:
                backoff = min(5 * (2 ** consecutive_errors), 30)
                logger.info(f"Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)

    async def _subscribe_to_logs(self, websocket) -> None:
        """Subscribe to Raydium LaunchLab program logs."""
        subscription = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.program_id]},
                {"commitment": "processed"},
            ],
        })

        await websocket.send(subscription)
        logger.info(f"Subscribed to logs for Raydium LaunchLab: {self.program_id}")

        response = await websocket.recv()
        data = json.loads(response)
        if "result" in data:
            logger.info(f"Subscription confirmed: {data['result']}")
        else:
            logger.warning(f"Unexpected response: {response}")

    async def _ping_loop(self, websocket) -> None:
        """Keep connection alive with graceful timeout handling."""
        ping_failures = 0
        max_ping_failures = 3  # Allow 3 failed pings before closing
        
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                try:
                    pong = await websocket.ping()
                    await asyncio.wait_for(pong, timeout=30)  # Increased to 30s
                    ping_failures = 0  # Reset on success
                except TimeoutError:
                    ping_failures += 1
                    logger.warning(
                        f"Ping timeout ({ping_failures}/{max_ping_failures})"
                    )
                    if ping_failures >= max_ping_failures:
                        logger.warning("Too many ping failures, closing connection")
                        await websocket.close()
                        return
                    # Continue trying instead of immediately closing
        except asyncio.CancelledError:
            pass

    async def _wait_for_token_creation(self, websocket) -> TokenInfo | None:
        """Wait for and parse token creation from logs."""
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=60)
            data = json.loads(response)

            if data.get("method") != "logsNotification":
                return None

            log_data = data["params"]["result"]["value"]
            logs = log_data.get("logs", [])
            signature = log_data.get("signature", "")

            if not signature:
                return None

            # Check if this looks like a token creation
            # Look for "initialize" in logs or specific patterns
            is_initialize = any(
                "initialize" in log.lower() or
                "Program log: Instruction: Initialize" in log
                for log in logs
            )

            if not is_initialize:
                return None

            logger.debug(f"Potential token creation detected: {signature[:16]}...")

            # Fetch full transaction to parse
            token_info = await self._fetch_and_parse_transaction(signature)
            return token_info

        except asyncio.TimeoutError:
            logger.debug("No logs for 60s")
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed:
            raise
        except Exception:
            logger.exception("Error processing log")

        return None

    async def _fetch_and_parse_transaction(self, signature: str) -> TokenInfo | None:
        """Fetch transaction and parse token creation data."""
        try:
            async with AsyncClient(self.rpc_endpoint) as client:
                # Convert string signature to Signature object
                sig = Signature.from_string(signature)
                
                # Fetch transaction with full encoding
                response = await client.get_transaction(
                    sig,
                    encoding="base64",
                    max_supported_transaction_version=0,
                )

                if not response.value:
                    logger.debug(f"Transaction not found: {signature[:16]}...")
                    return None

                tx = response.value
                
                # Get transaction data
                tx_data = tx.transaction
                if hasattr(tx_data, "transaction"):
                    # Versioned transaction response
                    raw_tx = tx_data.transaction
                else:
                    raw_tx = tx_data

                # Decode if base64
                if isinstance(raw_tx, str):
                    raw_tx = base64.b64decode(raw_tx)
                elif hasattr(raw_tx, "__iter__") and not isinstance(raw_tx, bytes):
                    raw_tx = bytes(raw_tx)

                # Parse transaction
                try:
                    transaction = VersionedTransaction.from_bytes(raw_tx)
                except Exception:
                    logger.debug("Failed to parse as VersionedTransaction")
                    return None

                # Extract account keys
                account_keys = list(transaction.message.account_keys)
                
                # Add lookup table addresses if present
                if hasattr(transaction.message, "address_table_lookups"):
                    for lookup in transaction.message.address_table_lookups:
                        # Note: We'd need to fetch lookup table data for full resolution
                        pass

                # Find and parse LaunchLab instructions
                for ix in transaction.message.instructions:
                    program_idx = ix.program_id_index
                    if program_idx >= len(account_keys):
                        continue

                    program_id = account_keys[program_idx]
                    if str(program_id) != self.program_id:
                        continue

                    # Convert account keys to bytes
                    account_keys_bytes = [bytes(key) for key in account_keys]

                    # Parse instruction
                    token_info = self.event_parser.parse_token_creation_from_instruction(
                        bytes(ix.data),
                        list(ix.accounts),
                        account_keys_bytes,
                    )

                    if token_info:
                        logger.info(f"Parsed bonk token: {token_info.name} ({token_info.symbol})")
                        return token_info

                return None

        except Exception:
            logger.exception(f"Failed to fetch/parse transaction: {signature[:16]}...")
            return None
