# Документ требований

## Введение

Добавление поддержки платформы BAGS как третьей торговой платформы в существующий Solana торговый бот. BAGS — это платформа для создания и торговли токенами на Solana, аналогичная pump.fun и letsbonk. Токены BAGS характеризуются тем, что их адреса заканчиваются на "bags". Интеграция должна следовать существующей архитектуре платформ и обеспечивать полную функциональность: мониторинг токенов, торговлю и отслеживание китов.

## Глоссарий

- **BAGS**: Платформа для создания и торговли токенами на Solana с Program ID: HWPsB1A5biibMngZB8XXb7FnFT4ohm1DMY6y1JdLBAGS
- **Platform**: Перечисление поддерживаемых торговых платформ в системе
- **AddressProvider**: Интерфейс для управления адресами и PDA-деривацией для конкретной платформы
- **InstructionBuilder**: Интерфейс для построения торговых инструкций (buy/sell)
- **CurveManager**: Интерфейс для расчёта цен и управления состоянием пула
- **EventParser**: Интерфейс для парсинга событий создания токенов
- **PumpPortalProcessor**: Компонент для обработки данных токенов от PumpPortal WebSocket
- **WhaleTracker**: Компонент для отслеживания покупок китов в реальном времени
- **TokenInfo**: Структура данных с информацией о токене, включая платформу и специфичные поля
- **IDL**: Interface Definition Language — описание структуры программы Solana

## Требования

### Требование 1: Расширение перечисления Platform

**User Story:** Как разработчик, я хочу иметь BAGS в перечислении Platform, чтобы система могла идентифицировать и маршрутизировать операции для токенов BAGS.

#### Критерии приёмки

1. THE Platform enum SHALL include a BAGS value with string representation "bags"
2. WHEN a token is identified as BAGS platform THEN the system SHALL use the BAGS value for all platform-specific routing
3. THE TokenInfo dataclass SHALL support BAGS-specific fields for pool addresses and vault addresses

### Требование 2: Реализация AddressProvider для BAGS

**User Story:** Как торговый бот, я хочу получать корректные адреса для BAGS платформы, чтобы выполнять торговые операции.

#### Критерии приёмки

1. THE BagsAddressProvider SHALL implement the AddressProvider interface
2. THE BagsAddressProvider SHALL return program_id as Pubkey("HWPsB1A5biibMngZB8XXb7FnFT4ohm1DMY6y1JdLBAGS")
3. WHEN derive_pool_address is called with a base_mint THEN the BagsAddressProvider SHALL return the correct pool PDA
4. WHEN derive_user_token_account is called THEN the BagsAddressProvider SHALL return the correct associated token account
5. WHEN get_additional_accounts is called with TokenInfo THEN the BagsAddressProvider SHALL return all platform-specific accounts needed for trading
6. THE BagsAddressProvider SHALL provide methods for deriving vault addresses (base_vault, quote_vault)

### Требование 3: Реализация InstructionBuilder для BAGS

**User Story:** Как торговый бот, я хочу строить корректные инструкции для покупки и продажи токенов BAGS.

#### Критерии приёмки

1. THE BagsInstructionBuilder SHALL implement the InstructionBuilder interface
2. WHEN build_buy_instruction is called THEN the BagsInstructionBuilder SHALL return valid Solana instructions for buying BAGS tokens
3. WHEN build_sell_instruction is called THEN the BagsInstructionBuilder SHALL return valid Solana instructions for selling BAGS tokens
4. THE BagsInstructionBuilder SHALL use IDL-based instruction encoding if IDL is available
5. WHEN get_required_accounts_for_buy is called THEN the BagsInstructionBuilder SHALL return all accounts needed for priority fee calculation
6. WHEN get_required_accounts_for_sell is called THEN the BagsInstructionBuilder SHALL return all accounts needed for priority fee calculation
7. THE BagsInstructionBuilder SHALL provide appropriate compute unit limits for buy and sell operations

### Требование 4: Реализация CurveManager для BAGS

**User Story:** Как торговый бот, я хочу получать актуальные цены и рассчитывать ожидаемые суммы для торговли токенами BAGS.

#### Критерии приёмки

1. THE BagsCurveManager SHALL implement the CurveManager interface
2. WHEN get_pool_state is called THEN the BagsCurveManager SHALL return current pool reserves and state
3. WHEN calculate_price is called THEN the BagsCurveManager SHALL return current token price in SOL
4. WHEN calculate_buy_amount_out is called with amount_in THEN the BagsCurveManager SHALL return expected tokens to receive
5. WHEN calculate_sell_amount_out is called with amount_in THEN the BagsCurveManager SHALL return expected SOL to receive
6. WHEN get_reserves is called THEN the BagsCurveManager SHALL return tuple of (base_reserves, quote_reserves)

### Требование 5: Реализация EventParser для BAGS

**User Story:** Как система мониторинга, я хочу парсить события создания токенов BAGS из различных источников данных.

#### Критерии приёмки

1. THE BagsEventParser SHALL implement the EventParser interface
2. WHEN parse_token_creation_from_logs is called with transaction logs THEN the BagsEventParser SHALL return TokenInfo if BAGS token creation is detected
3. WHEN parse_token_creation_from_instruction is called THEN the BagsEventParser SHALL parse instruction data and return TokenInfo
4. WHEN parse_token_creation_from_geyser is called THEN the BagsEventParser SHALL parse Geyser transaction data and return TokenInfo
5. WHEN parse_token_creation_from_block is called THEN the BagsEventParser SHALL parse block data and return TokenInfo
6. THE BagsEventParser SHALL return correct program_id via get_program_id method
7. THE BagsEventParser SHALL return instruction discriminators via get_instruction_discriminators method

### Требование 6: Реализация PumpPortalProcessor для BAGS

**User Story:** Как система мониторинга, я хочу получать данные о новых токенах BAGS через PumpPortal WebSocket.

#### Критерии приёмки

1. THE BagsPumpPortalProcessor SHALL implement the same interface as existing PumpPortal processors
2. THE BagsPumpPortalProcessor SHALL define supported_pool_names for BAGS platform
3. WHEN can_process is called with token data THEN the BagsPumpPortalProcessor SHALL return True if data is from BAGS platform
4. WHEN process_token_data is called THEN the BagsPumpPortalProcessor SHALL return TokenInfo with platform=Platform.BAGS
5. THE BagsPumpPortalProcessor SHALL correctly map PumpPortal fields to TokenInfo fields

### Требование 7: Регистрация платформы в PlatformFactory

**User Story:** Как система, я хочу автоматически получать реализации для BAGS платформы через фабрику.

#### Критерии приёмки

1. WHEN PlatformFactory is initialized THEN it SHALL register BAGS platform implementations
2. WHEN get_platform_implementations is called with Platform.BAGS THEN the factory SHALL return BagsAddressProvider, BagsInstructionBuilder, BagsCurveManager, and BagsEventParser
3. IF BAGS platform registration fails THEN the system SHALL log a warning and continue without BAGS support

### Требование 8: Интеграция с WhaleTracker

**User Story:** Как трейдер, я хочу отслеживать покупки китов на платформе BAGS для копирования их сделок.

#### Критерии приёмки

1. THE WhaleTracker SHALL include BAGS program ID in the list of monitored programs
2. WHEN a whale buy is detected on BAGS platform THEN the WhaleTracker SHALL emit WhaleBuy event with platform="bags"
3. THE PROGRAM_TO_PLATFORM mapping SHALL include BAGS program ID mapped to "bags"
4. WHEN target_platform is set to "bags" THEN the WhaleTracker SHALL filter only BAGS transactions

### Требование 9: Интеграция с UniversalPumpPortalListener

**User Story:** Как система мониторинга, я хочу получать уведомления о новых токенах BAGS через PumpPortal.

#### Критерии приёмки

1. THE UniversalPumpPortalListener SHALL include BagsPumpPortalProcessor in the list of processors
2. WHEN platforms parameter includes Platform.BAGS THEN the listener SHALL process BAGS tokens
3. WHEN a BAGS token is detected THEN the listener SHALL invoke token_callback with correct TokenInfo

### Требование 10: Интеграция с PlatformAwareTrader

**User Story:** Как торговый бот, я хочу покупать и продавать токены BAGS используя универсальный трейдер.

#### Критерии приёмки

1. WHEN PlatformAwareBuyer.execute is called with BAGS TokenInfo THEN it SHALL use BAGS platform implementations
2. WHEN PlatformAwareSeller.execute is called with BAGS TokenInfo THEN it SHALL use BAGS platform implementations
3. THE _get_pool_address method SHALL handle Platform.BAGS case
4. THE _get_sol_destination method SHALL handle Platform.BAGS case for correct SOL transfer tracking

### Требование 11: Поддержка IDL для BAGS

**User Story:** Как разработчик, я хочу использовать IDL для корректного парсинга и построения инструкций BAGS.

#### Критерии приёмки

1. IF BAGS IDL file exists THEN the system SHALL load and use it for instruction encoding/decoding
2. THE IDLManager SHALL support loading BAGS IDL from idl/bags.json
3. WHEN has_idl_support is called with Platform.BAGS THEN it SHALL return True if IDL is available
4. IF BAGS IDL is not available THEN the system SHALL use manual instruction building as fallback

### Требование 12: Конфигурация бота для BAGS

**User Story:** Как оператор бота, я хочу настраивать бота для работы с платформой BAGS через YAML конфигурацию.

#### Критерии приёмки

1. THE bot configuration SHALL accept platform: "bags" as valid value
2. WHEN platform is set to "bags" THEN the bot SHALL use BAGS-specific implementations
3. THE configuration validation SHALL accept "bags" as valid platform value

### Требование 13: WSS и RPC интеграция для BAGS

**User Story:** Как система мониторинга, я хочу получать данные о токенах BAGS в реальном времени через WSS и RPC.

#### Критерии приёмки

1. THE system SHALL support WSS connection for real-time BAGS token data streaming
2. THE system SHALL support RPC integration for fetching BAGS token and blockchain information
3. WHEN identifying BAGS tokens THEN the system SHALL check if token address ends with "bags"
4. THE BAGS listeners SHALL use the same WSS/RPC patterns as pump.fun and letsbonk listeners
5. THE system SHALL support logsSubscribe for BAGS program ID via WebSocket

### Требование 14: Анализ паттернов и торговые сигналы для BAGS

**User Story:** Как трейдер, я хочу получать торговые сигналы на основе анализа паттернов для токенов BAGS.

#### Критерии приёмки

1. THE system SHALL support pattern-based buy signal analysis for BAGS tokens
2. THE system SHALL support whale activity following for BAGS platform
3. WHEN a whale buys a BAGS token THEN the system SHALL generate a copy-trade signal
4. THE pattern analysis SHALL use the same methodology as for other platforms

### Требование 15: Стратегии продажи для BAGS

**User Story:** Как трейдер, я хочу иметь полный набор стратегий продажи для токенов BAGS, включая управление рисками.

#### Критерии приёмки

1. THE system SHALL support partial sell at profit targets for BAGS tokens
2. THE system SHALL support stop-loss logic for BAGS positions
3. THE system SHALL support trailing stop when following whales on BAGS platform
4. THE system SHALL support automatic sell on signals for BAGS tokens
5. THE system SHALL support position portfolio management for BAGS tokens
6. WHEN exit_strategy is "time_based" THEN the system SHALL sell BAGS tokens after configured hold time
7. WHEN exit_strategy is "tp_sl" THEN the system SHALL sell BAGS tokens at take-profit or stop-loss levels
8. THE FallbackSeller SHALL support BAGS tokens for migrated token selling

### Требование 16: Идентификация токенов BAGS

**User Story:** Как система, я хочу корректно идентифицировать токены BAGS по их характеристикам.

#### Критерии приёмки

1. WHEN a token mint address ends with "bags" THEN the system SHALL identify it as BAGS platform token
2. THE platform detection logic SHALL check BAGS program ID in transaction logs
3. THE system SHALL correctly route BAGS tokens to BAGS-specific implementations


### Требование 17: Мониторинг миграций BAGS токенов

**User Story:** Как трейдер, я хочу отслеживать миграции BAGS токенов между платформами, чтобы продолжать торговлю после миграции.

#### Критерии приёмки

1. THE system SHALL monitor BAGS token migrations from bonding curve to DEX/AMM
2. WHEN a BAGS token migrates THEN the system SHALL track old address to new address mapping
3. THE system SHALL update liquidity, volume and holder data after migration
4. WHEN token address changes after migration THEN the system SHALL resubscribe to events with new address
5. THE system SHALL log migration events with old and new addresses

### Требование 18: Поддержка DEX/AMM для торговли BAGS

**User Story:** Как трейдер, я хочу торговать BAGS токенами на DEX/AMM после миграции с bonding curve.

#### Критерии приёмки

1. THE system SHALL support trading BAGS tokens on DEX/AMM platforms after migration
2. WHEN BAGS token is migrated THEN the system SHALL use DEX/AMM for buy/sell operations
3. THE system SHALL select platform with best liquidity and lowest fees for trading
4. THE FallbackSeller SHALL support selling migrated BAGS tokens via Jupiter or PumpSwap
5. WHEN bonding curve is unavailable THEN the system SHALL automatically fallback to DEX/AMM

### Требование 19: Раннее обнаружение BAGS токенов

**User Story:** Как трейдер, я хочу обнаруживать новые BAGS токены как можно раньше для раннего входа.

#### Критерии приёмки

1. THE system SHALL monitor for new BAGS token creation events in real-time
2. THE system SHALL use WSS subscription for earliest possible detection
3. THE system SHALL use RPC transaction monitoring as backup detection method
4. WHEN new BAGS token is detected THEN the system SHALL log creation timestamp
5. THE system SHALL identify BAGS tokens by address ending with "bags" pattern

### Требование 20: Оптимизация входа в позицию

**User Story:** Как трейдер, я хочу входить в позицию на платформе с лучшими условиями.

#### Критерии приёмки

1. WHEN buying BAGS token THEN the system SHALL evaluate available platforms
2. THE system SHALL select platform with best liquidity for entry
3. THE system SHALL consider fees when selecting entry platform
4. IF BAGS token trades on multiple platforms THEN the system SHALL choose optimal one
