AI Agent Guidelines

## Safety Rules
- Never commit .env or private keys
- Test with minimal amounts first
- Use learning-examples/ for testing
- Check logs/ after each run

## Before Any Change
1. ruff check --fix
2. ruff format
3. Test on single bot first

## Bot Commands
- Run: python src/bot_runner.py bots/X.yaml
- Sell all: python src/sell_all.py
- Check positions: cat data/positions.json

## Platform Matching (Critical!)
- pump_fun -> listener_type: pumpportal
- lets_bonk -> listener_type: bonk_logs
- bags -> listener_type: bags_logs

## Key Directories
- src/ - Source code
- bots/ - YAML configs
- logs/ - Rotating logs (7 days)
- data/ - positions.json, purchases
- docs/sessions/ - Session documentation

## Logging (use these)
- from utils.logger import get_logger
- log_trade_event() for trades
- log_critical_error() for errors

## Recovery Steps
1. cat data/positions.json
2. tail -100 logs/bot-*.log
3. python src/sell_all.py if needed
4. Restart bot

## Sessions Completed (2026-01-20)
01-Security, 02-RaceConditions, 03-RPC
04-Validation, 05-PriorityFee, 06-Blockhash
07-BonkListener, 08-BagsListener, 09-AtomicWrites
10-Jito, 11-WhaleTracker, 12-Logging
