# Чеклист развёртывания pumpfun-bonkfun-bot v1.1.0

## Подготовка
- [ ] Бэкап текущей версии выполнен
- [ ] Redis установлен и запущен
- [ ] Python 3.11+ установлен
- [ ] Зависимости установлены (./commands/install-deps.sh)

## Волна 1: Базовая инфраструктура
- [ ] TraceContext (src/analytics/trace_context.py) создан
- [ ] TraceRecorder (src/analytics/trace_recorder.py) создан
- [ ] WatchdogMixin (src/monitoring/watchdog_mixin.py) создан
- [ ] FileGuard (src/security/file_guard.py) создан
- [ ] SecretsManager (src/security/secrets_manager.py) создан
- [ ] Unit-тесты пройдены

## Волна 2: Метрики и отправка
- [ ] MetricsServer (src/analytics/metrics_server.py) создан
- [ ] Sender Protocol (src/core/sender.py) создан
- [ ] SenderRegistry (src/core/sender_registry.py) создан
- [ ] Endpoint /metrics доступен

## Волна 3: State Machine
- [ ] PositionState (src/trading/position_state.py) создан
- [ ] Миграция positions.json проверена
- [ ] Дедупликация настроена

## Интеграция
- [ ] trace_id добавлен в логгер
- [ ] WatchdogMixin интегрирован в listeners
- [ ] Метрики интегрированы в trading

## Запуск
- [ ] Smoke test пройден
- [ ] Systemd services созданы
- [ ] Бот запущен и работает
- [ ] Метрики собираются
- [ ] Логи пишутся в logs/traces/

## Документация
- [ ] docs/observability.md создан
- [ ] docs/security.md создан  
- [ ] CHANGELOG.md обновлён
- [ ] README.md обновлён

Дата развёртывания: ____________
Версия: v1.1.0
Ответственный: ____________
