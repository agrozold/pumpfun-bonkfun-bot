"""
Solana client abstraction for blockchain operations.
"""

import asyncio
import json
import struct
from typing import Any

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.account import Account
from solders.transaction import Transaction
from solders.signature import Signature

from utils.logger import get_logger
from core.blockhash_cache import get_blockhash_cache
from trading.jito_sender import get_jito_sender

logger = get_logger(__name__)

# Redis cache for RPC
try:
    from core.redis_cache import cache_get, cache_set
    REDIS_AVAILABLE = True
    logger.info("[RPC] Redis cache enabled")
except ImportError:
    REDIS_AVAILABLE = False
    cache_get = lambda k: None
    cache_set = lambda k, v, t=60: False

# =============================================================================
# GLOBAL RPC CACHE - reduces QuickNode/Helius API calls
# =============================================================================
import time as _time

_rpc_cache: dict[str, tuple[any, float]] = {}  # key -> (value, expiry_timestamp)
_cache_stats = {"hits": 0, "misses": 0}

CACHE_TTL = {
    "account_info": 10,      # 10 sec
    "token_balance": 5,      # 5 sec  
    "multiple_accounts": 10, # 10 sec
    "health": 30,            # 30 sec
    "balance": 5,            # 5 sec
}

def _cache_get(key: str):
    """Get from cache (Redis first, then local)."""
    # Try Redis first
    if REDIS_AVAILABLE:
        cached = cache_get(f"rpc:{key}")
        if cached is not None:
            _cache_stats["hits"] += 1
            return cached
    
    # Fall back to local cache
    if key in _rpc_cache:
        value, expiry = _rpc_cache[key]
        if _time.time() < expiry:
            _cache_stats["hits"] += 1
            return value
        del _rpc_cache[key]
    _cache_stats["misses"] += 1
    return None

def _cache_set(key: str, value, ttl: int):
    """Set cache with TTL (Redis + local)."""
    # Save to Redis for cross-bot sharing
    if REDIS_AVAILABLE:
        cache_set(f"rpc:{key}", value, ttl)
    
    # Also save locally for speed
    if len(_rpc_cache) > 5000:
        now = _time.time()
        expired = [k for k, (v, exp) in _rpc_cache.items() if exp < now]
        for k in expired:
            del _rpc_cache[k]
        if len(_rpc_cache) > 4000:
            sorted_keys = sorted(_rpc_cache.keys(), key=lambda k: _rpc_cache[k][1])[:1000]
            for k in sorted_keys:
                del _rpc_cache[k]
    _rpc_cache[key] = (value, _time.time() + ttl)

def get_cache_stats() -> dict:
    """Return cache statistics."""
    total = _cache_stats["hits"] + _cache_stats["misses"]
    hit_rate = (_cache_stats["hits"] / total * 100) if total > 0 else 0
    return {**_cache_stats, "total": total, "hit_rate": f"{hit_rate:.1f}%", "cache_size": len(_rpc_cache)}
# =============================================================================


def set_loaded_accounts_data_size_limit(bytes_limit: int) -> Instruction:
    """
    Create SetLoadedAccountsDataSizeLimit instruction to reduce CU consumption.

    By default, Solana transactions can load up to 64MB of account data,
    costing 16k CU (8 CU per 32KB). Setting a lower limit reduces CU
    consumption and improves transaction priority.

    NOTE: CU savings are NOT visible in "consumed CU" metrics, which only
    show execution CU. The 16k CU loaded accounts overhead is counted
    separately for transaction priority/cost calculation.

    Args:
        bytes_limit: Max account data size in bytes (e.g., 512_000 = 512KB)

    Returns:
        Compute Budget instruction with discriminator 4

    Reference:
        https://www.anza.xyz/blog/cu-optimization-with-setloadedaccountsdatasizelimit
    """
    COMPUTE_BUDGET_PROGRAM = Pubkey.from_string(
        "ComputeBudget111111111111111111111111111111"
    )

    data = struct.pack("<BI", 4, bytes_limit)
    return Instruction(COMPUTE_BUDGET_PROGRAM, data, [])


class SolanaClient:
    """Abstraction for Solana RPC client operations."""

    def __init__(self, rpc_endpoint: str):
        """Initialize Solana client with RPC endpoint.

        Args:
            rpc_endpoint: URL of the Solana RPC endpoint
        """
        self.rpc_endpoint = rpc_endpoint
        self._client = None
        self._blockhash_cache = None  # Will be initialized lazily

    async def get_cached_blockhash(self) -> Hash:
        """Return cached blockhash from global BlockhashCache."""
        if self._blockhash_cache is None:
            self._blockhash_cache = await get_blockhash_cache(self.rpc_endpoint)
        return await self._blockhash_cache.get_blockhash()

    async def get_client(self) -> AsyncClient:
        """Get or create the AsyncClient instance.

        Returns:
            AsyncClient instance
        """
        if self._client is None:
            self._client = AsyncClient(self.rpc_endpoint)
        return self._client

    async def close(self):
        """Close the client connection."""
        if self._client:
            await self._client.close()
            self._client = None

    async def get_health(self) -> str | None:
        cache_key = "health"
        cached = _cache_get(cache_key)
        if cached is not None:
            return Account.from_json(cached)
            
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getHealth",
        }
        result = await self.post_rpc(body)
        if result and "result" in result:
            _cache_set(cache_key, result["result"], CACHE_TTL["health"])
            return result["result"]
        return None

    async def get_account_info(self, pubkey: Pubkey) -> dict[str, Any]:
        """Get account info from the blockchain (with caching).

        Args:
            pubkey: Public key of the account

        Returns:
            Account info response

        Raises:
            ValueError: If account doesn't exist or has no data
        """
        cache_key = f"acc:{str(pubkey)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return Account.from_json(cached)
            
        client = await self.get_client()
        response = await client.get_account_info(
            pubkey, encoding="base64"
        )  # base64 encoding for account data by default
        if not response.value:
            raise ValueError(f"Account {pubkey} not found")
        
        _cache_set(cache_key, response.value.to_json(), CACHE_TTL["account_info"])
        return response.value

    async def get_multiple_accounts(self, pubkeys: list[Pubkey]) -> list[dict[str, Any] | None]:
        """Get multiple accounts in a single RPC call (batch).
        
        Much more efficient than calling get_account_info multiple times.
        Solana supports up to 100 accounts per call.

        Args:
            pubkeys: List of public keys (max 100)

        Returns:
            List of account info dicts (None for accounts that don't exist)
        """
        if not pubkeys:
            return []
        
        # Solana limit is 100 accounts per call
        if len(pubkeys) > 100:
            logger.warning(f"get_multiple_accounts: truncating {len(pubkeys)} to 100")
            pubkeys = pubkeys[:100]
        
        client = await self.get_client()
        response = await client.get_multiple_accounts(pubkeys, encoding="base64")
        
        results = []
        for account in response.value:
            results.append(account if account else None)
        
        return results

    async def get_token_account_balance(self, token_account: Pubkey) -> int:
        """Get token balance for an account (with caching).

        Args:
            token_account: Token account address

        Returns:
            Token balance as integer
        """
        cache_key = f"bal:{str(token_account)}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return Account.from_json(cached)
            
        client = await self.get_client()
        response = await client.get_token_account_balance(token_account)
        result = int(response.value.amount) if response.value else 0
        
        _cache_set(cache_key, result, CACHE_TTL["token_balance"])
        return result

    async def get_latest_blockhash(self) -> Hash:
        """Get the latest blockhash.

        Returns:
            Recent blockhash as string
        """
        client = await self.get_client()
        response = await client.get_latest_blockhash(commitment="processed")
        return response.value.blockhash

    async def build_and_send_transaction(
        self,
        instructions: list[Instruction],
        signer_keypair: Keypair,
        skip_preflight: bool = False,
        max_retries: int = 5,
        priority_fee: int | None = None,
        compute_unit_limit: int | None = None,
        account_data_size_limit: int | None = None,
    ) -> str:
        """
        Send a transaction with optional priority fee and compute unit limit.

        Args:
            instructions: List of instructions to include in the transaction.
            signer_keypair: Keypair to sign the transaction.
            skip_preflight: Whether to skip preflight checks (default: False for reliability).
            max_retries: Maximum number of retry attempts (default: 5).
            priority_fee: Optional priority fee in microlamports.
            compute_unit_limit: Optional compute unit limit. Defaults to 85,000 if not provided.
            account_data_size_limit: Optional account data size limit in bytes (e.g., 512_000).
                                    Reduces CU cost from 16k to ~128 CU. Must be first instruction.

        Returns:
            Transaction signature.

        Raises:
            ValueError: If insufficient funds detected
            RuntimeError: If all retry attempts fail
        """
        client = await self.get_client()

        logger.info(
            f"Priority fee in microlamports: {priority_fee if priority_fee else 0}"
        )

        # Add compute budget instructions if applicable
        if (
            priority_fee is not None
            or compute_unit_limit is not None
            or account_data_size_limit is not None
        ):
            fee_instructions = []

            if account_data_size_limit is not None:
                fee_instructions.append(
                    set_loaded_accounts_data_size_limit(account_data_size_limit)
                )
                logger.info(f"Account data size limit: {account_data_size_limit} bytes")

            # Set compute unit limit (use provided value or default to 85,000)
            cu_limit = compute_unit_limit if compute_unit_limit is not None else 85_000
            fee_instructions.append(set_compute_unit_limit(cu_limit))

            # Set priority fee if provided
            if priority_fee is not None:
                fee_instructions.append(set_compute_unit_price(priority_fee))

            instructions = fee_instructions + instructions

        last_error = None

        for attempt in range(max_retries):
            try:
                # Get fresh blockhash for each attempt to avoid BlockhashNotFound
                try:
                    recent_blockhash = await self.get_cached_blockhash()
                except RuntimeError:
                    # Fallback to direct fetch if cache not ready
                    recent_blockhash = await self.get_latest_blockhash()

                # Add JITO tip instruction if JITO is enabled
                jito = get_jito_sender()
                tx_instructions = list(instructions)  # copy
                if jito.enabled:
                    tip_ix = jito.create_tip_instruction(signer_keypair.pubkey())
                    tx_instructions.append(tip_ix)
                    logger.debug(f"[JITO] Added tip: {jito.tip_lamports} lamports")
                
                message = Message(tx_instructions, signer_keypair.pubkey())
                transaction = Transaction([signer_keypair], message, recent_blockhash)

                tx_opts = TxOpts(
                    skip_preflight=skip_preflight, preflight_commitment=Processed
                )

                logger.info(f"Sending transaction attempt {attempt + 1}/{max_retries}...")
                
                # Try JITO first for faster landing
                if jito.enabled:
                    try:
                        jito_sig = await jito.send_transaction(transaction)
                        if jito_sig:
                            logger.info(f"[JITO] Transaction sent: {jito_sig}")
                            return jito_sig
                        logger.warning("[JITO] Failed, falling back to regular RPC")
                    except Exception as jito_err:
                        logger.warning(f"[JITO] Error: {jito_err}, falling back to regular RPC")
                
                # Fallback to regular RPC
                response = await client.send_transaction(transaction, tx_opts)
                logger.info(f"Transaction sent successfully: {response.value}")
                return response.value

            except Exception as e:
                last_error = e
                error_str = str(e)
                error_str_lower = error_str.lower()

                # ============================================
                # NON-RETRYABLE ERRORS - fail immediately
                # ============================================
                
                # BondingCurveComplete (6005/0x1775) - token migrated to Raydium
                # No point retrying - bonding curve is permanently closed
                if "0x1775" in error_str or "6005" in error_str or "bondingcurvecomplete" in error_str_lower:
                    logger.error(
                        f"BondingCurveComplete: Token has migrated to Raydium, cannot buy on bonding curve"
                    )
                    raise RuntimeError(
                        "BondingCurveComplete: Token migrated to Raydium"
                    ) from e
                
                # BondingCurveNotComplete (6006/0x1776) - cannot sell, curve still active
                if "0x1776" in error_str or "6006" in error_str or "bondingcurvenotcomplete" in error_str_lower:
                    logger.error(
                        f"BondingCurveNotComplete: Cannot perform this operation, curve still active"
                    )
                    raise RuntimeError(
                        "BondingCurveNotComplete: Bonding curve still active"
                    ) from e
                
                # SlippageExceeded - price moved too much
                if "slippage" in error_str_lower or "0x1772" in error_str:
                    logger.error(f"Slippage exceeded: Price moved beyond tolerance")
                    raise RuntimeError("SlippageExceeded: Price moved too much") from e
                
                # InsufficientFunds
                if "insufficient" in error_str_lower or "not enough" in error_str_lower:
                    logger.error(f"Insufficient funds detected: {e}")
                    raise ValueError(f"Insufficient funds: {e}") from e
                
                # AccountNotFound / InvalidAccount - wrong addresses
                if "account not found" in error_str_lower or "invalid account" in error_str_lower:
                    logger.error(f"Account error (non-retryable): {e}")
                    raise RuntimeError(f"Account error: {e}") from e

                # ============================================
                # RETRYABLE ERRORS - continue retry loop
                # ============================================
                
                if "blockhash not found" in error_str_lower or "blockhashnotfound" in error_str_lower:
                    logger.warning(f"Blockhash expired, fetching new one (attempt {attempt + 1})")
                    # Force refresh blockhash
                    try:
                        fresh_blockhash = await self.get_latest_blockhash()
                        async with self._blockhash_lock:
                            self._cached_blockhash = fresh_blockhash
                    except Exception as bh_err:
                        logger.warning(f"Failed to refresh blockhash: {bh_err}")

                if attempt == max_retries - 1:
                    logger.exception(
                        f"Failed to send transaction after {max_retries} attempts"
                    )
                    raise RuntimeError(
                        f"Transaction failed after {max_retries} attempts: {last_error}"
                    ) from last_error

                # Exponential backoff with jitter: 0.5s, 1s, 2s, 4s...
                base_wait = min(2 ** attempt, 8)  # Cap at 8 seconds
                wait_time = base_wait * (0.5 + 0.5 * (attempt / max_retries))
                logger.warning(
                    f"Transaction attempt {attempt + 1} failed: {e!s}, retrying in {wait_time:.1f}s"
                )
                await asyncio.sleep(wait_time)

    async def confirm_transaction(
        self, signature: str, commitment: str = "confirmed", timeout: float = 45.0
    ) -> bool:
        """Wait for transaction confirmation with timeout.
        
        IMPROVED: If timeout occurs, check transaction status directly.
        Transactions may be confirmed even if wait times out.

        Args:
            signature: Transaction signature
            commitment: Confirmation commitment level
            timeout: Maximum time to wait for confirmation (default: 45s)

        Returns:
            Whether transaction was confirmed
        """
        client = await self.get_client()
        sig_obj = Signature.from_string(signature) if isinstance(signature, str) else signature
        
        try:
            logger.info(f"Waiting for confirmation (timeout: {timeout}s)...")
            await asyncio.wait_for(
                client.confirm_transaction(sig_obj, commitment=commitment, sleep_seconds=0.5),
                timeout=timeout,
            )
            return True
        except TimeoutError:
            logger.warning(f"Confirmation wait timed out after {timeout}s, checking status directly...")
            # Don't give up! Check if transaction actually succeeded
            try:
                status = await client.get_signature_statuses([sig_obj])
                if status.value and status.value[0]:
                    stat = status.value[0]
                    if stat.err is None:
                        logger.info(f"Transaction {signature[:20]}... confirmed despite timeout!")
                        return True
                    else:
                        logger.error(f"Transaction failed with error: {stat.err}")
                        return False
                # Status not found yet - check transaction directly
                tx_resp = await client.get_transaction(
                    sig_obj, 
                    encoding="jsonParsed",
                    max_supported_transaction_version=0
                )
                if tx_resp.value and tx_resp.value.transaction:
                    meta = tx_resp.value.transaction.meta
                    if meta and meta.err is None:
                        logger.info(f"Transaction {signature[:20]}... SUCCESS (verified via getTransaction)")
                        return True
                logger.warning(f"Transaction {signature[:20]}... status unclear after timeout")
                return False
            except Exception as e:
                logger.warning(f"Failed to check transaction status: {e}")
                return False
        except Exception:
            logger.exception(f"Failed to confirm transaction {signature}")
            return False

    async def get_transaction_token_balance(
        self, signature: str, user_pubkey: Pubkey, mint: Pubkey
    ) -> int | None:
        """Get the user's token balance after a transaction from postTokenBalances.

        Args:
            signature: Transaction signature
            user_pubkey: User's wallet public key
            mint: Token mint address

        Returns:
            Token balance (raw amount) after transaction, or None if not found
        """
        result = await self._get_transaction_result(signature)
        if not result:
            return None

        meta = result.get("meta", {})
        post_token_balances = meta.get("postTokenBalances", [])

        user_str = str(user_pubkey)
        mint_str = str(mint)

        for balance in post_token_balances:
            if balance.get("owner") == user_str and balance.get("mint") == mint_str:
                ui_amount = balance.get("uiTokenAmount", {})
                amount_str = ui_amount.get("amount")
                if amount_str:
                    return int(amount_str)

        return None

    async def get_buy_transaction_details(
        self, signature: str, mint: Pubkey, sol_destination: Pubkey
    ) -> tuple[int | None, int | None]:
        """Get actual tokens received and SOL spent from a buy transaction.

        Uses preBalances/postBalances to find exact SOL transferred to the
        pool/curve and pre/post token balance diff to find tokens received.

        Args:
            signature: Transaction signature
            mint: Token mint address
            sol_destination: Address where SOL is sent (bonding curve for pump.fun,
                           quote_vault for letsbonk)

        Returns:
            Tuple of (tokens_received_raw, sol_spent_lamports), or (None, None)
        """
        result = await self._get_transaction_result(signature)
        if not result:
            return None, None

        meta = result.get("meta", {})
        mint_str = str(mint)

        # Get tokens received from pre/post token balance diff
        # This works for Token2022 where owner might be different
        tokens_received = None
        pre_token_balances = meta.get("preTokenBalances", [])
        post_token_balances = meta.get("postTokenBalances", [])

        # Build lookup by account index
        pre_by_idx = {b.get("accountIndex"): b for b in pre_token_balances}
        post_by_idx = {b.get("accountIndex"): b for b in post_token_balances}

        # Find positive token diff for our mint (user receiving tokens)
        all_indices = set(pre_by_idx.keys()) | set(post_by_idx.keys())
        for idx in all_indices:
            pre = pre_by_idx.get(idx)
            post = post_by_idx.get(idx)

            # Check if this is our mint
            balance_mint = (post or pre).get("mint", "")
            if balance_mint != mint_str:
                continue

            pre_amount = (
                int(pre.get("uiTokenAmount", {}).get("amount", 0)) if pre else 0
            )
            post_amount = (
                int(post.get("uiTokenAmount", {}).get("amount", 0)) if post else 0
            )
            diff = post_amount - pre_amount

            # Positive diff means tokens received (not the bonding curve's negative)
            if diff > 0:
                tokens_received = diff
                logger.info(f"Tokens received from tx: {tokens_received}")
                break

        # Get SOL spent from preBalances/postBalances at sol_destination
        sol_destination_str = str(sol_destination)
        sol_spent = None
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        account_keys = (
            result.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )

        for i, key in enumerate(account_keys):
            key_str = key if isinstance(key, str) else key.get("pubkey", "")
            if key_str == sol_destination_str:
                if i < len(pre_balances) and i < len(post_balances):
                    sol_spent = post_balances[i] - pre_balances[i]
                    if sol_spent > 0:
                        logger.info(f"SOL to pool/curve: {sol_spent} lamports")
                    else:
                        logger.warning(
                            f"SOL destination balance change not positive: {sol_spent}"
                        )
                        sol_spent = None
                break

        return tokens_received, sol_spent

    async def _get_transaction_result(self, signature: str) -> dict | None:
        """Fetch transaction result from RPC.

        Args:
            signature: Transaction signature

        Returns:
            Transaction result dict or None
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }

        response = await self.post_rpc(body)
        if not response or "result" not in response:
            logger.warning(f"Failed to get transaction {signature}")
            return None

        result = response["result"]
        if not result or "meta" not in result:
            return None

        return result

    async def post_rpc(self, body: dict[str, Any]) -> dict[str, Any] | None:
        """
        Send a raw RPC request to the Solana node.

        Args:
            body: JSON-RPC request body.

        Returns:
            Optional[Dict[str, Any]]: Parsed JSON response, or None if the request fails.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.rpc_endpoint,
                    json=body,
                    timeout=aiohttp.ClientTimeout(10),  # 10-second timeout
                ) as response:
                    response.raise_for_status()
                    return await response.json()
        except aiohttp.ClientError:
            logger.exception("RPC request failed")
            return None
        except json.JSONDecodeError:
            logger.exception("Failed to decode RPC response")
            return None
