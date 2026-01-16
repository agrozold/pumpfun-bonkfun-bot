# Implementation Plan: Multi-Platform Whale Copy Trading

## Overview

This implementation plan converts the design into discrete coding tasks. Each task builds on previous tasks and ends with integrated, working code. Supports pump.fun, letsbonk, and BAGS platforms.

## Tasks

- [x] 1. WhaleTracker multi-platform support (ЗАВЕРШЕНО)
  - [x] 1.1 Add `_handle_log` method that routes to platform-specific handlers
    - Created `_handle_log(self, data: dict)` method
    - Added `_detect_platform_from_logs(self, logs: list[str])` helper
    - Routes based on detected platform (pump_fun, lets_bonk, bags)
  
  - [x] 1.2 Platform detection from program IDs
    - PUMP_FUN_PROGRAM: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    - LETS_BONK_PROGRAM: "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
    - BAGS_PROGRAM: "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
    - PUMPSWAP_PROGRAM: "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP"
    - RAYDIUM_AMM_PROGRAM: "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
  
  - [x] 1.3 `_emit_whale_buy` accepts platform parameter
    - WhaleBuy.platform field propagates platform info

- [x] 2. UniversalTrader._on_whale_buy multi-platform support (ЗАВЕРШЕНО)
  - [x] 2.1 Universal whale copy via `_buy_any_dex` method
    - Tries platform-specific bonding curve first
    - Falls back to PumpSwap for migrated tokens
    - Falls back to Jupiter as universal aggregator
  
  - [x] 2.2 Platform-specific TokenInfo creation methods
    - `_create_pumpfun_token_info_from_mint` - pump.fun
    - `_create_letsbonk_token_info_from_mint` - letsbonk
    - `_create_bags_token_info_from_mint` - BAGS (Meteora DBC)
  
  - [x] 2.3 Helper methods
    - `_extract_creator(pool_state)` - extract creator from pool state

- [x] 3. BAGS platform integration (ЗАВЕРШЕНО)
  - [x] 3.1 BagsAddressProvider with DBC_PROGRAM and DEFAULT_CONFIG
  - [x] 3.2 BAGS added to _buy_any_dex method
  - [x] 3.3 BAGS added to whale_tracker PROGRAM_TO_PLATFORM mapping

- [x] 4. Configuration support (ЗАВЕРШЕНО)
  - [x] 4.1 Platform enum includes BAGS
  - [x] 4.2 config_loader.py supports bags platform
  - [x] 4.3 Listener factory supports BAGS

## Статус файлов

| Файл | Статус | Описание |
|------|--------|----------|
| src/monitoring/whale_tracker.py | [OK] | Multi-platform whale detection |
| src/trading/universal_trader.py | [OK] | Universal whale copy with _buy_any_dex |
| src/platforms/bags/address_provider.py | [OK] | BAGS addresses with DBC_PROGRAM |
| src/config_loader.py | [OK] | BAGS platform support |

## Тестирование (НА VPS)

- [ ] Запустить бота с whale_copy.enabled: true
- [ ] Проверить детекцию китов на всех платформах
- [ ] Тестовая покупка через whale copy
- [ ] Проверить fallback на Jupiter

## Notes

- Whale copy работает для ВСЕХ платформ: PUMP, BONK, BAGS
- Автоматический fallback: bonding curve -> PumpSwap -> Jupiter
- Каждый бот слушает только свою платформу (target_platform)
- Мигрированные токены покупаются через Jupiter
