"""Fallback trading methods for migrated tokens.

Provides Jupiter buy/sell functionality when bonding curve is unavailable.
"""

import struct
from typing import TYPE_CHECKING

import aiohttp
import base58
from solana.rpc.commitment import Confirmed
from solana.rpc.types import MemcmpOpts, TxOpts
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import get_associated_token_address

from utils.logger import get_logger

if TYPE_CHECKING:
    from core.client import SolanaClient
    from core.wallet import Wallet

logger = get_logger(__name__)

# Constants
TOKEN_DECIMALS = 6
LAMPORTS_PER_SOL = 1_000_000_000

# PumpSwap constants
SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_SWAP_GLOBAL_CONFIG = Pubkey.from_string("ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw")
PUMP_SWAP_EVENT_AUTHORITY = Pubkey.from_string("GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR")
STANDARD_PUMPSWAP_FEE_RECIPIENT = Pubkey.from_string("7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ")
PUMP_FEE_PROGRAM = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")
BUY_DISCRIMINATOR = bytes.fromhex("66063d1201daebea")

# System constants
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
SYSTEM_TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# Pool structure offsets
POOL_BASE_MINT_OFFSET = 43
POOL_MAYHEM_MODE_OFFSET = 243
POOL_MAYHEM_MODE_MIN_SIZE = 244
GLOBALCONFIG_RESERVED_FEE_OFFSET = 72


class FallbackSeller:
    """Handles selling tokens via PumpSwap or Jupiter when bonding curve unavailable."""

    def __init__(
        self,
        client: "SolanaClient",
        wallet: "Wallet",
        slippage: float = 0.25,
        priority_fee: int = 100_000,
        max_retries: int = 3,
    ):
        self.client = client
        self.wallet = wallet
        self.slippage = slippage
        self.priority_fee = priority_fee
        self.max_retries = max_retries

    async def buy_via_pumpswap(
        self,
        mint: Pubkey,
        sol_amount: float,
        symbol: str = "TOKEN",
        market_address: Pubkey | None = None,  # Optional - skip lookup if provided
    ) -> tuple[bool, str | None, str | None]:
        """Buy token via PumpSwap AMM - for migrated tokens.
        
        Args:
            mint: Token mint address
            sol_amount: Amount of SOL to spend
            symbol: Token symbol for logging
            market_address: Optional pool address (skip lookup if provided)
            
        Returns:
            Tuple of (success, tx_signature, error_message)
        """
        from solders.system_program import TransferParams, transfer
        from spl.token.instructions import (
            SyncNativeParams,
            create_idempotent_associated_token_account,
            sync_native,
        )
        
        try:
            rpc_client = await self.client.get_client()
            
            # Use provided market or find it
            if market_address:
                market = market_address
                logger.info(f"📍 Using provided PumpSwap market: {market}")
            else:
                # Find market via get_program_accounts (expensive!)
                filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(mint))]
                response = await rpc_client.get_program_accounts(
                    PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters
                )
                
                if not response.value:
                    return False, None, "PumpSwap market not found"
                
                market = response.value[0].pubkey
                logger.info(f"📍 Found PumpSwap market: {market}")
            
            # Get market data
            market_response = await rpc_client.get_account_info(market, encoding="base64")
            if not market_response.value:
                return False, None, f"Market account {market} not found on chain"
            
            data = market_response.value.data
            market_data = self._parse_market_data(data)
            
            token_program_id = await self._get_token_program_id(mint)
            
            # Get user token accounts
            user_base_ata = get_associated_token_address(
                self.wallet.pubkey, mint, token_program_id
            )
            user_quote_ata = get_associated_token_address(
                self.wallet.pubkey, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )
            
            # Get pool accounts
            pool_base_ata = Pubkey.from_string(market_data["pool_base_token_account"])
            pool_quote_ata = Pubkey.from_string(market_data["pool_quote_token_account"])
            
            # Calculate price and token amount
            base_resp = await rpc_client.get_token_account_balance(pool_base_ata)
            quote_resp = await rpc_client.get_token_account_balance(pool_quote_ata)
            base_amount = float(base_resp.value.ui_amount)
            quote_amount = float(quote_resp.value.ui_amount)
            price = quote_amount / base_amount
            
            # Calculate expected tokens
            expected_tokens = sol_amount / price
            min_tokens_output = int(expected_tokens * (1 - self.slippage) * 10**TOKEN_DECIMALS)
            buy_amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
            
            logger.info(f"💵 PumpSwap BUY: {sol_amount} SOL -> ~{expected_tokens:,.2f} {symbol}")
            
            # Get fee recipients
            fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
            fee_recipient_ata = get_associated_token_address(
                fee_recipient, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )
            
            # Get creator vault
            coin_creator = Pubkey.from_string(market_data["coin_creator"])
            coin_creator_vault, _ = Pubkey.find_program_address(
                [b"creator_vault", bytes(coin_creator)], PUMP_AMM_PROGRAM_ID
            )
            coin_creator_vault_ata = get_associated_token_address(
                coin_creator_vault, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )
            
            # Fee config PDA
            fee_config, _ = Pubkey.find_program_address(
                [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)], PUMP_FEE_PROGRAM
            )
            
            # Build accounts for BUY (SOL -> Token)
            accounts = [
                AccountMeta(pubkey=market, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SOL_MINT, is_signer=False, is_writable=False),  # input mint (SOL)
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),  # output mint (token)
                AccountMeta(pubkey=user_quote_ata, is_signer=False, is_writable=True),  # user SOL ATA
                AccountMeta(pubkey=user_base_ata, is_signer=False, is_writable=True),  # user token ATA
                AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),  # pool SOL
                AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),  # pool token
                AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),
                AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=coin_creator_vault_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=coin_creator_vault, is_signer=False, is_writable=False),
                AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
            ]
            
            # Build instruction data: discriminator + amount_in + min_amount_out
            ix_data = BUY_DISCRIMINATOR + struct.pack("<Q", buy_amount_lamports) + struct.pack("<Q", min_tokens_output)
            
            # Instructions
            compute_limit_ix = set_compute_unit_limit(200_000)
            compute_price_ix = set_compute_unit_price(self.priority_fee)
            
            # Create token ATA (idempotent)
            create_token_ata_ix = create_idempotent_associated_token_account(
                self.wallet.pubkey, self.wallet.pubkey, mint, token_program_id
            )
            
            # Create wrapped SOL ATA (idempotent)
            create_wsol_ata_ix = create_idempotent_associated_token_account(
                self.wallet.pubkey, self.wallet.pubkey, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )
            
            # Transfer SOL to wrapped SOL account
            transfer_ix = transfer(
                TransferParams(
                    from_pubkey=self.wallet.pubkey,
                    to_pubkey=user_quote_ata,
                    lamports=buy_amount_lamports,
                )
            )
            
            # Sync native (update wrapped SOL balance)
            sync_ix = sync_native(SyncNativeParams(SYSTEM_TOKEN_PROGRAM, user_quote_ata))
            
            # Buy instruction
            buy_ix = Instruction(PUMP_AMM_PROGRAM_ID, ix_data, accounts)
            
            # Send transaction
            for attempt in range(self.max_retries):
                try:
                    blockhash = await rpc_client.get_latest_blockhash()
                    msg = Message.new_with_blockhash(
                        [
                            compute_limit_ix,
                            compute_price_ix,
                            create_token_ata_ix,
                            create_wsol_ata_ix,
                            transfer_ix,
                            sync_ix,
                            buy_ix,
                        ],
                        self.wallet.pubkey,
                        blockhash.value.blockhash,
                    )
                    tx = VersionedTransaction(message=msg, keypairs=[self.wallet.keypair])
                    
                    logger.info(f"🚀 PumpSwap BUY attempt {attempt + 1}/{self.max_retries}...")
                    result = await rpc_client.send_transaction(
                        tx, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                    )
                    sig = str(result.value)
                    
                    logger.info(f"📤 PumpSwap BUY signature: {sig}")
                    
                    await rpc_client.confirm_transaction(sig, commitment="confirmed")
                    logger.info(f"✅ PumpSwap BUY confirmed! Got ~{expected_tokens:,.2f} {symbol}")
                    return True, sig, None
                    
                except Exception as e:
                    logger.warning(f"PumpSwap BUY attempt {attempt + 1} failed: {e}")
                    if attempt == self.max_retries - 1:
                        return False, None, str(e)
            
            return False, None, "All PumpSwap BUY attempts failed"
            
        except Exception as e:
            return False, None, str(e)

    async def buy_via_jupiter(
        self,
        mint: Pubkey,
        sol_amount: float,
        symbol: str = "TOKEN",
    ) -> tuple[bool, str | None, str | None]:
        """Buy token via Jupiter aggregator - works for any token with liquidity.
        
        Args:
            mint: Token mint address
            sol_amount: Amount of SOL to spend
            symbol: Token symbol for logging
            
        Returns:
            Tuple of (success, tx_signature, error_message)
        """
        import base64
        
        try:
            logger.info(f"🪐 Jupiter BUY for {symbol} with {sol_amount} SOL...")
            
            buy_amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)
            slippage_bps = int(self.slippage * 10000)
            
            jupiter_quote_url = "https://quote-api.jup.ag/v6/quote"
            jupiter_swap_url = "https://quote-api.jup.ag/v6/swap"
            
            async with aiohttp.ClientSession() as session:
                # Get quote: SOL -> Token
                quote_params = {
                    "inputMint": str(SOL_MINT),
                    "outputMint": str(mint),
                    "amount": str(buy_amount_lamports),
                    "slippageBps": slippage_bps,
                }
                
                async with session.get(jupiter_quote_url, params=quote_params) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        return False, None, f"Jupiter quote failed: {error_text}"
                    quote = await resp.json()
                
                out_amount = int(quote.get("outAmount", 0))
                out_amount_tokens = out_amount / (10 ** TOKEN_DECIMALS)
                logger.info(f"💵 Jupiter expected: ~{out_amount_tokens:,.2f} {symbol}")
                
                # Get swap transaction
                swap_body = {
                    "quoteResponse": quote,
                    "userPublicKey": str(self.wallet.pubkey),
                    "wrapAndUnwrapSol": True,
                    "prioritizationFeeLamports": self.priority_fee,
                }
                
                rpc_client = await self.client.get_client()
                
                for attempt in range(self.max_retries):
                    try:
                        async with session.post(jupiter_swap_url, json=swap_body) as resp:
                            if resp.status != 200:
                                error_text = await resp.text()
                                logger.warning(f"Jupiter swap request failed: {error_text}")
                                continue
                            swap_data = await resp.json()
                        
                        swap_tx_base64 = swap_data.get("swapTransaction")
                        if not swap_tx_base64:
                            return False, None, "No swap transaction in Jupiter response"
                        
                        tx_bytes = base64.b64decode(swap_tx_base64)
                        tx = VersionedTransaction.from_bytes(tx_bytes)
                        signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])
                        
                        logger.info(f"🚀 Jupiter BUY attempt {attempt + 1}/{self.max_retries}...")
                        result = await rpc_client.send_transaction(
                            signed_tx,
                            opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                        )
                        sig = str(result.value)
                        
                        logger.info(f"📤 Jupiter BUY signature: {sig}")
                        
                        await rpc_client.confirm_transaction(sig, commitment="confirmed")
                        logger.info(f"✅ Jupiter BUY confirmed! Got ~{out_amount_tokens:,.2f} {symbol}")
                        return True, sig, None
                        
                    except Exception as e:
                        logger.warning(f"Jupiter BUY attempt {attempt + 1} failed: {e}")
                        if attempt == self.max_retries - 1:
                            return False, None, str(e)
                
                return False, None, "All Jupiter BUY attempts failed"
                
        except Exception as e:
            return False, None, str(e)

    async def sell(
        self,
        mint: Pubkey,
        token_amount: float,
        symbol: str = "TOKEN",
    ) -> tuple[bool, str | None, str | None]:
        """Try to sell via PumpSwap, fallback to Jupiter.
        
        Returns:
            Tuple of (success, tx_signature, error_message)
        """
        logger.info(f"🔄 Attempting fallback sell for {symbol} ({mint})")
        
        # Try PumpSwap first
        success, sig, error = await self._sell_via_pumpswap(mint, token_amount, symbol)
        if success:
            return success, sig, None
        
        logger.info(f"PumpSwap failed: {error}, trying Jupiter...")
        
        # Fallback to Jupiter
        success, sig, error = await self._sell_via_jupiter(mint, token_amount, symbol)
        return success, sig, error

    async def _get_token_program_id(self, mint: Pubkey) -> Pubkey:
        """Determine if mint uses TokenProgram or Token2022Program."""
        rpc_client = await self.client.get_client()
        mint_info = await rpc_client.get_account_info(mint)
        if not mint_info.value:
            raise ValueError(f"Could not fetch mint info for {mint}")
        owner = mint_info.value.owner
        if owner == SYSTEM_TOKEN_PROGRAM:
            return SYSTEM_TOKEN_PROGRAM
        elif owner == TOKEN_2022_PROGRAM:
            return TOKEN_2022_PROGRAM
        raise ValueError(f"Unknown token program: {owner}")

    async def _get_token_balance(self, ata: Pubkey) -> int:
        """Get token balance in raw units."""
        rpc_client = await self.client.get_client()
        response = await rpc_client.get_token_account_balance(ata)
        return int(response.value.amount) if response.value else 0

    async def _sell_via_pumpswap(
        self,
        mint: Pubkey,
        token_amount: float,
        symbol: str,
    ) -> tuple[bool, str | None, str | None]:
        """Sell via PumpSwap AMM."""
        try:
            rpc_client = await self.client.get_client()
            
            # Find market
            filters = [MemcmpOpts(offset=POOL_BASE_MINT_OFFSET, bytes=bytes(mint))]
            response = await rpc_client.get_program_accounts(
                PUMP_AMM_PROGRAM_ID, encoding="base64", filters=filters
            )
            
            if not response.value:
                return False, None, "PumpSwap market not found"
            
            market = response.value[0].pubkey
            logger.info(f"📍 Found PumpSwap market: {market}")
            
            # Get market data
            market_response = await rpc_client.get_account_info(market, encoding="base64")
            data = market_response.value.data
            market_data = self._parse_market_data(data)
            
            token_program_id = await self._get_token_program_id(mint)
            
            # Get user token accounts
            user_base_ata = get_associated_token_address(
                self.wallet.pubkey, mint, token_program_id
            )
            user_quote_ata = get_associated_token_address(
                self.wallet.pubkey, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )
            
            # Calculate sell amount
            sell_amount = int(token_amount * 10**TOKEN_DECIMALS)
            
            # Get pool accounts
            pool_base_ata = Pubkey.from_string(market_data["pool_base_token_account"])
            pool_quote_ata = Pubkey.from_string(market_data["pool_quote_token_account"])
            
            # Calculate price
            base_resp = await rpc_client.get_token_account_balance(pool_base_ata)
            quote_resp = await rpc_client.get_token_account_balance(pool_quote_ata)
            base_amount = float(base_resp.value.ui_amount)
            quote_amount = float(quote_resp.value.ui_amount)
            price = quote_amount / base_amount
            
            sol_value = token_amount * price
            min_sol_output = int(sol_value * (1 - self.slippage) * LAMPORTS_PER_SOL)
            
            logger.info(f"💵 PumpSwap price: {price:.10f} SOL, expected: ~{sol_value:.6f} SOL")
            
            # Get fee recipients
            fee_recipient = STANDARD_PUMPSWAP_FEE_RECIPIENT
            fee_recipient_ata = get_associated_token_address(
                fee_recipient, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )
            
            # Get creator vault
            coin_creator = Pubkey.from_string(market_data["coin_creator"])
            coin_creator_vault, _ = Pubkey.find_program_address(
                [b"creator_vault", bytes(coin_creator)], PUMP_AMM_PROGRAM_ID
            )
            coin_creator_vault_ata = get_associated_token_address(
                coin_creator_vault, SOL_MINT, SYSTEM_TOKEN_PROGRAM
            )
            
            # Fee config PDA
            fee_config, _ = Pubkey.find_program_address(
                [b"fee_config", bytes(PUMP_AMM_PROGRAM_ID)], PUMP_FEE_PROGRAM
            )
            
            # Build accounts
            accounts = [
                AccountMeta(pubkey=market, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=PUMP_SWAP_GLOBAL_CONFIG, is_signer=False, is_writable=False),
                AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SOL_MINT, is_signer=False, is_writable=False),
                AccountMeta(pubkey=user_base_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_quote_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=pool_base_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=pool_quote_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=False),
                AccountMeta(pubkey=fee_recipient_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=token_program_id, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=ASSOCIATED_TOKEN_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_SWAP_EVENT_AUTHORITY, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_AMM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=coin_creator_vault_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=coin_creator_vault, is_signer=False, is_writable=False),
                AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False),
                AccountMeta(pubkey=PUMP_FEE_PROGRAM, is_signer=False, is_writable=False),
            ]
            
            # Build instruction
            ix_data = SELL_DISCRIMINATOR + struct.pack("<Q", sell_amount) + struct.pack("<Q", min_sol_output)
            
            # Create ATA instruction (idempotent)
            create_ata_accounts = [
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=user_quote_ata, is_signer=False, is_writable=True),
                AccountMeta(pubkey=self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(pubkey=SOL_MINT, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
                AccountMeta(pubkey=SYSTEM_TOKEN_PROGRAM, is_signer=False, is_writable=False),
            ]
            create_ata_ix = Instruction(ASSOCIATED_TOKEN_PROGRAM, bytes([1]), create_ata_accounts)
            
            compute_limit_ix = set_compute_unit_limit(150_000)
            compute_price_ix = set_compute_unit_price(self.priority_fee)
            sell_ix = Instruction(PUMP_AMM_PROGRAM_ID, ix_data, accounts)
            
            # Send transaction
            for attempt in range(self.max_retries):
                try:
                    blockhash = await rpc_client.get_latest_blockhash()
                    msg = Message.new_with_blockhash(
                        [compute_limit_ix, compute_price_ix, create_ata_ix, sell_ix],
                        self.wallet.pubkey,
                        blockhash.value.blockhash,
                    )
                    tx = VersionedTransaction(message=msg, keypairs=[self.wallet.keypair])
                    
                    logger.info(f"🚀 PumpSwap sell attempt {attempt + 1}/{self.max_retries}...")
                    result = await rpc_client.send_transaction(
                        tx, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                    )
                    sig = str(result.value)
                    
                    logger.info(f"📤 PumpSwap signature: {sig}")
                    
                    await rpc_client.confirm_transaction(sig, commitment="confirmed")
                    logger.info("✅ PumpSwap sell confirmed!")
                    return True, sig, None
                    
                except Exception as e:
                    logger.warning(f"PumpSwap attempt {attempt + 1} failed: {e}")
                    if attempt == self.max_retries - 1:
                        return False, None, str(e)
            
            return False, None, "All PumpSwap attempts failed"
            
        except Exception as e:
            return False, None, str(e)

    def _parse_market_data(self, data: bytes) -> dict:
        """Parse PumpSwap pool account data."""
        parsed_data = {}
        offset = 8  # Skip discriminator
        
        fields = [
            ("pool_bump", "u8"), ("index", "u16"), ("creator", "pubkey"),
            ("base_mint", "pubkey"), ("quote_mint", "pubkey"), ("lp_mint", "pubkey"),
            ("pool_base_token_account", "pubkey"), ("pool_quote_token_account", "pubkey"),
            ("lp_supply", "u64"), ("coin_creator", "pubkey"),
        ]
        
        for field_name, field_type in fields:
            if field_type == "pubkey":
                parsed_data[field_name] = base58.b58encode(data[offset:offset + 32]).decode("utf-8")
                offset += 32
            elif field_type in {"u64", "i64"}:
                parsed_data[field_name] = struct.unpack("<Q" if field_type == "u64" else "<q", data[offset:offset + 8])[0]
                offset += 8
            elif field_type == "u16":
                parsed_data[field_name] = struct.unpack("<H", data[offset:offset + 2])[0]
                offset += 2
            elif field_type == "u8":
                parsed_data[field_name] = data[offset]
                offset += 1
        
        return parsed_data

    async def _sell_via_jupiter(
        self,
        mint: Pubkey,
        token_amount: float,
        symbol: str,
    ) -> tuple[bool, str | None, str | None]:
        """Sell via Jupiter aggregator."""
        import base64
        
        try:
            logger.info(f"🪐 Jupiter sell for {symbol}...")
            
            sell_amount = int(token_amount * 10**TOKEN_DECIMALS)
            slippage_bps = int(self.slippage * 10000)
            
            jupiter_quote_url = "https://quote-api.jup.ag/v6/quote"
            jupiter_swap_url = "https://quote-api.jup.ag/v6/swap"
            
            async with aiohttp.ClientSession() as session:
                # Get quote
                quote_params = {
                    "inputMint": str(mint),
                    "outputMint": str(SOL_MINT),
                    "amount": str(sell_amount),
                    "slippageBps": slippage_bps,
                }
                
                async with session.get(jupiter_quote_url, params=quote_params) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        return False, None, f"Jupiter quote failed: {error_text}"
                    quote = await resp.json()
                
                out_amount = int(quote.get("outAmount", 0))
                out_amount_sol = out_amount / LAMPORTS_PER_SOL
                logger.info(f"💵 Jupiter expected output: ~{out_amount_sol:.6f} SOL")
                
                # Get swap transaction
                swap_body = {
                    "quoteResponse": quote,
                    "userPublicKey": str(self.wallet.pubkey),
                    "wrapAndUnwrapSol": True,
                    "prioritizationFeeLamports": self.priority_fee,
                }
                
                for attempt in range(self.max_retries):
                    try:
                        async with session.post(jupiter_swap_url, json=swap_body) as resp:
                            if resp.status != 200:
                                error_text = await resp.text()
                                logger.warning(f"Jupiter swap request failed: {error_text}")
                                continue
                            swap_data = await resp.json()
                        
                        swap_tx_base64 = swap_data.get("swapTransaction")
                        if not swap_tx_base64:
                            return False, None, "No swap transaction in Jupiter response"
                        
                        tx_bytes = base64.b64decode(swap_tx_base64)
                        tx = VersionedTransaction.from_bytes(tx_bytes)
                        signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])
                        
                        rpc_client = await self.client.get_client()
                        
                        logger.info(f"🚀 Jupiter sell attempt {attempt + 1}/{self.max_retries}...")
                        result = await rpc_client.send_transaction(
                            signed_tx,
                            opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed)
                        )
                        sig = str(result.value)
                        
                        logger.info(f"📤 Jupiter signature: {sig}")
                        
                        await rpc_client.confirm_transaction(sig, commitment="confirmed")
                        logger.info("✅ Jupiter sell confirmed!")
                        return True, sig, None
                        
                    except Exception as e:
                        logger.warning(f"Jupiter attempt {attempt + 1} failed: {e}")
                        if attempt == self.max_retries - 1:
                            return False, None, str(e)
                
                return False, None, "All Jupiter attempts failed"
                
        except Exception as e:
            return False, None, str(e)
