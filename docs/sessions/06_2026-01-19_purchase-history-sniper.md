# Session 010 - 2026-01-19

## Whale Copy Multi-Platform + Архитектура ботов

### Проблема
- 7 ботов работали, включая дубликат bags бота
- Whale copy был включен в нескольких ботах — дублирование
- Whale tracker слушал только одну платформу

### Решение

#### 1. Архитектура ботов (5 конфигов, 6 процессов):

| Бот | Платформа | Функции |
|-----|-----------|---------|
| bot-whale-copy | ВСЕ | Копирует китов из smart_money_wallets.json |
| bot-sniper-0-pump | pump_fun | Снайпер новых токенов |
| bot-sniper-0-bonk | lets_bonk | Снайпер новых токенов |
| bot-sniper-0-bags | bags | Снайпер новых токенов |
| bot-volume-sniper | pump_fun | Volume analyzer + Trending |

#### 2. Whale Copy Multi-Platform:
- whale_all_platforms: true в конфиге
- Слушает: pump_fun, lets_bonk, bags, pumpswap, raydium
- Мониторит 78 китов из smart_money_wallets.json

#### 3. Stablecoin Filter:
Не копирует сделки с: USDC, USDT, USDH, USDS, PYUSD, USD1, WSOL

#### 4. Убрали дублирование:
- Удалён bags-example.yaml
- Отключен whale_copy во всех снайперах

---

## Дополнение: Global Purchase History + Sniper Enabled

### Проблема
- Несколько ботов покупали один и тот же токен одновременно
- whale-copy и volume-sniper снайпили новые токены (не должны)

### Решение

#### 1. Global Purchase History
Файл `/data/purchased_tokens_history.json` — общий для всех ботов:
- При старте каждый бот загружает историю
- При покупке — записывает в файл
- Токен купленный один раз — НИКОГДА не покупается снова

#### 2. sniper_enabled флаг
- `sniper_enabled: true` (default) — бот снайпит новые токены
- `sniper_enabled: false` — бот НЕ снайпит, только whale/trending/volume

#### Текущая архитектура (5 конфигов, 6 процессов):

| Бот | sniper_enabled | Что делает |
|-----|----------------|------------|
| bot-sniper-0-pump | true | Снайпит pump_fun |
| bot-sniper-0-bonk | true | Снайпит lets_bonk |
| bot-sniper-0-bags | true | Снайпит bags |
| bot-whale-copy | false | Только копирует китов |
| bot-volume-sniper | false | Только trending/volume |

### Файлы:
- `src/trading/purchase_history.py` — модуль истории покупок
- `data/purchased_tokens_history.json` — файл истории
- `bots/bot-whale-copy.yaml` — sniper_enabled: false
- `bots/bot-volume-sniper.yaml` — sniper_enabled: false
