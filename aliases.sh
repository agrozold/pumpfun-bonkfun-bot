#!/bin/bash
# Whale Copy Trading Bot — Shell Aliases
# source /opt/pumpfun-bonkfun-bot/aliases.sh

BOT_DIR="/opt/pumpfun-bonkfun-bot"

# ─────────────────────────────────────────────────────────────
# 🛑 BLACKLIST — управление блэклистом deployer'ов
# ─────────────────────────────────────────────────────────────
# Usage: blacklist add <WALLET> [label]
#        blacklist del <WALLET|LABEL>
#        blacklist list
alias blacklist='cd $BOT_DIR && ./venv/bin/python3 scripts/blacklist_cli.py'

# ─────────────────────────────────────────────────────────────
# 🤖 BOT CONTROL
# ─────────────────────────────────────────────────────────────
alias bot-start='sudo systemctl start whale-bot && sleep 2 && echo "✅ Bot started" && systemctl is-active whale-bot'
alias bot-stop='sudo systemctl stop whale-bot && echo "⛔ Bot stopped"'
alias bot-restart='sudo systemctl stop whale-bot 2>/dev/null; pkill -f "bot_runner.py" 2>/dev/null; sleep 1; sudo systemctl start whale-bot; sleep 3; systemctl is-active whale-bot && echo "✅ Bot restarted" || echo "❌ Bot failed!"'
alias bot-status='sudo systemctl status whale-bot --no-pager | head -20; echo ""; curl -s http://localhost:8000/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(webhook offline)"'
alias bot-health='curl -s http://localhost:8000/health 2>/dev/null | python3 -m json.tool || echo "Webhook server not running"'
alias bot-mode='if grep -q "^GEYSER_API_KEY=" $BOT_DIR/.env; then echo "🟢 gRPC + Webhook"; else echo "🟡 Webhook only"; fi'
alias bot-webhook='sed -i "s/^GEYSER_API_KEY=/#GEYSER_API_KEY=/" $BOT_DIR/.env && sudo systemctl restart whale-bot && echo "🟡 Webhook-only mode"'
alias bot-ungeyser='sed -i "s/^#GEYSER_API_KEY=/GEYSER_API_KEY=/" $BOT_DIR/.env && echo "🔓 GEYSER_API_KEY uncommented"'
alias bot-geyser='grep -q "^GEYSER_API_KEY=" $BOT_DIR/.env && sudo systemctl restart whale-bot && echo "🟢 gRPC mode" || echo "❌ Run bot-ungeyser first"'

# ─────────────────────────────────────────────────────────────
# 📜 LOGS
# ─────────────────────────────────────────────────────────────
alias bot-logs='tail -f $BOT_DIR/logs/bot-whale-copy.log'
alias bot-logs-100='tail -100 $BOT_DIR/logs/bot-whale-copy.log'
alias bot-trades='grep -h "BUY\|SELL\|bought\|sold\|EMIT" $BOT_DIR/logs/bot-whale-copy.log | tail -30'
alias bot-whales='grep -h "WHALE" $BOT_DIR/logs/bot-whale-copy.log | tail -30'
alias bot-errors='grep -h "ERROR\|FAILED" $BOT_DIR/logs/bot-whale-copy.log | tail -20'
alias bot-watchdog='grep -i "WATCHDOG" $BOT_DIR/logs/bot-whale-copy.log | tail -20'

# ─────────────────────────────────────────────────────────────
# 📊 INFO
# ─────────────────────────────────────────────────────────────
alias bot-stats='curl -s http://localhost:8000/health 2>/dev/null | python3 -m json.tool || echo "No stats"'
alias bot-balance='cd $BOT_DIR && ./venv/bin/python3 scripts/check_balance.py'
alias bot-config='cat $BOT_DIR/bots/bot-whale-copy.yaml'
alias bot-edit='nano $BOT_DIR/bots/bot-whale-copy.yaml'
alias bot-strategy='cd $BOT_DIR && ./venv/bin/python3 scripts/show_strategy.py'

# ─────────────────────────────────────────────────────────────
# 🛡️ NO-SL — исключения из стоп-лосса
# ─────────────────────────────────────────────────────────────
# Usage: no-sl list | no-sl add <MINT> | no-sl remove <MINT>
alias no-sl='cd $BOT_DIR && ./venv/bin/python3 scripts/no_sl.py'

# ─────────────────────────────────────────────────────────────
# 💰 TRADING
# ─────────────────────────────────────────────────────────────
alias buy='cd $BOT_DIR && ./venv/bin/python3 buy.py'
alias sell='cd $BOT_DIR && ./venv/bin/python3 sell.py'
alias wsync='cd $BOT_DIR && ./venv/bin/python3 wsync.py'

buysync() { cd $BOT_DIR && ./venv/bin/python3 buy.py "$1" "$2" && sleep 3 && wsync && echo "✅ Bought + synced"; }

# ─────────────────────────────────────────────────────────────
# ⚡ QUICK SELL
# ─────────────────────────────────────────────────────────────
sell10()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 10; }
sell20()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 20; }
sell30()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 30; }
sell40()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 40; }
sell50()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 50; }
sell60()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 60; }
sell70()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 70; }
sell80()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 80; }
sell90()  { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 90; }
sell100() { cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 100; }

# ─────────────────────────────────────────────────────────────
# 🗑️ DUST — очистка мусорных токенов
# ─────────────────────────────────────────────────────────────
# Usage: dust [threshold]      — сжечь всё < threshold (default: $0.40)
#        dust 0.5 --dry        — preview с порогом $0.50
#        dust-dry [threshold]  — preview без удаления (default: $0.40)
dust()     { cd "$BOT_DIR" && ./venv/bin/python3 cleanup_dust.py "${1:-0.40}" "${@:2}"; }
dust-dry() { cd "$BOT_DIR" && ./venv/bin/python3 cleanup_dust.py "${1:-0.40}" --dry; }

# ─────────────────────────────────────────────────────────────
# 🐋 WHALE — управление китами
# ─────────────────────────────────────────────────────────────
# Usage: whale add <ADDR> [label] | whale del <ADDR|LABEL>
#        whale list [search]    | whale info <ADDR|LABEL>
#        whale sync             | whale <MINT|SYMBOL>
alias whale='cd $BOT_DIR && ./venv/bin/python3 scripts/whale_cli.py'

echo "🐋 Whale Bot aliases loaded"
