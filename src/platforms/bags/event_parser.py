"""
BAGS implementation of EventParser interface.

This module parses BAGS-specific token creation events from various sources
by implementing the EventParser interface.

BAGS uses Meteora DBC (Dynamic Bonding Curve) for token launches:
- DBC Program ID: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
- Token characteristic: addresses end with "bags"
- Creation instruction: initialize_virtual_pool_with_spl_token
- Creation event: EvtInitializeVirtualPoolWithSplToken
"""

import base64
import hashlib
import struct
from time import monotonic
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.pubkeys import SystemAddresses
from interfaces.core import EventParser, Platform, TokenInfo
from platforms.bags.address_provider import BagsAddressProvider, BagsAddresses
from utils.idl_parser import IDLParser
from utils.logger import get_logger

logger = get_logger(__name__)


class BagsEventParser(EventParser):
    """BAGS implementation of EventParser interface for Meteora DBC events."""

    def __init__(self, idl_parser: IDLParser | None = None):
        """Initialize BAGS event parser with optional IDL parser.

        Args:
            idl_parser: Pre-loaded IDL parser for BAGS platform (optional)
        """
        self.address_provider = BagsAddressProvider()
        self._idl_parser = idl_parser

        # Compute discriminator for initialize_virtual_pool_with_spl_token
        self._initialize_discriminator = self._compute_discriminator(
            "initialize_virtual_pool_with_spl_token"
        )
        self._initialize_discriminator_int = struct.unpack("<Q", self._initialize_discriminator)[0]

        if idl_parser:
            # Try to get discriminator from IDL if available
            discriminators = self._idl_parser.get_instruction_discriminators()
            if "initialize_virtual_pool_with_spl_token" in discriminators:
                self._initialize_discriminator = discriminators["initialize_virtual_pool_with_spl_token"]
                self._initialize_discriminator_int = struct.unpack("<Q", self._initialize_discriminator)[0]
            logger.info("BAGS event parser initialized with IDL parser")
        else:
            logger.info("BAGS event parser initialized without IDL (using manual discriminators)")

    def _compute_discriminator(self, instruction_name: str) -> bytes:
        """Compute instruction discriminator using Anchor convention.
        
        Args:
            instruction_name: Name of the instruction (snake_case)
            
        Returns:
            8-byte discriminator
        """
        preimage = f"global:{instruction_name}"
        return hashlib.sha256(preimage.encode()).digest()[:8]

    @property
    def platform(self) -> Platform:
        """Get the platform this parser serves."""
        return Platform.BAGS

    def parse_token_creation_from_logs(
        self, logs: list[str], signature: str
    ) -> TokenInfo | None:
        """Parse token creation from BAGS transaction logs.

        Looks for EvtInitializeVirtualPoolWithSplToken event in Program data logs.

        Args:
            logs: List of log strings from transaction
            signature: Transaction signature

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        bags_program_str = str(BagsAddresses.PROGRAM)
        
        for log in logs:
            # Check for DBC program invocation
            if bags_program_str in log:
                logger.debug(f"BAGS program activity detected: {signature}")
                
            # Look for Program data (event emission)
            if "Program data:" in log:
                try:
                    encoded_data = log.split("Program data: ")[1].strip()
                    decoded_data = base64.b64decode(encoded_data)
                    
                    # Try to parse as EvtInitializeVirtualPoolWithSplToken
                    token_info = self._parse_initialize_event(decoded_data, signature)
                    if token_info:
                        return token_info
                except Exception as e:
                    logger.debug(f"Failed to parse Program data: {e}")
                    
        return None

    def _parse_initialize_event(self, data: bytes, signature: str) -> TokenInfo | None:
        """Parse EvtInitializeVirtualPoolWithSplToken event data.
        
        Event structure from IDL:
        - name: string
        - symbol: string
        - uri: string
        - creator: publicKey
        - mint: publicKey
        - pool: publicKey
        - quoteMint: publicKey
        - baseVault: publicKey
        - quoteVault: publicKey
        
        Args:
            data: Decoded event data
            signature: Transaction signature
            
        Returns:
            TokenInfo if valid event, None otherwise
        """
        try:
            # Skip 8-byte event discriminator
            offset = 8
            
            # Read name (string: 4-byte length + data)
            if offset + 4 > len(data):
                return None
            name_len = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            if offset + name_len > len(data):
                return None
            name = data[offset:offset + name_len].decode("utf-8", errors="ignore")
            offset += name_len
            
            # Read symbol
            if offset + 4 > len(data):
                return None
            symbol_len = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            if offset + symbol_len > len(data):
                return None
            symbol = data[offset:offset + symbol_len].decode("utf-8", errors="ignore")
            offset += symbol_len
            
            # Read uri
            if offset + 4 > len(data):
                return None
            uri_len = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            if offset + uri_len > len(data):
                return None
            uri = data[offset:offset + uri_len].decode("utf-8", errors="ignore")
            offset += uri_len
            
            # Read creator (publicKey - 32 bytes)
            if offset + 32 > len(data):
                return None
            creator = Pubkey.from_bytes(data[offset:offset + 32])
            offset += 32
            
            # Read mint (publicKey - 32 bytes)
            if offset + 32 > len(data):
                return None
            mint = Pubkey.from_bytes(data[offset:offset + 32])
            offset += 32
            
            # Read pool (publicKey - 32 bytes)
            if offset + 32 > len(data):
                return None
            pool = Pubkey.from_bytes(data[offset:offset + 32])
            offset += 32
            
            # Read quoteMint (publicKey - 32 bytes)
            if offset + 32 > len(data):
                return None
            quote_mint = Pubkey.from_bytes(data[offset:offset + 32])
            offset += 32
            
            # Read baseVault (publicKey - 32 bytes)
            if offset + 32 > len(data):
                return None
            base_vault = Pubkey.from_bytes(data[offset:offset + 32])
            offset += 32
            
            # Read quoteVault (publicKey - 32 bytes)
            if offset + 32 > len(data):
                return None
            quote_vault = Pubkey.from_bytes(data[offset:offset + 32])
            
            # Note: bags.fm tokens are identified by being created via Meteora DBC program,
            # NOT by mint address suffix. The "bags" suffix is just a common pattern but not required.
            logger.info(f"🎒 BAGS token created: {name} ({symbol}) - {mint}")
            
            return TokenInfo(
                name=name,
                symbol=symbol,
                uri=uri,
                mint=mint,
                platform=Platform.BAGS,
                pool_state=pool,
                base_vault=base_vault,
                quote_vault=quote_vault,
                user=creator,
                creator=creator,
                token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,  # BAGS uses Token-2022
                creation_timestamp=monotonic(),
            )
            
        except Exception as e:
            logger.debug(f"Failed to parse initialize event: {e}")
            return None

    def parse_token_creation_from_instruction(
        self, instruction_data: bytes, accounts: list[int], account_keys: list[bytes]
    ) -> TokenInfo | None:
        """Parse token creation from BAGS instruction data.

        Args:
            instruction_data: Raw instruction data
            accounts: List of account indices
            account_keys: List of account public keys

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        # Check if instruction starts with initialize discriminator
        if len(instruction_data) < 8:
            return None
            
        discriminator = struct.unpack("<Q", instruction_data[:8])[0]
        if discriminator != self._initialize_discriminator_int:
            return None

        try:
            # Helper to get account key
            def get_account_key(index: int) -> Pubkey | None:
                if index >= len(accounts):
                    return None
                account_index = accounts[index]
                if account_index >= len(account_keys):
                    return None
                return Pubkey.from_bytes(account_keys[account_index])

            # Parse InitializeVirtualPoolParams from instruction data
            # Skip 8-byte discriminator
            offset = 8
            
            # Read name
            if offset + 4 > len(instruction_data):
                return None
            name_len = struct.unpack_from("<I", instruction_data, offset)[0]
            offset += 4
            if offset + name_len > len(instruction_data):
                return None
            name = instruction_data[offset:offset + name_len].decode("utf-8", errors="ignore")
            offset += name_len
            
            # Read symbol
            if offset + 4 > len(instruction_data):
                return None
            symbol_len = struct.unpack_from("<I", instruction_data, offset)[0]
            offset += 4
            if offset + symbol_len > len(instruction_data):
                return None
            symbol = instruction_data[offset:offset + symbol_len].decode("utf-8", errors="ignore")
            offset += symbol_len
            
            # Read uri
            if offset + 4 > len(instruction_data):
                return None
            uri_len = struct.unpack_from("<I", instruction_data, offset)[0]
            offset += 4
            if offset + uri_len > len(instruction_data):
                return None
            uri = instruction_data[offset:offset + uri_len].decode("utf-8", errors="ignore")

            # Extract accounts based on IDL order:
            # 0: creator, 1: pool, 2: config, 3: baseMint, 4: quoteMint,
            # 5: baseVault, 6: quoteVault, 7: mintMetadata, 8: payer, ...
            creator = get_account_key(0)
            pool = get_account_key(1)
            config = get_account_key(2)
            base_mint = get_account_key(3)
            quote_mint = get_account_key(4)
            base_vault = get_account_key(5)
            quote_vault = get_account_key(6)

            if not all([creator, pool, base_mint]):
                return None

            # Note: bags.fm tokens are identified by Meteora DBC program, not mint suffix
            return TokenInfo(
                name=name,
                symbol=symbol,
                uri=uri,
                mint=base_mint,
                platform=Platform.BAGS,
                pool_state=pool,
                base_vault=base_vault,
                quote_vault=quote_vault,
                global_config=config,  # Store DBC config for PDA derivation
                user=creator,
                creator=creator,
                token_program_id=SystemAddresses.TOKEN_2022_PROGRAM,
                creation_timestamp=monotonic(),
            )

        except Exception as e:
            logger.debug(f"Failed to parse initialize instruction: {e}")
            return None

    def parse_token_creation_from_geyser(
        self, transaction_info: Any
    ) -> TokenInfo | None:
        """Parse token creation from Geyser transaction data.

        Args:
            transaction_info: Geyser transaction information

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            if not hasattr(transaction_info, "transaction"):
                return None

            tx = transaction_info.transaction.transaction.transaction
            msg = getattr(tx, "message", None)
            if msg is None:
                return None

            for ix in msg.instructions:
                # Skip non-BAGS program instructions
                program_idx = ix.program_id_index
                if program_idx >= len(msg.account_keys):
                    continue

                program_id = msg.account_keys[program_idx]
                if bytes(program_id) != bytes(self.get_program_id()):
                    continue

                # Process instruction data
                token_info = self.parse_token_creation_from_instruction(
                    ix.data, ix.accounts, msg.account_keys
                )
                if token_info:
                    return token_info

            return None

        except Exception as e:
            logger.debug(f"Failed to parse geyser transaction: {e}")
            return None

    def get_program_id(self) -> Pubkey:
        """Get the BAGS program ID this parser monitors.

        Returns:
            Meteora DBC program ID
        """
        return BagsAddresses.PROGRAM

    def get_instruction_discriminators(self) -> list[bytes]:
        """Get instruction discriminators for token creation.

        Returns:
            List of discriminator bytes to match
        """
        return [self._initialize_discriminator]


    def parse_token_creation_from_block(self, block_data: dict) -> TokenInfo | None:
        """Parse token creation from block data (for block listener).

        Args:
            block_data: Block data from WebSocket

        Returns:
            TokenInfo if token creation found, None otherwise
        """
        try:
            if "transactions" not in block_data:
                return None

            for tx in block_data["transactions"]:
                if not isinstance(tx, dict) or "transaction" not in tx:
                    continue

                # Decode base64 transaction data if needed
                tx_data = tx["transaction"]
                if isinstance(tx_data, list) and len(tx_data) > 0:
                    try:
                        tx_data_encoded = tx_data[0]
                        tx_data_decoded = base64.b64decode(tx_data_encoded)
                        transaction = VersionedTransaction.from_bytes(tx_data_decoded)

                        for ix in transaction.message.instructions:
                            program_id = transaction.message.account_keys[
                                ix.program_id_index
                            ]

                            # Check if instruction is from BAGS program
                            if str(program_id) != str(self.get_program_id()):
                                continue

                            ix_data = bytes(ix.data)

                            # Check for initialize discriminator
                            if len(ix_data) >= 8:
                                discriminator = struct.unpack("<Q", ix_data[:8])[0]

                                if discriminator == self._initialize_discriminator_int:
                                    # Token creation should have substantial data
                                    if len(ix_data) <= 8 or len(ix.accounts) < 10:
                                        continue

                                    account_keys_bytes = [
                                        bytes(key)
                                        for key in transaction.message.account_keys
                                    ]

                                    token_info = (
                                        self.parse_token_creation_from_instruction(
                                            ix_data, ix.accounts, account_keys_bytes
                                        )
                                    )
                                    if token_info:
                                        return token_info

                    except Exception as e:
                        logger.debug(f"Failed to parse block transaction: {e}")
                        continue

                # Handle already decoded transaction data
                elif isinstance(tx_data, dict) and "message" in tx_data:
                    try:
                        message = tx_data["message"]
                        if (
                            "instructions" not in message
                            or "accountKeys" not in message
                        ):
                            continue

                        for ix in message["instructions"]:
                            if (
                                "programIdIndex" not in ix
                                or "accounts" not in ix
                                or "data" not in ix
                            ):
                                continue

                            program_idx = ix["programIdIndex"]
                            if program_idx >= len(message["accountKeys"]):
                                continue

                            program_id_str = message["accountKeys"][program_idx]
                            if program_id_str != str(self.get_program_id()):
                                continue

                            # Decode instruction data
                            ix_data = base64.b64decode(ix["data"])

                            if len(ix_data) >= 8:
                                discriminator = struct.unpack("<Q", ix_data[:8])[0]

                                if discriminator == self._initialize_discriminator_int:
                                    if len(ix_data) <= 8 or len(ix["accounts"]) < 10:
                                        continue

                                    account_keys_bytes = [
                                        Pubkey.from_string(key).to_bytes()
                                        for key in message["accountKeys"]
                                    ]

                                    token_info = (
                                        self.parse_token_creation_from_instruction(
                                            ix_data, ix["accounts"], account_keys_bytes
                                        )
                                    )
                                    if token_info:
                                        return token_info

                    except Exception as e:
                        logger.debug(f"Failed to parse decoded block transaction: {e}")
                        continue

            return None

        except Exception as e:
            logger.debug(f"Failed to parse block data: {e}")
            return None
