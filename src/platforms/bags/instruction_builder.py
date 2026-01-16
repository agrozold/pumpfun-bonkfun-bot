"""
BAGS implementation of InstructionBuilder interface.

This module builds BAGS specific buy and sell instructions using Meteora DBC.

BAGS uses Meteora DBC (Dynamic Bonding Curve) for token trading:
- DBC Program ID: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
- Instructions: swap (single instruction for both buy and sell)
- Swap direction determined by source/destination token accounts
"""

import hashlib
import struct
import time

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountWithSeedParams, create_account_with_seed
from spl.token.instructions import create_idempotent_associated_token_account

from core.pubkeys import (
    TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE,
    TOKEN_ACCOUNT_SIZE,
    TOKEN_DECIMALS,
    SystemAddresses,
)
from interfaces.core import AddressProvider, InstructionBuilder, Platform, TokenInfo
from platforms.bags.address_provider import BagsAddresses
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class BagsInstructionBuilder(InstructionBuilder):
    """BAGS implementation of InstructionBuilder interface using Meteora DBC swap.
    
    Meteora DBC uses a single 'swap' instruction for both buy and sell operations.
    The direction is determined by which token accounts are source vs destination.
    """

    def __init__(self, idl_parser: IDLParser | None = None):
        """Initialize BAGS instruction builder with optional IDL parser.

        Args:
            idl_parser: Pre-loaded IDL parser for BAGS platform (optional)
        """
        self._idl_parser = idl_parser

        if idl_parser:
            # Get discriminators from injected IDL parser
            discriminators = self._idl_parser.get_instruction_discriminators()
            self._swap_discriminator = discriminators.get("swap", b"")
            if not self._swap_discriminator:
                self._swap_discriminator = self._compute_discriminator("swap")
            logger.info("BAGS instruction builder initialized with IDL parser")
        else:
            # Fallback: compute swap discriminator using Anchor convention
            self._swap_discriminator = self._compute_discriminator("swap")
            logger.info("BAGS instruction builder initialized without IDL (using manual discriminators)")

    def _compute_discriminator(self, instruction_name: str) -> bytes:
        """Compute instruction discriminator using Anchor convention.
        
        Args:
            instruction_name: Name of the instruction
            
        Returns:
            8-byte discriminator
        """
        # Anchor discriminator = first 8 bytes of sha256("global:<instruction_name>")
        preimage = f"global:{instruction_name}"
        return hashlib.sha256(preimage.encode()).digest()[:8]

    @property
    def platform(self) -> Platform:
        """Get the platform this builder serves."""
        return Platform.BAGS


    async def build_buy_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build buy instruction(s) for BAGS using Meteora DBC swap.

        For BUY: source = WSOL (quote), destination = base token
        
        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of SOL to spend (in lamports)
            minimum_amount_out: Minimum tokens expected (raw token units)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the buy operation
        """
        instructions = []

        # Get all required accounts
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)

        # Determine token program to use for base token
        base_token_program = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_2022_PROGRAM
        )

        # 1. Create idempotent ATA for base token (destination)
        ata_instruction = create_idempotent_associated_token_account(
            user,  # payer
            user,  # owner
            token_info.mint,  # mint
            base_token_program,  # token program (dynamic for token2022 support)
        )
        instructions.append(ata_instruction)

        # 2. Create WSOL account with seed (source - temporary account for the transaction)
        wsol_seed = self._generate_wsol_seed(user)
        wsol_account = address_provider.create_wsol_account_with_seed(user, wsol_seed)

        # Account creation cost + amount to spend
        account_creation_lamports = TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE
        total_lamports = amount_in + account_creation_lamports

        create_wsol_ix = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=user,
                to_pubkey=wsol_account,
                base=user,
                seed=wsol_seed,
                lamports=total_lamports,
                space=TOKEN_ACCOUNT_SIZE,
                owner=SystemAddresses.TOKEN_PROGRAM,
            )
        )
        instructions.append(create_wsol_ix)

        # 3. Initialize WSOL account
        initialize_wsol_ix = self._create_initialize_account_instruction(
            wsol_account, SystemAddresses.SOL_MINT, user
        )
        instructions.append(initialize_wsol_ix)

        # 4. Build swap instruction for BUY (quote -> base)
        # Account order from IDL: pool, config, userSourceToken, userDestinationToken,
        # baseVault, quoteVault, baseMint, quoteMint, user, tokenProgram, eventAuthority, program
        swap_accounts = [
            AccountMeta(
                pubkey=accounts_info["pool_state"], is_signer=False, is_writable=True
            ),  # pool
            AccountMeta(
                pubkey=accounts_info["config"], is_signer=False, is_writable=False
            ) if accounts_info.get("config") else AccountMeta(
                pubkey=BagsAddresses.POOL_AUTHORITY, is_signer=False, is_writable=False
            ),  # config
            AccountMeta(
                pubkey=wsol_account, is_signer=False, is_writable=True
            ),  # userSourceToken (WSOL - spending)
            AccountMeta(
                pubkey=accounts_info["user_base_token"],
                is_signer=False,
                is_writable=True,
            ),  # userDestinationToken (base token - receiving)
            AccountMeta(
                pubkey=accounts_info["base_vault"], is_signer=False, is_writable=True
            ),  # baseVault
            AccountMeta(
                pubkey=accounts_info["quote_vault"], is_signer=False, is_writable=True
            ),  # quoteVault
            AccountMeta(
                pubkey=token_info.mint, is_signer=False, is_writable=False
            ),  # baseMint
            AccountMeta(
                pubkey=SystemAddresses.SOL_MINT, is_signer=False, is_writable=False
            ),  # quoteMint
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),  # user
            AccountMeta(
                pubkey=SystemAddresses.TOKEN_PROGRAM, is_signer=False, is_writable=False
            ),  # tokenProgram (for WSOL)
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),  # eventAuthority
            AccountMeta(
                pubkey=BagsAddresses.PROGRAM, is_signer=False, is_writable=False
            ),  # program
        ]

        # Build instruction data: discriminator + amountIn + minimumAmountOut
        instruction_data = (
            self._swap_discriminator
            + struct.pack("<Q", amount_in)  # amountIn (u64) - SOL to spend
            + struct.pack("<Q", minimum_amount_out)  # minimumAmountOut (u64) - min tokens
        )

        swap_instruction = Instruction(
            program_id=BagsAddresses.PROGRAM,
            data=instruction_data,
            accounts=swap_accounts,
        )
        instructions.append(swap_instruction)

        # 5. Close WSOL account to reclaim remaining SOL
        close_wsol_ix = self._create_close_account_instruction(wsol_account, user, user)
        instructions.append(close_wsol_ix)

        return instructions

    async def build_sell_instruction(
        self,
        token_info: TokenInfo,
        user: Pubkey,
        amount_in: int,
        minimum_amount_out: int,
        address_provider: AddressProvider,
    ) -> list[Instruction]:
        """Build sell instruction(s) for BAGS using Meteora DBC swap.

        For SELL: source = base token, destination = WSOL (quote)
        
        Args:
            token_info: Token information
            user: User's wallet address
            amount_in: Amount of tokens to sell (raw token units)
            minimum_amount_out: Minimum SOL expected (in lamports)
            address_provider: Platform address provider

        Returns:
            List of instructions needed for the sell operation
        """
        instructions = []

        # Get all required accounts
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)

        # Determine token program to use for base token
        base_token_program = (
            token_info.token_program_id
            if token_info.token_program_id
            else SystemAddresses.TOKEN_2022_PROGRAM
        )

        # 1. Create WSOL account with seed (destination - to receive SOL)
        wsol_seed = self._generate_wsol_seed(user)
        wsol_account = address_provider.create_wsol_account_with_seed(user, wsol_seed)

        # Minimal account creation cost
        account_creation_lamports = TOKEN_ACCOUNT_RENT_EXEMPT_RESERVE

        create_wsol_ix = create_account_with_seed(
            CreateAccountWithSeedParams(
                from_pubkey=user,
                to_pubkey=wsol_account,
                base=user,
                seed=wsol_seed,
                lamports=account_creation_lamports,
                space=TOKEN_ACCOUNT_SIZE,
                owner=SystemAddresses.TOKEN_PROGRAM,
            )
        )
        instructions.append(create_wsol_ix)

        # 2. Initialize WSOL account
        initialize_wsol_ix = self._create_initialize_account_instruction(
            wsol_account, SystemAddresses.SOL_MINT, user
        )
        instructions.append(initialize_wsol_ix)

        # 3. Build swap instruction for SELL (base -> quote)
        # Account order from IDL: pool, config, userSourceToken, userDestinationToken,
        # baseVault, quoteVault, baseMint, quoteMint, user, tokenProgram, eventAuthority, program
        swap_accounts = [
            AccountMeta(
                pubkey=accounts_info["pool_state"], is_signer=False, is_writable=True
            ),  # pool
            AccountMeta(
                pubkey=accounts_info["config"], is_signer=False, is_writable=False
            ) if accounts_info.get("config") else AccountMeta(
                pubkey=BagsAddresses.POOL_AUTHORITY, is_signer=False, is_writable=False
            ),  # config
            AccountMeta(
                pubkey=accounts_info["user_base_token"],
                is_signer=False,
                is_writable=True,
            ),  # userSourceToken (base token - selling)
            AccountMeta(
                pubkey=wsol_account, is_signer=False, is_writable=True
            ),  # userDestinationToken (WSOL - receiving)
            AccountMeta(
                pubkey=accounts_info["base_vault"], is_signer=False, is_writable=True
            ),  # baseVault
            AccountMeta(
                pubkey=accounts_info["quote_vault"], is_signer=False, is_writable=True
            ),  # quoteVault
            AccountMeta(
                pubkey=token_info.mint, is_signer=False, is_writable=False
            ),  # baseMint
            AccountMeta(
                pubkey=SystemAddresses.SOL_MINT, is_signer=False, is_writable=False
            ),  # quoteMint
            AccountMeta(pubkey=user, is_signer=True, is_writable=True),  # user
            AccountMeta(
                pubkey=base_token_program, is_signer=False, is_writable=False
            ),  # tokenProgram (for base token)
            AccountMeta(
                pubkey=accounts_info["event_authority"],
                is_signer=False,
                is_writable=False,
            ),  # eventAuthority
            AccountMeta(
                pubkey=BagsAddresses.PROGRAM, is_signer=False, is_writable=False
            ),  # program
        ]

        # Build instruction data: discriminator + amountIn + minimumAmountOut
        instruction_data = (
            self._swap_discriminator
            + struct.pack("<Q", amount_in)  # amountIn (u64) - tokens to sell
            + struct.pack("<Q", minimum_amount_out)  # minimumAmountOut (u64) - min SOL
        )

        swap_instruction = Instruction(
            program_id=BagsAddresses.PROGRAM,
            data=instruction_data,
            accounts=swap_accounts,
        )
        instructions.append(swap_instruction)

        # 4. Close WSOL account to reclaim SOL (converts WSOL back to SOL)
        close_wsol_ix = self._create_close_account_instruction(wsol_account, user, user)
        instructions.append(close_wsol_ix)

        return instructions

    def get_required_accounts_for_buy(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for buy operation (for priority fee calculation).

        Args:
            token_info: Token information
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        accounts_info = address_provider.get_buy_instruction_accounts(token_info, user)

        return [
            accounts_info["pool_state"],
            accounts_info["user_base_token"],
            accounts_info["base_vault"],
            accounts_info["quote_vault"],
            token_info.mint,
            SystemAddresses.SOL_MINT,
            accounts_info["program"],
        ]

    def get_required_accounts_for_sell(
        self, token_info: TokenInfo, user: Pubkey, address_provider: AddressProvider
    ) -> list[Pubkey]:
        """Get list of accounts required for sell operation (for priority fee calculation).

        Args:
            token_info: Token information
            user: User's wallet address
            address_provider: Platform address provider

        Returns:
            List of account addresses that will be accessed
        """
        accounts_info = address_provider.get_sell_instruction_accounts(token_info, user)

        return [
            accounts_info["pool_state"],
            accounts_info["user_base_token"],
            accounts_info["base_vault"],
            accounts_info["quote_vault"],
            token_info.mint,
            SystemAddresses.SOL_MINT,
            accounts_info["program"],
        ]

    def _generate_wsol_seed(self, user: Pubkey) -> str:
        """Generate a unique seed for WSOL account creation.

        Args:
            user: User's wallet address

        Returns:
            Unique seed string for WSOL account
        """
        # Generate a unique seed based on timestamp and user pubkey
        seed_data = f"{int(time.time())}{user!s}"
        return hashlib.sha256(seed_data.encode()).hexdigest()[:32]

    def _create_initialize_account_instruction(
        self, account: Pubkey, mint: Pubkey, owner: Pubkey
    ) -> Instruction:
        """Create an InitializeAccount instruction for the Token Program.

        Args:
            account: The account to initialize
            mint: The token mint
            owner: The account owner

        Returns:
            Instruction for initializing the account
        """
        accounts = [
            AccountMeta(pubkey=account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=owner, is_signer=False, is_writable=False),
            AccountMeta(
                pubkey=SystemAddresses.RENT, is_signer=False, is_writable=False
            ),
        ]

        # InitializeAccount instruction discriminator (instruction 1 in Token Program)
        data = bytes([1])

        return Instruction(
            program_id=SystemAddresses.TOKEN_PROGRAM, data=data, accounts=accounts
        )

    def _create_close_account_instruction(
        self, account: Pubkey, destination: Pubkey, owner: Pubkey
    ) -> Instruction:
        """Create a CloseAccount instruction for the Token Program.

        Args:
            account: The account to close
            destination: Where to send the remaining lamports
            owner: The account owner (must sign)

        Returns:
            Instruction for closing the account
        """
        accounts = [
            AccountMeta(pubkey=account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=destination, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ]

        # CloseAccount instruction discriminator (instruction 9 in Token Program)
        data = bytes([9])

        return Instruction(
            program_id=SystemAddresses.TOKEN_PROGRAM, data=data, accounts=accounts
        )

    def calculate_token_amount_raw(self, token_amount_decimal: float) -> int:
        """Convert decimal token amount to raw token units.

        Args:
            token_amount_decimal: Token amount in decimal form

        Returns:
            Token amount in raw units (adjusted for decimals)
        """
        return int(token_amount_decimal * 10**TOKEN_DECIMALS)

    def calculate_token_amount_decimal(self, token_amount_raw: int) -> float:
        """Convert raw token amount to decimal form.

        Args:
            token_amount_raw: Token amount in raw units

        Returns:
            Token amount in decimal form
        """
        return token_amount_raw / 10**TOKEN_DECIMALS

    def get_buy_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for BAGS buy operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for buy operations
        """
        if config_override is not None:
            return config_override
        # Buy operations: ATA creation + WSOL creation/init/close + buy instruction
        return 150_000

    def get_sell_compute_unit_limit(self, config_override: int | None = None) -> int:
        """Get the recommended compute unit limit for BAGS sell operations.

        Args:
            config_override: Optional override from configuration

        Returns:
            Compute unit limit appropriate for sell operations
        """
        if config_override is not None:
            return config_override
        # Sell operations: WSOL creation/init/close + sell instruction
        return 150_000
