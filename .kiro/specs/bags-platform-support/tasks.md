# План реализации: Поддержка платформы BAGS

## Обзор

Реализация поддержки платформы BAGS следует существующей архитектуре платформ (pump.fun, letsbonk). 
BAGS использует Meteora DBC (Dynamic Bonding Curve) программу: `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`

## Ключевые отличия от других платформ

1. **Единая swap инструкция** - Meteora DBC использует одну `swap` инструкцию для buy и sell (направление определяется source/destination аккаунтами)
2. **VirtualPool структура** - Вместо bonding curve используется VirtualPool с baseReserve/quoteReserve/sqrtPrice
3. **Миграция на DAMM v2** - После graduation токены мигрируют на `cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG`
4. **Pool PDA** - Требует config из события создания: seeds = [baseMint, quoteMint, config]

## Задачи

### Базовая инфраструктура (ЗАВЕРШЕНО)

- [x] 1. Расширение Platform enum и базовой инфраструктуры
  - [x] 1.1 Добавить BAGS в Platform enum в src/interfaces/core.py
  - [x] 1.2 Расширить TokenInfo для поддержки BAGS-специфичных полей

- [x] 2. Создать структуру директории src/platforms/bags/
  - [x] 2.1 Создать src/platforms/bags/__init__.py
  - [x] 2.2 Создать src/platforms/bags/address_provider.py (Meteora DBC адреса)

- [x] 3. Реализовать BagsInstructionBuilder
  - [x] 3.1 Создать src/platforms/bags/instruction_builder.py
    - Использует единую `swap` инструкцию Meteora DBC
    - Направление buy/sell определяется source/destination аккаунтами
    - Данные: discriminator + amountIn + minimumAmountOut

- [x] 4. Реализовать BagsCurveManager
  - [x] 4.1 Создать src/platforms/bags/curve_manager.py
    - Парсинг VirtualPool структуры (baseReserve, quoteReserve, sqrtPrice, status)
    - Методы is_pool_migrated(), get_migration_threshold()

- [x] 5. Реализовать BagsEventParser
  - [x] 5.1 Создать src/platforms/bags/event_parser.py
    - Парсинг initialize_virtual_pool_with_spl_token инструкции
    - Парсинг EvtInitializeVirtualPoolWithSplToken события

### Интеграция (ЗАВЕРШЕНО)

- [x] 6. Реализовать BagsPumpPortalProcessor
  - [x] 6.1 Создать src/platforms/bags/pumpportal_processor.py

- [x] 7. Интеграция с PlatformFactory
  - [x] 7.1 Обновить src/platforms/__init__.py

- [x] 8. Интеграция с IDLManager
  - [x] 8.1 Обновить src/utils/idl_manager.py
  - [x] 8.2 Создать idl/bags.json с реальной Meteora DBC структурой

- [x] 9. Интеграция с WhaleTracker
  - [x] 9.1 Обновить src/monitoring/whale_tracker.py

- [x] 10. Интеграция с UniversalPumpPortalListener
  - [x] 10.1 Обновить src/monitoring/universal_pumpportal_listener.py

- [x] 11. Интеграция с PlatformAwareTrader
  - [x] 11.1 Обновить src/trading/platform_aware.py

- [x] 12. Функция идентификации BAGS токенов
  - [x] 12.1 is_bags_token() в address_provider.py

- [x] 13. Поддержка стратегий продажи
  - [x] 13.1 Exit strategies (time_based, tp_sl) - platform-agnostic
  - [x] 13.2 FallbackSeller - platform-agnostic (Jupiter/PumpSwap)

- [x] 14. Конфигурация бота
  - [x] 14.1 Обновить валидацию конфигурации
  - [x] 14.2 Создать bots/bags-example.yaml

- [x] 15. WSS/RPC интеграция
  - [x] 15.1 Добавить BAGS в ListenerFactory
  - [x] 15.2 Добавить BAGS в config_loader.py

- [x] 16. Learning examples
  - [x] 16.1 Создать learning-examples/bags/fetch_bags_price.py
  - [x] 16.2 Создать learning-examples/bags/listen_bags_tokens.py

### Миграции (В ПРОЦЕССЕ)

- [x] 17. Создать BagsMigrationTracker
  - [x] 17.1 Создать src/monitoring/bags_migration_tracker.py
    - Мониторинг EvtMigrateMeteoraDammV2 событий
    - Маппинг old_pool -> new_pool
    - Callback при миграции

- [x] 18. Интеграция миграций с UniversalTrader
  - [x] 18.1 Добавить BAGS в _buy_any_dex метод
  - [x] 18.2 Добавить _create_bags_token_info_from_mint метод
  - [x] 18.3 Автоматический fallback на Jupiter при миграции

### Тестирование (НА VPS)

- [ ] 19. Тестирование на VPS
  - [ ] 19.1 Запустить learning-examples/bags/listen_bags_tokens.py
  - [ ] 19.2 Проверить парсинг реальных BAGS токенов
  - [ ] 19.3 Тестовая покупка с минимальной суммой
  - [ ] 19.4 Тестовая продажа
  - [ ] 19.5 Проверить fallback на Jupiter для мигрированных токенов

## Статус файлов

| Файл | Статус | Описание |
|------|--------|----------|
| src/platforms/bags/address_provider.py | [OK] | Meteora DBC адреса, PDA derivation |
| src/platforms/bags/instruction_builder.py | [OK] | Swap инструкция (buy/sell) |
| src/platforms/bags/curve_manager.py | [OK] | VirtualPool парсинг |
| src/platforms/bags/event_parser.py | [OK] | Token creation events |
| src/platforms/bags/pumpportal_processor.py | [OK] | PumpPortal интеграция |
| src/platforms/bags/__init__.py | [OK] | Экспорты |
| src/monitoring/bags_migration_tracker.py | [OK] | Migration monitoring |
| idl/bags.json | [OK] | Meteora DBC IDL |
| bots/bags-example.yaml | [OK] | Пример конфигурации |
| learning-examples/bags/*.py | [OK] | Примеры |

## Примечания

- BAGS токены идентифицируются по окончанию mint адреса на "bags"
- Pool PDA требует config из события создания токена
- После миграции торговля через Jupiter/DAMM v2
- Тестирование только на VPS с реальными токенами
