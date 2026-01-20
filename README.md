# Solana Trading Bot

Multi-platform sniper bot for pump.fun, bonk.fun, bags.fm

## Features
- Snipe new tokens on 3 platforms
- Whale copy trading
- Volume-based sniping
- Take profit / Stop loss
- Jito bundles for speed
- Rotating logs with cleanup

## Platforms
- pump.fun (pumpportal WebSocket)
- bonk.fun (Raydium LaunchLab logs)
- bags.fm (Meteora DBC logs)

## Installation
git clone https://github.com/agrozold/pumpfun-bonkfun-bot.git cd pumpfun-bonkfun-bot python3 -m venv venv source venv/bin/activate pip install -e . cp .env.example .env nano .env


## Environment Variables
- SOLANA_NODE_RPC_ENDPOINT - RPC HTTP
- SOLANA_NODE_WSS_ENDPOINT - RPC WebSocket
- SOLANA_PRIVATE_KEY - Base58 key
- HELIUS_API_KEY - Optional

## Running Bots
python src/bot_runner.py bots/bot-sniper-0-pump.yaml python src/bot_runner.py bots/bot-sniper-0-bonkfun.yaml python src/bot_runner.py bots/bot-sniper-0-bags.yaml python src/bot_runner.py bots/bot-whale-copy.yaml


## Bot Configs
- bot-sniper-0-pump.yaml - Pump.fun
- bot-sniper-0-bonkfun.yaml - Bonk.fun
- bot-sniper-0-bags.yaml - Bags.fm
- bot-whale-copy.yaml - Copy whales
- bot-volume-sniper.yaml - Volume sniper

## Platform-Listener Mapping
- platform: pump_fun -> listener_type: pumpportal
- platform: lets_bonk -> listener_type: bonk_logs
- platform: bags -> listener_type: bags_logs

## Trade Settings
- buy_amount: SOL per trade (0.01)
- buy_slippage: 30% (0.30)
- sell_slippage: 35% (0.35)
- take_profit_percentage: 100% (1.0)
- stop_loss_percentage: 20% (0.20)
- moon_bag_percentage: 50%

## Project Structure
- src/bot_runner.py - Entry point
- src/trading/ - Trading logic
- src/monitoring/ - Listeners
- src/platforms/ - Platform code
- src/utils/ - Logger, utilities
- bots/ - YAML configs
- logs/ - Rotating logs
- data/ - Positions, state

## Recovery
1. Check positions: cat data/positions.json
2. Check logs: tail -100 logs/bot-*.log
3. Sell all: python src/sell_all.py
4. Restart bot

## Documentation
- CLAUDE.md - Development guide
- AGENTS.md - AI agent rules
- BOT_COMMANDS.md - All commands
- docs/sessions/ - Session logs

## Based on
 pump-fun-bot
