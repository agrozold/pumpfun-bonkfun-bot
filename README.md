# Whale Copy Trading Bot

Bot for copying whale trades on Solana (Pump.fun, PumpSwap, Raydium).

## Quick Start

git clone, pip install -r requirements.txt, configure .env, run bot.

## Commands

- bot-start/stop/restart/status
- bot-logs, bot-trades, bot-errors
- buy TOKEN SOL, sell TOKEN PCT
- buysync TOKEN SOL, wsync

## Config (bots/bot-whale-copy.yaml)

- buy_amount: 0.02
- stop_loss: 20%
- take_profit: 10000%
- tsl_activation: 20%
- tsl_trail: 50%
- moon_bag: 10%

## Important

Never commit .env or private keys!

MIT License
