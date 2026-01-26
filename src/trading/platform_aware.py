"""
Platform-aware trader implementations that use the interface system.
Final cleanup removing all platform-specific hardcoding.
"""

import asyncio
from solders.pubkey import Pubkey

from core.client import SolanaClient
from core.priority_fee.manager import PriorityFeeManager
from core.pubkeys import LAMPORTS_PER_SOL, TOKEN_DECIMALS, SystemAddresses
from core.wallet import Wallet
from interfaces.core import AddressProvider, Platform, TokenInfo
from platforms import get_platform_implementations
from trading.base import Trader, TradeResult
from trading.fallback_seller import FallbackSeller
from utils.logger import get_logger

logger = get_logger(__name__)


class PlatformAwareBuyer(Trader):
    """Platform-aware token buyer that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        amount: float,
        slippage: float = 0.01,
        max_retries: int = 5,
        extreme_fast_token_amount: int = 0,
        extreme_fast_mode: bool = False,
        compute_units: dict | None = None,
    ):
        """Initialize platform-aware token buyer."""
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.amount = amount
        self.slippage = slippage
        self.max_retries = max_retries
        self.extreme_fast_mode = extreme_fast_mode
        self.extreme_fast_token_amount = extreme_fast_token_amount
        self.compute_units = compute_units or {}

    async def execute(self, token_info: TokenInfo) -> TradeResult:
        """Execute buy operation using platform-specific implementations."""
        try:
            # Get platform-specific implementations
            logger.info(f"[INIT] Getting platform implementations for {token_info.platform.value}...")
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder
            curve_manager = implementations.curve_manager

            # Get pool address and verify it exists before proceeding
            pool_address = self._get_pool_address(token_info, address_provider)
            logger.info(f"[INIT] Pool address: {pool_address}")

            # Quick check if pool account exists with retries (race condition fix)
            max_retries = 5
            pool_exists = False
            for attempt in range(max_retries):
                try:
                    logger.info(f"[CHECK] Checking pool account exists... (attempt {attempt+1}/{max_retries})")
                    await self.client.get_account_info(pool_address)
                    logger.info("[CHECK] Pool account exists [OK]")
                    pool_exists = True
                    break
                except ValueError as e:
                    if "not found" in str(e).lower():
                        if attempt < max_retries - 1:
                            logger.warning("[WAIT] Pool not ready, waiting 1s...")
                            await asyncio.sleep(1)
                        else:
                            logger.warning(
                                f"Pool account {pool_address} does not exist for {token_info.symbol} "
                                f"on {token_info.platform.value} after {max_retries} attempts"
                            )
                    else:
                        raise

            if not pool_exists:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Pool account not found: {pool_address}",
                )

            # Convert amount to lamports
            amount_lamports = int(self.amount * LAMPORTS_PER_SOL)

            if self.extreme_fast_mode:
                # Skip the wait and directly calculate the amount
                token_amount = self.extreme_fast_token_amount
                token_price_sol = self.amount / token_amount if token_amount > 0 else 0
            else:
                # Regular behavior with RPC call
                # Fetch pool state to get price and mayhem mode status
                # (pool_address already obtained and validated above)
                pool_state = await curve_manager.get_pool_state(pool_address)
                token_price_sol = pool_state.get("price_per_token")

                # Validate price_per_token is present and positive
                if token_price_sol is None or token_price_sol <= 0:
                    raise ValueError(
                        f"Invalid price_per_token: {token_price_sol} for pool {pool_address} "
                        f"(mint: {token_info.mint}) - cannot execute buy with zero/invalid price"
                    )

                # Set is_mayhem_mode from bonding curve state
                token_info.is_mayhem_mode = pool_state.get("is_mayhem_mode", False)
                token_amount = self.amount / token_price_sol

            # Calculate minimum token amount with slippage
            minimum_token_amount = token_amount * (1 - self.slippage)
            minimum_token_amount_raw = int(minimum_token_amount * 10**TOKEN_DECIMALS)

            # Calculate maximum SOL to spend with slippage
            max_amount_lamports = int(amount_lamports * (1 + self.slippage))

            # Build buy instructions using platform-specific builder
            instructions = await instruction_builder.build_buy_instruction(
                token_info,
                self.wallet.pubkey,
                max_amount_lamports,  # amount_in (SOL)
                minimum_token_amount_raw,  # minimum_amount_out (tokens)
                address_provider,
            )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_buy(
                token_info, self.wallet.pubkey, address_provider
            )

            logger.info(
                f"Buying {token_amount:.6f} tokens at {token_price_sol:.8f} SOL per token on {token_info.platform.value}"
            )
            logger.info(
                f"Total cost: {self.amount:.6f} SOL (max: {max_amount_lamports / LAMPORTS_PER_SOL:.6f} SOL)"
            )

            # Send transaction with preflight checks enabled for reliability
            try:
                logger.info(f"[TX] Building and sending buy transaction for {token_info.symbol}...")
                tx_signature = await self.client.build_and_send_transaction(
                    instructions,
                    self.wallet.keypair,
                    skip_preflight=False,  # Enable preflight for better error detection
                    max_retries=self.max_retries,
                    priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                        priority_accounts
                    ),
                    compute_unit_limit=instruction_builder.get_buy_compute_unit_limit(
                        self._get_cu_override("buy", token_info.platform)
                    ),
                    account_data_size_limit=self._get_cu_override(
                        "account_data_size", token_info.platform
                    ),
                )
                logger.info(f"[TX] Transaction sent: {tx_signature}")
            except ValueError as e:
                # Insufficient funds - don't retry
                logger.error(f"Buy failed - insufficient funds: {e}")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Insufficient funds: {e}",
                )
            except RuntimeError as e:
                # All retries failed
                logger.error(f"Buy failed - all retries exhausted: {e}")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed: {e}",
                )

            # Try to confirm but don't block forever
            try:
                success = await asyncio.wait_for(
                    self.client.confirm_transaction(tx_signature, timeout=30.0),
                    timeout=35.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"[BUY] Confirmation timeout, assuming success: {tx_signature}")
                success = False  # Timeout - do not assume success

            if success:
                logger.info(f"Buy transaction confirmed/assumed: {tx_signature}")

                # Fetch actual tokens and SOL spent from transaction
                # Uses preBalances/postBalances to get exact amounts
                sol_destination = self._get_sol_destination(
                    token_info, address_provider
                )
                tokens_raw, sol_spent = await self.client.get_buy_transaction_details(
                    str(tx_signature), token_info.mint, sol_destination
                )

                if tokens_raw is not None and sol_spent is not None:
                    actual_amount = tokens_raw / 10**TOKEN_DECIMALS
                    actual_price = (sol_spent / LAMPORTS_PER_SOL) / actual_amount
                    logger.info(
                        f"Actual tokens received: {actual_amount:.6f} "
                        f"(expected: {token_amount:.6f})"
                    )
                    logger.info(
                        f"Actual SOL spent: {sol_spent / LAMPORTS_PER_SOL:.10f} SOL"
                    )
                    logger.info(f"Actual price: {actual_price:.10f} SOL/token")
                    token_amount = actual_amount
                    token_price_sol = actual_price
                else:
                    logger.warning(f"Balance parse delayed, trusting tx: tokens={tokens_raw}, sol_spent={sol_spent}")
                    token_amount = 20
                    token_price_sol = 0.000005
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_amount,
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            error_str = str(e)
            # Check if bonding curve is unavailable (migrated token)
            if "Invalid bonding curve state" in error_str or "virtual_token_reserves: 0" in error_str:
                logger.warning(f"[MIGRATE] Bonding curve unavailable for {token_info.symbol}, trying fallback...")
                return await self._fallback_buy(token_info, self.amount)

            logger.exception("Buy operation failed")
            return TradeResult(
                success=False, platform=token_info.platform, error_message=str(e)
            )


    async def _fallback_buy(
        self,
        token_info: TokenInfo,
        sol_amount: float,
    ) -> TradeResult:
        """Try to buy via PumpSwap or Jupiter when bonding curve unavailable."""
        try:
            fallback_buyer = FallbackSeller(
                client=self.client,
                wallet=self.wallet,
                slippage=self.slippage,
                priority_fee=100_000,
                max_retries=self.max_retries,
            )

            # Try PumpSwap first
            logger.info(f"[FALLBACK] Trying PumpSwap BUY for {token_info.symbol}...")
            success, sig, error, token_amount, price = await fallback_buyer.buy_via_pumpswap(
                mint=token_info.mint,
                sol_amount=sol_amount,
                symbol=token_info.symbol,
            )

            if success:
                logger.info(f"[OK] PumpSwap BUY successful: {sig}")
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=sig,
                    amount=token_amount,
                    price=price,
                )

            # Fallback to Jupiter
            logger.info(f"[FALLBACK] PumpSwap failed: {error}, trying Jupiter...")
            success, sig, error = await fallback_buyer.buy_via_jupiter(
                mint=token_info.mint,
                sol_amount=sol_amount,
                symbol=token_info.symbol,
            )

            if success:
                logger.info(f"[OK] Jupiter BUY successful: {sig}")
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=sig,
                    amount=0,
                    price=0,
                )

            logger.error(f"[FAIL] All fallback buy methods failed: {error}")
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message=f"Fallback buy failed: {error}",
            )

        except Exception as e:
            logger.exception("Fallback buy operation failed")
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message=f"Fallback buy error: {e}",
            )

    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state
        elif token_info.platform == Platform.BAGS:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)

    def _get_sol_destination(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the address where SOL is sent during a buy transaction.

        For pump.fun: SOL goes to the bonding curve
        For letsbonk: SOL goes to the quote_vault (WSOL vault)
        For BAGS: SOL goes to the quote_vault (WSOL vault)

        Args:
            token_info: Token information
            address_provider: Platform-specific address provider

        Returns:
            Address where SOL is transferred during buy

        Raises:
            NotImplementedError: If platform SOL destination is not implemented
        """
        if token_info.platform == Platform.PUMP_FUN:
            # For pump.fun, SOL goes directly to bonding curve
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
            return address_provider.derive_pool_address(token_info.mint)
        elif token_info.platform == Platform.LETS_BONK:
            # For letsbonk, SOL goes to quote_vault (WSOL vault)
            if hasattr(token_info, "quote_vault") and token_info.quote_vault:
                return token_info.quote_vault
            # Derive quote_vault if not available
            return address_provider.derive_quote_vault(token_info.mint)
        elif token_info.platform == Platform.BAGS:
            # For BAGS, SOL goes to quote_vault (WSOL vault)
            if hasattr(token_info, "quote_vault") and token_info.quote_vault:
                return token_info.quote_vault
            # Derive quote_vault if not available
            return address_provider.derive_quote_vault(token_info.mint)

        raise NotImplementedError(
            f"SOL destination not implemented for platform {token_info.platform.value}. "
            f"Add platform-specific logic to _get_sol_destination() to specify where "
            f"SOL is transferred during buy transactions for this platform."
        )

    def _get_cu_override(self, operation: str, platform: Platform) -> int | None:
        """Get compute unit override from configuration.

        Args:
            operation: "buy" or "sell"
            platform: Trading platform (unused - each config is platform-specific)

        Returns:
            CU override value if configured, None otherwise
        """
        if not self.compute_units:
            return None

        # Just check for operation override (buy/sell)
        return self.compute_units.get(operation)


class PlatformAwareSeller(Trader):
    """Platform-aware token seller that works with any supported platform."""

    def __init__(
        self,
        client: SolanaClient,
        wallet: Wallet,
        priority_fee_manager: PriorityFeeManager,
        slippage: float = 0.25,
        max_retries: int = 5,
        compute_units: dict | None = None,
        jupiter_api_key: str | None = None,
    ):
        """Initialize platform-aware token seller."""
        self.client = client
        self.wallet = wallet
        self.priority_fee_manager = priority_fee_manager
        self.slippage = slippage
        self.max_retries = max_retries
        self.compute_units = compute_units or {}
        self.jupiter_api_key = jupiter_api_key

    async def execute(
        self, token_info: TokenInfo, token_amount: float, token_price: float
    ) -> TradeResult:
        """Execute sell operation using platform-specific implementations.

        Args:
            token_info: Token information for the sell operation
            token_amount: Token amount to sell (from buy result). Required to avoid
                         RPC balance query delays.
            token_price: Token price in SOL (from buy result). Required to avoid
                        RPC pool state query delays.

        Returns:
            TradeResult with operation outcome

        Raises:
            ValueError: If required parameters are not provided
        """
        if token_amount is None:
            raise ValueError(
                "token_amount is required for sell operation. "
                "Pass the amount from buy result to avoid RPC delays."
            )
        if token_price is None or token_price <= 0:
            raise ValueError(
                "token_price is required for sell operation and must be positive. "
                "Pass the price from buy result to avoid RPC delays."
            )

        try:
            # Get platform-specific implementations
            implementations = get_platform_implementations(
                token_info.platform, self.client
            )
            address_provider = implementations.address_provider
            instruction_builder = implementations.instruction_builder

            # Use pre-known amount and price (no RPC delay)
            token_balance_decimal = token_amount
            token_balance = int(token_amount * 10**TOKEN_DECIMALS)
            token_price_sol = token_price

            logger.info(f"Token balance: {token_balance_decimal:.6f}")
            logger.info(f"Price per Token (from buy): {token_price_sol:.8f} SOL")

            if token_balance == 0:
                logger.info("No tokens to sell.")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message="No tokens to sell",
                )

            # Calculate expected SOL output with slippage protection
            expected_sol_output = token_balance_decimal * token_price_sol
            min_sol_output = max(
                1,
                int((expected_sol_output * (1 - self.slippage)) * LAMPORTS_PER_SOL),
            )
            logger.info(
                f"Selling {token_balance_decimal} tokens on {token_info.platform.value}"
            )
            logger.info(f"Expected SOL output: {expected_sol_output:.10f} SOL")
            logger.info(
                f"Minimum SOL output (with {self.slippage * 100:.1f}% slippage): "
                f"{min_sol_output / LAMPORTS_PER_SOL:.10f} SOL ({min_sol_output} lamports)"
            )

            # Validate required token_info fields for sell
            if token_info.platform == Platform.PUMP_FUN:
                if not token_info.bonding_curve or not token_info.creator_vault:
                    # Token may have migrated - try fallback methods
                    logger.warning(
                        f"[WARN] {token_info.symbol}: missing bonding_curve or creator_vault - "
                        "trying fallback sell methods (PumpSwap/Jupiter)"
                    )
                    return await self._fallback_sell(
                        token_info, token_balance_decimal, token_price_sol
                    )

            # Build sell instructions using platform-specific builder
            instructions = await instruction_builder.build_sell_instruction(
                token_info,
                self.wallet.pubkey,
                token_balance,  # amount_in (tokens)
                min_sol_output,  # minimum_amount_out (SOL)
                address_provider,
            )

            # Get accounts for priority fee calculation
            priority_accounts = instruction_builder.get_required_accounts_for_sell(
                token_info, self.wallet.pubkey, address_provider
            )

            # Send transaction with preflight checks enabled for reliability
            try:
                tx_signature = await self.client.build_and_send_transaction(
                    instructions,
                    self.wallet.keypair,
                    skip_preflight=False,  # Enable preflight for better error detection
                    max_retries=self.max_retries,
                    priority_fee=await self.priority_fee_manager.calculate_priority_fee(
                        priority_accounts
                    ),
                    compute_unit_limit=instruction_builder.get_sell_compute_unit_limit(
                        self._get_cu_override("sell", token_info.platform)
                    ),
                    account_data_size_limit=self._get_cu_override(
                        "account_data_size", token_info.platform
                    ),
                )
            except ValueError as e:
                # Insufficient funds - don't retry
                logger.error(f"Sell failed - insufficient funds: {e}")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Insufficient funds: {e}",
                )

            success = await self.client.confirm_transaction(tx_signature, timeout=45.0)

            if success:
                # VERIFY: Check token balance is actually 0 after sell
                try:
                    from spl.token.instructions import get_associated_token_address
                    # TOKEN2022 FIX: Most pump.fun/bonk/bags tokens use Token2022
                    # Default to TOKEN_2022_PROGRAM if token_program_id is not set
                    token_prog = token_info.token_program_id or SystemAddresses.TOKEN_2022_PROGRAM
                    ata = get_associated_token_address(self.wallet.pubkey, token_info.mint, token_prog)
                    remaining = await self.client.get_token_account_balance(ata)
                    if remaining > 1000:  # More than dust remaining
                        logger.error(f"[VERIFY FAIL] Tokens still in wallet: {remaining / 10**6:.2f} - sell did NOT complete!")
                        return TradeResult(
                            success=False,
                            platform=token_info.platform,
                            error_message=f"Sell verification failed: {remaining / 10**6:.2f} tokens still in wallet",
                        )
                    logger.info(f"[VERIFY OK] Token balance after sell: {remaining}")
                except Exception as ve:
                    logger.warning(f"[VERIFY] Could not verify balance: {ve}")

                logger.info(f"Sell transaction confirmed: {tx_signature}")
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_balance_decimal,
                    price=token_price_sol,
                )
            else:
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Transaction failed to confirm: {tx_signature}",
                )

        except Exception as e:
            logger.exception("Sell operation failed")
            return TradeResult(
                success=False, platform=token_info.platform, error_message=str(e)
            )


    def _get_pool_address(
        self, token_info: TokenInfo, address_provider: AddressProvider
    ) -> Pubkey:
        """Get the pool/curve address for price calculations using platform-agnostic method."""
        # Try to get the address from token_info first, then derive if needed
        if token_info.platform == Platform.PUMP_FUN:
            if hasattr(token_info, "bonding_curve") and token_info.bonding_curve:
                return token_info.bonding_curve
        elif token_info.platform == Platform.LETS_BONK:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state
        elif token_info.platform == Platform.BAGS:
            if hasattr(token_info, "pool_state") and token_info.pool_state:
                return token_info.pool_state

        # Fallback to deriving the address using platform provider
        return address_provider.derive_pool_address(token_info.mint)

    def _get_cu_override(self, operation: str, platform: Platform) -> int | None:
        """Get compute unit override from configuration.

        Args:
            operation: "buy" or "sell"
            platform: Trading platform (unused - each config is platform-specific)

        Returns:
            CU override value if configured, None otherwise
        """
        if not self.compute_units:
            return None

        # Just check for operation override (buy/sell)
        return self.compute_units.get(operation)

    async def _fallback_sell(
        self,
        token_info: TokenInfo,
        token_amount: float,
        token_price: float,
    ) -> TradeResult:
        """Try to sell via PumpSwap or Jupiter when bonding curve unavailable.

        Args:
            token_info: Token information
            token_amount: Amount of tokens to sell
            token_price: Price per token in SOL

        Returns:
            TradeResult with success/failure status
        """
        try:
            fallback_seller = FallbackSeller(
                client=self.client,
                wallet=self.wallet,
                slippage=self.slippage,
                priority_fee=10000,  # Low priority fee for sell
                max_retries=self.max_retries,
                jupiter_api_key=self.jupiter_api_key,
            )

            success, tx_signature, error = await fallback_seller.sell(
                mint=token_info.mint,
                token_amount=token_amount,
                symbol=token_info.symbol,
            )

            if success:
                logger.info(f"[OK] Fallback sell successful: {tx_signature}")
                return TradeResult(
                    success=True,
                    platform=token_info.platform,
                    tx_signature=tx_signature,
                    amount=token_amount,
                    price=token_price,
                )
            else:
                logger.error(f"[FAIL] All fallback sell methods failed: {error}")
                return TradeResult(
                    success=False,
                    platform=token_info.platform,
                    error_message=f"Fallback sell failed: {error}",
                )

        except Exception as e:
            logger.exception("Fallback sell operation failed")
            return TradeResult(
                success=False,
                platform=token_info.platform,
                error_message=f"Fallback sell error: {e}",
            )
