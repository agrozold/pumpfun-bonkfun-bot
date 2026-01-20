Подробное резюме проекта pumpfun-bonkfun-bot: Оптимизация API запросов
Исходная проблема
Бот для снайпинга токенов на pump.fun/letsbonk.fun/bags.fm имел критическую проблему с количеством API запросов:

При проверке каждого создателя токена бот вызывал getSignaturesForAddress (1 запрос)
Затем для каждой сигнатуры вызывал getTransaction (50-100 запросов)
Итого: ~100 RPC запросов на одного создателя
Это быстро исчерпывало лимиты Helius (бесплатный план: 10 RPC req/s, 2 Enhanced req/s) и Chainstack
Реализованное решение
1. Новый модуль src/optimization/helius_optimizer.py
Использует Helius Enhanced Transactions API — один вызов возвращает обогащённую историю транзакций:

Copy# Endpoint: GET https://api.helius.xyz/v0/addresses/{creator_address}/transactions
# Params: api-key, limit=50

async def get_creator_stats_optimized(creator_address: str, limit: int = 50) -> Optional[dict]:
    # Возвращает: total_txs, tokens_created, tokens_sold, unique_tokens_sold, large_sells
Также реализован batch_parse_transactions() для парсинга до 100 сигнатур одним POST запросом.

2. Новый модуль src/optimization/cache_manager.py
SQLite кэш для сохранения результатов проверок между перезапусками бота:

База данных: data/creator_cache.db
TTL по умолчанию: 3600 секунд (1 час)
Таблица creator_cache: address, is_risky, risk_score, tokens_created, tokens_sold, last_checked, details
Функции:

init_db() — инициализация базы
get_cached_creator_status(address, ttl_seconds) — проверка кэша
cache_creator_status(address, is_risky, risk_score, ...) — сохранение в кэш
cleanup_expired_cache(max_age_hours) — очистка старых записей
get_cache_stats() — статистика кэша
3. Новый модуль src/optimization/creator_analyzer.py
Интеграция кэша и Helius API с расчётом риска:

CopyRISK_THRESHOLDS = {
    "max_tokens_created": 10,      # Порог созданных токенов
    "max_tokens_sold_ratio": 0.8   # Порог доли проданных токенов
}

async def is_creator_safe(creator_address, cache_ttl_seconds=3600, risk_threshold=50.0):
    # 1. Проверяет кэш (0 API запросов если найден)
    # 2. Если не в кэше — 1 запрос к Helius Enhanced API
    # 3. Рассчитывает risk_score (0-100)
    # 4. Сохраняет результат в кэш
    # Возвращает: (is_safe: bool, details: dict)
Модель риска:

tokens_created > 10 → +3 балла за каждый лишний токен (до 30)
sell_ratio > 0.8 → +30 × sell_ratio баллов
large_sells → +5 баллов за каждую крупную продажу (до 20)
Итого: риск от 0 до 100, порог по умолчанию 50
4. Обновлённый src/monitoring/dev_reputation.py
Класс DevReputationChecker полностью переписан:

Использует тот же Helius Enhanced API (1 запрос вместо ~100)
SQLite кэш вместо in-memory кэша
Совместим с существующим интерфейсом check_dev(creator_address)
5. Модуль инициализации src/optimization/__init__.py
Copyfrom .creator_analyzer import is_creator_safe, batch_check_creators
from .cache_manager import get_cached_creator_status, cache_creator_status, get_cache_stats
from .helius_optimizer import get_creator_stats_optimized

__all__ = [
    "is_creator_safe",
    "batch_check_creators", 
    "get_cached_creator_status",
    "cache_creator_status",
    "get_cache_stats",
    "get_creator_stats_optimized",
]
Конфигурация
Обновлённый .env
Copy# Helius RPC (основной)
SOLANA_NODE_RPC_ENDPOINT=https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_API_KEY
HELIUS_API_KEY=YOUR_HELIUS_API_KEY

# Chainstack WSS (для WebSocket подписок)
SOLANA_NODE_WSS_ENDPOINT=wss://solana-mainnet.core.chainstack.com/YOUR_CHAINSTACK_KEY
CHAINSTACK_RPC_ENDPOINT=https://solana-mainnet.core.chainstack.com/YOUR_CHAINSTACK_KEY
CHAINSTACK_WSS_ENDPOINT=wss://solana-mainnet.core.chainstack.com/YOUR_CHAINSTACK_KEY

# Alchemy (fallback)
ALCHEMY_RPC_ENDPOINT=https://solana-mainnet.g.alchemy.com/v2/[YOUR_KEY]

# Настройки кэша создателей
CREATOR_CACHE_TTL=3600          # TTL кэша в секундах
CREATOR_RISK_THRESHOLD=50       # Порог риска (0-100)
Важно: Helius не предоставляет WSS endpoint на бесплатном плане, поэтому используется Chainstack для WebSocket подписок.

Обновлённые YAML конфиги ботов
Все файлы в bots/*.yaml теперь используют переменные окружения вместо хардкода:

Copy# Было:
rpc_endpoint: https://mainnet.helius-rpc.com/?api-key=YOUR_KEY...
wss_endpoint: wss://api.mainnet-beta.solana.com

# Стало:
rpc_endpoint: ${SOLANA_NODE_RPC_ENDPOINT}
wss_endpoint: ${SOLANA_NODE_WSS_ENDPOINT}
Обновлённые файлы:

bots/bot-sniper-0-pump.yaml
bots/bot-sniper-0-bonkfun.yaml
bots/bot-sniper-0-bags.yaml
bots/bot-whale-copy.yaml
bots/bot-volume-sniper.yaml
bots/bags-example.yaml
Включение проверки создателей в YAML
Copydev_check:
  enabled: true              # Включить проверку
  max_tokens_created: 10     # Максимум токенов у создателя
  min_account_age_days: 3    # Минимальный возраст аккаунта
Структура файлов проекта
/opt/pumpfun-bonkfun-bot/
├── .env                              # Конфигурация (ключи API, endpoints)
├── .env.backup                       # Резервная копия
├── data/
│   └── creator_cache.db              # SQLite кэш создателей
├── src/
│   ├── optimization/                 # НОВАЯ ПАПКА
│   │   ├── __init__.py
│   │   ├── helius_optimizer.py       # Helius Enhanced API
│   │   ├── cache_manager.py          # SQLite кэш
│   │   └── creator_analyzer.py       # Интеграция + расчёт риска
│   ├── monitoring/
│   │   ├── dev_reputation.py         # ОБНОВЛЁН (с SQLite кэшем)
│   │   └── dev_reputation.py.backup  # Резервная копия оригинала
│   ├── trading/
│   │   └── universal_trader.py       # Основной трейдер (использует dev_checker)
│   └── ...
├── bots/
│   ├── bot-sniper-0-pump.yaml        # Конфиг pump.fun бота
│   ├── bot-sniper-0-bonkfun.yaml     # Конфиг letsbonk.fun бота
│   ├── bot-sniper-0-bags.yaml        # Конфиг bags.fm бота
│   └── ...
└── ...
Результаты тестирования
Тест 1: API запрос (первый вызов)
Testing: vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg
Result: is_safe=True, risk_score=6.0, tokens_created=12, tokens_sold=9
Stats: api_calls=1, cache_hits=0
Тест 2: Кэш (повторный вызов)
Testing (same address): vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg  
Result: is_safe=True, risk_score=6.0, tokens_created=12, tokens_sold=9
Stats: api_calls=1, cache_hits=1
Source: cache
Результат: Повторная проверка того же создателя = 0 API запросов.

Экономия ресурсов
Метрика	До оптимизации	После оптимизации	Экономия
Запросов на создателя	~100	1	99%
Повторная проверка	~100	0 (кэш)	100%
Тип кэша	In-memory	SQLite	Персистентный
Сохранение между перезапусками	Нет	Да	—
Архитектура RPC провайдеров
Бот использует RPC Manager с приоритетами:

Helius (primary) — ~6 req/min для RPC, ~1.2 req/min для Enhanced API
Chainstack (co-primary) — ~7 req/min, используется для WSS
Alchemy (fallback #1) — 1.0 req/s
Public Solana (fallback #2) — 0.5 req/s
Комбинированный бюджет: ~1.8M credits/month (Helius 800k + Chainstack 1M)

Известные проблемы и TODO
Исправить: bots/bot-volume-sniper.yaml
Ошибка: Missing required config key: filters.listener_type

Rate Limiting
При старте бота Helius Enhanced API может возвращать 429 (rate limit). Система автоматически делает backoff (2 секунды) и повторяет запрос.

Рекомендации по безопасности
Приватный ключ (SOLANA_PRIVATE_KEY) был виден в логах — рекомендуется создать новый кошелёк
Добавить .env в .gitignore
Рассмотреть использование secret manager
Команды для запуска
Copy# Перейти в директорию проекта
cd /opt/pumpfun-bonkfun-bot

# Активировать виртуальное окружение
source venv/bin/activate

# Запустить бота
python3 src/bot_runner.py

# Или через установленный пакет
pump_bot
Полезные команды для диагностики
Copy# Проверить статус кэша
python3 -c "
import sys; sys.path.insert(0, 'src')
from optimization.cache_manager import get_cache_stats
print(get_cache_stats())
"

# Очистить кэш
rm -f data/creator_cache.db

# Проверить конфигурацию бота
python3 -c "
import sys; sys.path.insert(0, 'src')
from config_loader import load_bot_config
config = load_bot_config('bots/bot-sniper-0-pump.yaml')
print(f'RPC: {config[\"rpc_endpoint\"][:50]}...')
print(f'WSS: {config[\"wss_endpoint\"][:50]}...')
print(f'Dev check enabled: {config.get(\"dev_check\", {}).get(\"enabled\")}')
"

# Тест Helius API напрямую
curl "https://api.helius.xyz/v0/addresses/[ADDRESS]/transactions?api-key=[YOUR_KEY]&limit=1"
Ссылки
Репозиторий: https://github.com/agrozold/pumpfun-bonkfun-bot
Helius Enhanced API docs: https://www.helius.dev/docs/api-reference/enhanced-transactions/gettransactionsbyaddress
Chainstack Solana docs: https://docs.chainstack.com/docs/solana-creating-a-pumpfun-bot
Chainstack limits: https://docs.chainstack.com/docs/limits
