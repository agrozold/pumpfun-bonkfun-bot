# Claude Development Guide

## Project
Solana trading bot for pump.fun, bonk.fun, bags.fm

## Key Files
- src/bot_runner.py - Entry point
- src/trading/universal_trader.py - Core trading
- src/monitoring/*_listener.py - Platform listeners
- src/utils/logger.py - Unified logging
- bots/*.yaml - Bot configurations

## Commands
- ruff format - Format code
- ruff check --fix - Fix linting
- python src/bot_runner.py bots/X.yaml - Run bot

## Sessions 2026-01-20
01 - Security audit (keys in .env only)
02 - Race conditions (asyncio.Lock)
03 - RPC manager audit
04 - Platform-listener validation
05 - Dynamic priority fee
06 - Blockhash caching
07 - Bonkfun listener audit
08 - Bags listener verification
09 - Atomic file writes (safe_file_writer)
10 - Jito buy/sell integration
11 - Whale tracker debug
12 - Logging standardization (rotation)

## Platform Mapping
- pump_fun -> pumpportal
- lets_bonk -> bonk_logs
- bags -> bags_logs

## Code Style
- Python 3.11+, type hints, 88 chars
- Google docstrings, double quotes
