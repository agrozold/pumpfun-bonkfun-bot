# Session 011: Whale Tracker WebSocket & RPC Debugging

**Date:** 2026-01-20
**Status:** COMPLETED

## Problem

Whale tracker не получал данные от WebSocket и не обрабатывал транзакции китов.

## Root Cause Analysis

### 1. Публичный Solana RPC не поддерживает logsSubscribe
- `api.mainnet-beta.solana.com` не отдаёт данные по logsSubscribe для произвольных аккаунтов
- **Решение:** Переключение на Chainstack WSS

### 2. Отсутствие логирования входящих сообщений
- Код не логировал что приходит от WebSocket
- Невозможно было понять где проблема
- **Решение:** Добавлены debug-логи на каждом этапе

### 3. Helius Enhanced rate limit слишком низкий
- Было: 0.008 req/s = 1 запрос в 125 секунд
- Блокировало WebSocket цикл на ожидании rate limit
- **Решение:** Увеличен до 0.02 req/s = 1 запрос в 50 секунд

## Changes Made

### 1. RPC Manager (src/core/rpc_manager.py)
- rate_limit_per_second: 0.008 -> 0.02

### 2. Whale Tracker Debug Logs (src/monitoring/whale_tracker.py)
Добавлены временные debug-логи для диагностики на каждом этапе обработки.

## Data Flow (Verified Working)

1. WebSocket (Chainstack) -> logsNotification received
2. _handle_log() - parse signature, logs, err
3. _detect_platform_from_logs() - pump_fun/bags/raydium/etc
4. is_buy detection (Instruction: Buy/swap)
5. _check_if_whale_tx()
6. _check_whale_tx_with_manager()
7. Helius Enhanced API -> get transaction details
8. _process_helius_tx() -> check if feePayer in whale_wallets
9. If match -> emit whale buy signal

## Rate Limit Budget (0.02 req/s)

- Requests per day: 1,728
- Credits per day: 86,400 (50 credits x 1,728)
- Days on 500K credits: ~5.8 days at max load

## Status

- WebSocket connected to Chainstack: OK
- logsNotification received: OK
- Platform detection working: OK
- Buy/Sell detection working: OK
- Helius Enhanced API responding: OK
- feePayer check against whale_wallets: OK
- Waiting for actual whale buy to trigger signal
