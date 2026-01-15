# Implementation Plan: Multi-Platform Whale Copy Trading

## Overview

This implementation plan converts the design into discrete coding tasks. Each task builds on previous tasks and ends with integrated, working code. Focus is on pump.fun and letsbonk platforms first.

## Tasks

- [ ] 1. Fix WhaleTracker log handler bug and add platform detection
  - [ ] 1.1 Add `_handle_log` method that routes to platform-specific handlers
    - Create `_handle_log(self, data: dict)` method
    - Add `_detect_platform_from_logs(self, logs: list[str])` helper
    - Route to `_handle_pump_log` or new `_handle_bonk_log` based on detected platform
    - _Requirements: 6.1, 6.2_
  
  - [ ] 1.2 Add `_handle_bonk_log` method for letsbonk logs
    - Mirror structure of `_handle_pump_log`
    - Check for letsbonk Buy instruction pattern in logs
    - Call `_check_if_whale_tx` with platform context
    - _Requirements: 1.1, 1.3_
  
  - [ ] 1.3 Update `_emit_whale_buy` to accept and propagate platform parameter
    - Add `platform: str = "pump_fun"` parameter
    - Set `WhaleBuy.platform` from parameter instead of hardcoded value
    - Update callers to pass platform
    - _Requirements: 1.4_
  
  - [ ]* 1.4 Write property test for platform detection from logs
    - **Property 1: Platform Detection from Logs**
    - **Validates: Requirements 1.1, 6.1**

- [ ] 2. Checkpoint - Verify whale tracker changes
  - Ensure whale tracker correctly detects platforms from logs
  - Run `ruff format` and `ruff check` on whale_tracker.py
  - Ask the user if questions arise

- [ ] 3. Refactor UniversalTrader._on_whale_buy for multi-platform support
  - [ ] 3.1 Replace hardcoded pump.fun check with platform matching logic
    - Remove `if self.platform != Platform.PUMP_FUN: return` check
    - Add platform comparison: `whale_platform = Platform(whale_buy.platform)`
    - Skip trade if `whale_platform != self.platform` with info log
    - _Requirements: 2.1, 2.2, 2.3, 2.4_
  
  - [ ] 3.2 Extract pump.fun TokenInfo creation into `_create_pumpfun_token_info` method
    - Move existing pump.fun TokenInfo creation logic to new method
    - Return `TokenInfo | None` (None if migrated or dev check fails)
    - Keep dev check logic inside the method
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  
  - [ ] 3.3 Add `_create_letsbonk_token_info` method for letsbonk TokenInfo creation
    - Use LetsBonkAddressProvider for address derivation
    - Derive pool_address, base_vault, quote_vault from mint
    - Fetch pool_state from letsbonk curve_manager
    - Handle migration check and dev check
    - Create TokenInfo with Platform.LETS_BONK and letsbonk-specific fields
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_
  
  - [ ] 3.4 Add helper methods for common operations
    - Add `_extract_creator(self, pool_state: dict) -> Pubkey | None`
    - Add `_check_dev_reputation(self, creator: Pubkey | None, symbol: str) -> bool`
    - Refactor existing code to use these helpers
    - _Requirements: 3.4, 4.3_
  
  - [ ]* 3.5 Write property test for platform matching behavior
    - **Property 3: Platform Matching Behavior**
    - **Validates: Requirements 2.1, 2.2, 2.3**

- [ ] 4. Checkpoint - Verify UniversalTrader changes
  - Ensure _on_whale_buy correctly routes by platform
  - Run `ruff format` and `ruff check` on universal_trader.py
  - Ask the user if questions arise

- [ ] 5. Add property tests for address derivation
  - [ ]* 5.1 Write property test for LetsBonk address derivation
    - **Property 4: LetsBonk Address Derivation**
    - **Validates: Requirements 3.1, 3.2**
  
  - [ ]* 5.2 Write property test for PumpFun address derivation
    - **Property 5: PumpFun Address Derivation**
    - **Validates: Requirements 4.1, 4.2**
  
  - [ ]* 5.3 Write property test for TokenInfo platform consistency
    - **Property 6: TokenInfo Platform Consistency**
    - **Validates: Requirements 3.4, 4.3**

- [ ] 6. Update bot configuration
  - [ ] 6.1 Enable whale_copy in bot-sniper-0-bonkfun.yaml
    - Set `whale_copy.enabled: true`
    - Ensure wallets_file and min_buy_amount match pump.fun bot
    - _Requirements: 5.1, 5.2_

- [ ] 7. Final checkpoint - Ensure all tests pass
  - Run `ruff format` and `ruff check` on all modified files
  - Verify no regressions in existing functionality
  - Ask the user if questions arise

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties
- Unit tests validate specific examples and edge cases
- Test with learning-examples before running main bot
