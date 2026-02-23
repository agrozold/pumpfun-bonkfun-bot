#!/usr/bin/env bash
BOT_DIR="/opt/pumpfun-bonkfun-bot"
BOT_SERVICE="whale-bot"

# ─────────────────────────────────────────────────────────────
# 🛑 BLACKLIST
# ─────────────────────────────────────────────────────────────
alias blacklist='cd $BOT_DIR && ./venv/bin/python3 scripts/blacklist_cli.py'

# ─────────────────────────────────────────────────────────────
# 🤖 BOT CONTROL
# ─────────────────────────────────────────────────────────────
alias bot-start='sudo systemctl start whale-bot && echo "✅ Started"'
alias bot-stop='sudo systemctl stop whale-bot 2>/dev/null; pkill -9 -f bot_runner.py 2>/dev/null; sleep 1; echo "⛔ Stopped"'
alias bot-restart='sudo systemctl stop whale-bot 2>/dev/null; pkill -9 -f bot_runner.py 2>/dev/null; sleep 2; sudo systemctl start whale-bot; sleep 1; echo "✅ restarted (PID: $(systemctl show whale-bot -p MainPID --value))"'
alias bot-status='sudo systemctl status whale-bot --no-pager | head -40; echo ""; curl -s http://localhost:8000/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(webhook offline)"'

alias bot-mode='if grep -q "^GEYSER_API_KEY=" $BOT_DIR/.env; then echo "🟢 gRPC + Webhook"; else echo "🟡 Webhook only"; fi'
alias bot-webhook='sed -i "s/^GEYSER_API_KEY=/#GEYSER_API_KEY=/" $BOT_DIR/.env && sudo systemctl restart whale-bot && echo "🟡 Webhook-only mode"'
alias bot-ungeyser='sed -i "s/^#GEYSER_API_KEY=/GEYSER_API_KEY=/" $BOT_DIR/.env && echo "🔓 GEYSER_API_KEY uncommented"'
alias bot-geyser='grep -q "^GEYSER_API_KEY=" $BOT_DIR/.env && sudo systemctl restart whale-bot && echo "🟢 gRPC mode" || echo "❌ Run bot-ungeyser first"'

# ─────────────────────────────────────────────────────────────
# 📜 LOGS
# ─────────────────────────────────────────────────────────────
alias bot-logs='tail -f $BOT_DIR/logs/bot-whale-copy.log'
alias bot-trades='grep -h "BUY\|SELL\|bought\|sold\|EMIT" $BOT_DIR/logs/bot-whale-copy.log | tail -30'
alias bot-whales='grep -h "WHALE" $BOT_DIR/logs/bot-whale-copy.log | tail -30'
alias bot-errors='grep -h "ERROR\|FAILED" $BOT_DIR/logs/bot-whale-copy.log | tail -50'
alias bot-health='curl -s http://localhost:8000/health 2>/dev/null | python3 -m json.tool || echo "Webhook server not running"'

# ─────────────────────────────────────────────────────────────
# 📊 INFO
# ─────────────────────────────────────────────────────────────
alias bot-stats='curl -s http://localhost:8000/health 2>/dev/null | python3 -m json.tool || echo "No stats"'
alias bot-balance='cd $BOT_DIR && ./venv/bin/python3 scripts/check_balance.py'
alias bot-config='cat $BOT_DIR/bots/bot-whale-copy.yaml'
alias bot-edit='nano $BOT_DIR/bots/bot-whale-copy.yaml'
alias bot-strategy='cd $BOT_DIR && ./venv/bin/python3 scripts/show_strategy.py'

# ─────────────────────────────────────────────────────────────
# 🛡️ NO-SL
# ─────────────────────────────────────────────────────────────
alias no-sl='cd $BOT_DIR && ./venv/bin/python3 scripts/no_sl.py'

# ─────────────────────────────────────────────────────────────
# 💰 TRADING
# ─────────────────────────────────────────────────────────────
alias buy='cd $BOT_DIR && ./venv/bin/python3 buy.py'
alias sell='cd $BOT_DIR && ./venv/bin/python3 sell.py'
alias wsync='cd $BOT_DIR && ./venv/bin/python3 wsync.py'
alias buysync='cd $BOT_DIR && ./venv/bin/python3 buy.py "$1" "$2" && sleep 3 && ./venv/bin/python3 wsync.py && echo "✅ Bought + synced"'

# ⚡ QUICK SELL
alias sell10='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 10'
alias sell20='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 20'
alias sell30='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 30'
alias sell40='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 40'
alias sell50='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 50'
alias sell60='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 60'
alias sell70='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 70'
alias sell80='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 80'
alias sell90='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 90'
alias sell100='cd $BOT_DIR && ./venv/bin/python3 sell.py "$1" 100'

# ─────────────────────────────────────────────────────────────
# 🗑️ DUST
# ─────────────────────────────────────────────────────────────
alias dust='cd $BOT_DIR && ./venv/bin/python3 scripts/dust_cleaner.py'
alias dust-dry='cd $BOT_DIR && ./venv/bin/python3 scripts/dust_cleaner.py --dry'

# ─────────────────────────────────────────────────────────────
# 🐋 WHALE
# ─────────────────────────────────────────────────────────────
alias whale='cd $BOT_DIR && ./venv/bin/python3 scripts/whale_cli.py'

# Session 4: Cleaners
alias zombies='cd $BOT_DIR && ./venv/bin/python3 scripts/zombie_cleaner.py'

echo "🐋 Whale Bot aliases loaded"
