# Requirements Document

## Introduction

This feature extends the whale copy trading functionality to support all trading platforms and DEXes. Currently, whale tracking detects buys from bonding curve platforms (pump.fun and letsbonk) but fails when tokens have migrated to DEXes (PumpSwap, Raydium, Orca). This enhancement enables:

1. **Bonding Curve Trading**: Copy whale trades on pump.fun and letsbonk bonding curves (existing)
2. **DEX Trading**: Copy whale trades on migrated tokens via PumpSwap, Raydium, Orca (new)
3. **Platform Detection**: Automatically detect which DEX/platform the whale used
4. **Fallback Logic**: Try alternative platforms if primary fails

## Glossary

- **Whale_Tracker**: Component that monitors blockchain logs for whale wallet transactions across all supported platforms
- **WhaleBuy**: Data structure containing information about a whale's token purchase including platform, mint, amount, and wallet
- **Universal_Trader**: Trading coordinator that handles token purchases and sales for a specific platform
- **Platform**: Enum representing supported trading platforms (PUMP_FUN, LETS_BONK)
- **TokenInfo**: Data structure containing all information needed to execute a trade on a specific platform
- **Address_Provider**: Platform-specific component that derives PDAs and account addresses
- **Pool_State**: On-chain account containing trading pool/curve state data
- **Bonding_Curve**: pump.fun's price curve mechanism (PDA derived from mint)
- **Pool_Address**: letsbonk's trading pool address (PDA derived from base_mint and quote_mint)

## Requirements

### Requirement 1: Platform Detection in Whale Tracker

**User Story:** As a trading bot operator, I want the whale tracker to detect which platform a whale buy occurred on, so that the correct platform-specific logic can be applied.

#### Acceptance Criteria

1. WHEN a log notification is received, THE Whale_Tracker SHALL determine the platform by matching the program ID in the log against known platform program IDs
2. WHEN the program ID matches PUMP_FUN_PROGRAM, THE Whale_Tracker SHALL set the platform to "pump_fun"
3. WHEN the program ID matches LETS_BONK_PROGRAM, THE Whale_Tracker SHALL set the platform to "lets_bonk"
4. WHEN a WhaleBuy is emitted, THE Whale_Tracker SHALL include the detected platform in the WhaleBuy dataclass

### Requirement 2: Platform-Aware Whale Buy Handler

**User Story:** As a trading bot operator, I want each bot to only copy whale trades from its own platform, so that pump.fun bots copy pump.fun trades and letsbonk bots copy letsbonk trades.

#### Acceptance Criteria

1. WHEN a whale buy is received, THE Universal_Trader SHALL compare the whale buy platform with the bot's configured platform
2. WHEN the whale buy platform matches the bot's platform, THE Universal_Trader SHALL proceed with the copy trade
3. WHEN the whale buy platform does not match the bot's platform, THE Universal_Trader SHALL skip the trade and log the mismatch
4. THE Universal_Trader SHALL remove the hardcoded pump.fun-only check that currently blocks all non-pump.fun bots

### Requirement 3: LetsBonk TokenInfo Creation

**User Story:** As a trading bot operator, I want the whale copy handler to create correct TokenInfo for letsbonk tokens, so that letsbonk whale trades can be executed successfully.

#### Acceptance Criteria

1. WHEN processing a letsbonk whale buy, THE Universal_Trader SHALL use LetsBonkAddressProvider to derive the pool_address from the mint
2. WHEN processing a letsbonk whale buy, THE Universal_Trader SHALL use LetsBonkAddressProvider to derive base_vault and quote_vault
3. WHEN processing a letsbonk whale buy, THE Universal_Trader SHALL fetch pool_state from the letsbonk curve_manager
4. WHEN processing a letsbonk whale buy, THE Universal_Trader SHALL create TokenInfo with Platform.LETS_BONK and letsbonk-specific fields (pool_state, base_vault, quote_vault, global_config, platform_config)
5. IF the letsbonk pool_state indicates migration is complete, THEN THE Universal_Trader SHALL skip the trade and log the migration status

### Requirement 4: PumpFun TokenInfo Creation (Existing)

**User Story:** As a trading bot operator, I want the existing pump.fun whale copy logic to continue working correctly, so that pump.fun whale trades are not disrupted.

#### Acceptance Criteria

1. WHEN processing a pump.fun whale buy, THE Universal_Trader SHALL use PumpFunAddresses to derive the bonding_curve from the mint
2. WHEN processing a pump.fun whale buy, THE Universal_Trader SHALL derive associated_bonding_curve and creator_vault using pump.fun PDAs
3. WHEN processing a pump.fun whale buy, THE Universal_Trader SHALL create TokenInfo with Platform.PUMP_FUN and pump.fun-specific fields (bonding_curve, associated_bonding_curve, creator_vault)
4. IF the pump.fun pool_state indicates migration is complete, THEN THE Universal_Trader SHALL skip the trade and log the migration status

### Requirement 5: Bot Configuration Update

**User Story:** As a trading bot operator, I want to enable whale copy trading on my letsbonk bot, so that I can copy whale trades on the letsbonk platform.

#### Acceptance Criteria

1. WHEN letsbonk whale copy support is implemented, THE bot-sniper-0-bonkfun.yaml configuration SHALL have whale_copy.enabled set to true
2. THE configuration SHALL maintain the same whale_copy parameters (wallets_file, min_buy_amount) as the pump.fun bot

### Requirement 6: Log Handler Bug Fix

**User Story:** As a developer, I want the whale tracker to correctly call the log handler method, so that whale transactions are properly detected.

#### Acceptance Criteria

1. THE Whale_Tracker SHALL have a _handle_log method that routes logs to the appropriate platform-specific handler
2. WHEN a log is received, THE _handle_log method SHALL be called (fixing the current bug where _handle_pump_log is defined but _handle_log is called)


### Requirement 7: DEX Platform Detection from Whale Transaction

**User Story:** As a trading bot operator, I want the whale tracker to detect which DEX a whale used for their trade, so that I can copy the trade on the same DEX.

#### Acceptance Criteria

1. WHEN processing a whale transaction, THE system SHALL parse the transaction instructions to identify the DEX program ID
2. THE system SHALL detect the following DEX programs:
   - PumpSwap (pump.fun AMM for migrated tokens)
   - Raydium AMM
   - Orca Whirlpool
3. WHEN a DEX is detected, THE WhaleBuy SHALL include the `dex_platform` field with the detected DEX name
4. IF no DEX is detected but a bonding curve program is found, THE system SHALL set `dex_platform` to None (bonding curve trade)

### Requirement 8: Multi-DEX Buy Execution

**User Story:** As a trading bot operator, I want the bot to execute buys on any supported DEX, so that I can copy whale trades regardless of where they occurred.

#### Acceptance Criteria

1. THE system SHALL support buying tokens on:
   - pump.fun bonding curve (existing)
   - letsbonk bonding curve (existing)
   - PumpSwap AMM (new)
   - Raydium AMM (new)
   - Orca Whirlpool (new)
2. WHEN a whale buy is detected on a specific DEX, THE system SHALL attempt to buy on that same DEX first
3. IF the primary DEX buy fails, THE system SHALL try fallback DEXes in order of liquidity
4. THE system SHALL log all buy attempts with platform/DEX name and result

### Requirement 9: PumpSwap Buy Implementation

**User Story:** As a trading bot operator, I want to buy migrated pump.fun tokens on PumpSwap, so that I can copy whale trades on graduated tokens.

#### Acceptance Criteria

1. THE system SHALL find the PumpSwap pool for a given token mint
2. THE system SHALL fetch pool reserves to calculate swap price
3. THE system SHALL build and send a PumpSwap swap transaction with slippage protection
4. THE system SHALL handle PumpSwap-specific errors (pool not found, insufficient liquidity)

### Requirement 10: Raydium Buy Implementation

**User Story:** As a trading bot operator, I want to buy tokens on Raydium AMM, so that I can copy whale trades on Raydium pools.

#### Acceptance Criteria

1. THE system SHALL find the Raydium pool for a given token mint
2. THE system SHALL fetch pool state to calculate swap price
3. THE system SHALL build and send a Raydium swap transaction with slippage protection
4. THE system SHALL handle Raydium-specific errors (pool not found, insufficient liquidity)

### Requirement 11: Fallback Platform Logic

**User Story:** As a trading bot operator, I want the bot to try alternative platforms if the primary fails, so that I maximize trade success rate.

#### Acceptance Criteria

1. WHEN a buy on the primary platform fails, THE system SHALL try fallback platforms in this order:
   - For pump_fun bonding curve: [pumpswap, raydium]
   - For pumpswap: [raydium, orca]
   - For raydium: [orca, pumpswap]
2. THE system SHALL log each fallback attempt with reason for primary failure
3. IF all platforms fail, THE system SHALL log error with all attempted platforms

### Requirement 12: Position Tracking with Platform

**User Story:** As a trading bot operator, I want positions to track which platform they were bought on, so that I can sell on the correct platform.

#### Acceptance Criteria

1. WHEN a position is created, THE system SHALL store the `buy_platform` field (bonding curve or DEX name)
2. WHEN selling a position, THE system SHALL use the same platform where it was bought
3. IF the original platform is unavailable for sell, THE system SHALL try fallback platforms
