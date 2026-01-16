"""
BAGS implementation of AddressProvider interface.

This module provides all BAGS specific addresses and PDA derivations
by implementing the AddressProvider interface.

BAGS uses Meteora DBC (Dynamic Bonding Curve) for token trading:
- DBC Program ID: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
- DAMM v2 Program ID: cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG (post-migration)
- Bags Fee Share V2: FEE2tBhCKAt7shrod19QttSVREUYPiyMzoku1mL1gqVK

Token characteristic: addresses end with "bags"
"""

from dataclasses import dataclass
from typing import Final

from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from core.pubkeys import SystemAddresses
from interfaces.core import AddressProvider, Platform, TokenInfo


@dataclass
class BagsAddresses:
    """BAGS program addresses - uses Meteora DBC infrastructure."""

    # Meteora DBC (Dynamic Bonding Curve) program - main trading program for BAGS tokens
    PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
    )
    # Meteora DAMM v2 - for post-migration tokens
    DAMM_V2_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
    )
    # Bags Fee Share V2 program
    FEE_SHARE_PROGRAM: Final[Pubkey] = Pubkey.from_string(
        "FEE2tBhCKAt7shrod19QttSVREUYPiyMzoku1mL1gqVK"
    )
    # Pool Authority PDA for Meteora DBC
    POOL_AUTHORITY: Final[Pubkey] = Pubkey.from_string(
        "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM"
    )
    # Migration keepers (run by Meteora to auto-migrate pools)
    MIGRATION_KEEPER_1: Final[Pubkey] = Pubkey.from_string(
        "CQdrEsYAxRqkwmpycuTwnMKggr3cr9fqY8Qma4J9TudY"
    )
    MIGRATION_KEEPER_2: Final[Pubkey] = Pubkey.from_string(
        "DeQ8dPv6ReZNQ45NfiWwS5CchWpB2BVq1QMyNV8L2uSW"
    )


class BagsAddressProvider(AddressProvider):
    """BAGS implementation of AddressProvider interface using Meteora DBC."""

    @property
    def platform(self) -> Platform:
        """Get the platform this provider serves."""
        return Platform.BAGS

    @property
    def program_id(self) -> Pubkey:
        """Get the main program ID for this platform (Meteora DBC)."""
        return BagsAddresses.PROGRAM

    def get_system_addresses(self) -> dict[str, Pubkey]:
        """Get all system addresses required for BAGS.

        Returns:
            Dictionary mapping address names to Pubkey objects
        """
        # Get system addresses from the single source of truth
        system_addresses = SystemAddresses.get_all_system_addresses()

        # Add BAGS/Meteora DBC specific addresses
        bags_addresses = {
            "program": BagsAddresses.PROGRAM,
            "damm_v2_program": BagsAddresses.DAMM_V2_PROGRAM,
            "fee_share_program": BagsAddresses.FEE_SHARE_PROGRAM,
            "pool_authority": BagsAddresses.POOL_AUTHORITY,
        }

        # Combine system and platform-specific addresses
        return {**system_addresses, **bags_addresses}


    def derive_pool_address(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None, config: Pubkey | None = None
    ) -> Pubkey:
        """Derive the pool state address for a token pair.

        For BAGS/Meteora DBC, pool is PDA of [baseMint, quoteMint, config].

        Args:
            base_mint: Base token mint address
            quote_mint: Quote token mint (defaults to WSOL)
            config: DBC config key (required for accurate derivation)

        Returns:
            Pool state address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        # Meteora DBC pool PDA: seeds = [base_mint, quote_mint, config]
        # If config not provided, we cannot derive accurately
        # This is a limitation - config must be known from token creation event
        if config is None:
            # Fallback: derive without config (may not match actual pool)
            pool_state, _ = Pubkey.find_program_address(
                [b"pool", bytes(base_mint), bytes(quote_mint)], BagsAddresses.PROGRAM
            )
        else:
            pool_state, _ = Pubkey.find_program_address(
                [bytes(base_mint), bytes(quote_mint), bytes(config)], BagsAddresses.PROGRAM
            )
        return pool_state

    def derive_base_vault(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the base vault address for a token pair.

        Args:
            base_mint: Base token mint address
            quote_mint: Quote token mint (defaults to WSOL)

        Returns:
            Base vault address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        # First derive the pool state address
        pool_state = self.derive_pool_address(base_mint, quote_mint)

        # Then derive the base vault using pool_vault seed
        base_vault, _ = Pubkey.find_program_address(
            [b"pool_vault", bytes(pool_state), bytes(base_mint)],
            BagsAddresses.PROGRAM,
        )
        return base_vault

    def derive_quote_vault(
        self, base_mint: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the quote vault address for a token pair.

        Args:
            base_mint: Base token mint address
            quote_mint: Quote token mint (defaults to WSOL)

        Returns:
            Quote vault address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        # First derive the pool state address
        pool_state = self.derive_pool_address(base_mint, quote_mint)

        # Then derive the quote vault using pool_vault seed
        quote_vault, _ = Pubkey.find_program_address(
            [b"pool_vault", bytes(pool_state), bytes(quote_mint)],
            BagsAddresses.PROGRAM,
        )
        return quote_vault

    def derive_user_token_account(
        self, user: Pubkey, mint: Pubkey, token_program_id: Pubkey | None = None
    ) -> Pubkey:
        """Derive user's associated token account address.

        Args:
            user: User's wallet address
            mint: Token mint address
            token_program_id: Token program (TOKEN or TOKEN_2022). Defaults to TOKEN_2022_PROGRAM

        Returns:
            User's associated token account address
        """
        if token_program_id is None:
            token_program_id = SystemAddresses.TOKEN_2022_PROGRAM
        return get_associated_token_address(user, mint, token_program_id)

    def get_additional_accounts(self, token_info: TokenInfo) -> dict[str, Pubkey]:
        """Get BAGS-specific additional accounts needed for trading.

        Args:
            token_info: Token information

        Returns:
            Dictionary of additional account addresses
        """
        accounts = {}

        # Add pool state - derive if not present or use existing
        if token_info.pool_state:
            accounts["pool_state"] = token_info.pool_state
        else:
            accounts["pool_state"] = self.derive_pool_address(token_info.mint)

        # Add vault addresses - derive if not present or use existing
        if token_info.base_vault:
            accounts["base_vault"] = token_info.base_vault
        else:
            accounts["base_vault"] = self.derive_base_vault(token_info.mint)

        if token_info.quote_vault:
            accounts["quote_vault"] = token_info.quote_vault
        else:
            accounts["quote_vault"] = self.derive_quote_vault(token_info.mint)

        # Derive authority PDA
        accounts["authority"] = self.derive_authority_pda()

        # Derive event authority PDA
        accounts["event_authority"] = self.derive_event_authority_pda()

        return accounts

    def derive_authority_pda(self) -> Pubkey:
        """Derive the authority PDA for BAGS.

        This PDA acts as the authority for pool vault operations.

        Returns:
            Authority PDA address
        """
        AUTH_SEED = b"vault_auth_seed"
        authority_pda, _ = Pubkey.find_program_address(
            [AUTH_SEED], BagsAddresses.PROGRAM
        )
        return authority_pda

    def derive_event_authority_pda(self) -> Pubkey:
        """Derive the event authority PDA for BAGS.

        This PDA is used for emitting program events during swaps.

        Returns:
            Event authority PDA address
        """
        EVENT_AUTHORITY_SEED = b"__event_authority"
        event_authority_pda, _ = Pubkey.find_program_address(
            [EVENT_AUTHORITY_SEED], BagsAddresses.PROGRAM
        )
        return event_authority_pda

    def derive_creator_fee_vault(
        self, creator: Pubkey, quote_mint: Pubkey | None = None
    ) -> Pubkey:
        """Derive the creator fee vault PDA.

        This vault accumulates creator fees from trades.

        Args:
            creator: The pool creator's pubkey
            quote_mint: The quote token mint (defaults to WSOL)

        Returns:
            Creator fee vault address
        """
        if quote_mint is None:
            quote_mint = SystemAddresses.SOL_MINT

        creator_fee_vault, _ = Pubkey.find_program_address(
            [bytes(creator), bytes(quote_mint)], BagsAddresses.PROGRAM
        )
        return creator_fee_vault

    def create_wsol_account_with_seed(self, payer: Pubkey, seed: str) -> Pubkey:
        """Create a WSOL account address using createAccountWithSeed pattern.

        Args:
            payer: The account that will pay for and own the new account
            seed: String seed for deterministic account generation

        Returns:
            New WSOL account address
        """
        return Pubkey.create_with_seed(payer, seed, SystemAddresses.TOKEN_PROGRAM)

    def get_buy_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a buy instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for buy instruction
        """
        additional_accounts = self.get_additional_accounts(token_info)

        # Determine token program to use
        token_program_id = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_2022_PROGRAM
        )

        # Use config from TokenInfo (extracted from pool creation event)
        # Config is required for Meteora DBC - it defines the bonding curve parameters
        config = token_info.global_config if token_info.global_config else None

        accounts = {
            "payer": user,
            "authority": BagsAddresses.POOL_AUTHORITY,
            "config": config,  # DBC config key from token creation
            "pool_state": additional_accounts["pool_state"],
            "user_base_token": self.derive_user_token_account(user, token_info.mint, token_program_id),
            "base_vault": additional_accounts["base_vault"],
            "quote_vault": additional_accounts["quote_vault"],
            "base_token_mint": token_info.mint,
            "quote_token_mint": SystemAddresses.SOL_MINT,
            "base_token_program": token_program_id,
            "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
            "event_authority": additional_accounts["event_authority"],
            "program": BagsAddresses.PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
        }

        return accounts

    def get_sell_instruction_accounts(
        self, token_info: TokenInfo, user: Pubkey
    ) -> dict[str, Pubkey]:
        """Get all accounts needed for a sell instruction.

        Args:
            token_info: Token information
            user: User's wallet address

        Returns:
            Dictionary of account addresses for sell instruction
        """
        additional_accounts = self.get_additional_accounts(token_info)

        # Determine token program to use
        token_program_id = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_2022_PROGRAM
        )

        # Use config from TokenInfo (extracted from pool creation event)
        config = token_info.global_config if token_info.global_config else None

        accounts = {
            "payer": user,
            "authority": BagsAddresses.POOL_AUTHORITY,
            "config": config,  # DBC config key from token creation
            "pool_state": additional_accounts["pool_state"],
            "user_base_token": self.derive_user_token_account(user, token_info.mint, token_program_id),
            "base_vault": additional_accounts["base_vault"],
            "quote_vault": additional_accounts["quote_vault"],
            "base_token_mint": token_info.mint,
            "quote_token_mint": SystemAddresses.SOL_MINT,
            "base_token_program": token_program_id,
            "quote_token_program": SystemAddresses.TOKEN_PROGRAM,
            "event_authority": additional_accounts["event_authority"],
            "program": BagsAddresses.PROGRAM,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
        }

        return accounts

    def get_wsol_account_creation_accounts(
        self, user: Pubkey, wsol_account: Pubkey
    ) -> dict[str, Pubkey]:
        """Get accounts needed for WSOL account creation and initialization.

        Args:
            user: User's wallet address
            wsol_account: WSOL account to be created

        Returns:
            Dictionary of account addresses for WSOL operations
        """
        return {
            "payer": user,
            "wsol_account": wsol_account,
            "wsol_mint": SystemAddresses.SOL_MINT,
            "owner": user,
            "system_program": SystemAddresses.SYSTEM_PROGRAM,
            "token_program": SystemAddresses.TOKEN_PROGRAM,
            "rent": SystemAddresses.RENT,
        }


def is_bags_token(mint_address: str) -> bool:
    """Check if token is BAGS by address suffix.
    
    BAGS tokens are identified by their mint address ending with "bags".
    
    Args:
        mint_address: Token mint address as string
        
    Returns:
        True if token is a BAGS platform token
    """
    return mint_address.lower().endswith("bags")
