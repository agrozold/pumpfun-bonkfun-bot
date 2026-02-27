#!/usr/bin/env bash
BOT_DIR="/opt/pumpfun-bonkfun-bot"
BOT_SERVICE="whale-bot"

# ─── BOT CONTROL (ТОЛЬКО whale-bot)
__bot_wait_active() {
  local svc="$1"
  for i in {1..40}; do
    sleep 1
    st="$(systemctl is-active "$svc" 2>/dev/null || true)"
    [ "$st" = "active" ] && return 0
    [ "$st" = "failed" ] && return 1
  done
  return 2
}

__bot_start()   { sudo systemctl start   "$BOT_SERVICE"; __bot_wait_active "$BOT_SERVICE"; }
__bot_stop()    { sudo systemctl stop    "$BOT_SERVICE"; }
__bot_restart() { sudo systemctl restart "$BOT_SERVICE"; __bot_wait_active "$BOT_SERVICE"; }

alias bot-start='__bot_start && echo "✅ started" || echo "❌ start failed (см. bot-logs)"'
alias bot-stop='__bot_stop && echo "⛔ stopped"'
alias bot-restart='__bot_restart && echo "✅ restarted" || echo "❌ restart failed (см. bot-logs)"'

alias bot-status='sudo systemctl status whale-bot --no-pager | head -60'
alias bot-logs='tail -f /opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log'
alias bot-errors='grep -hE "Traceback|ERROR|Exception|FAILED|IndentationError" /opt/pumpfun-bonkfun-bot/logs/bot-whale-copy.log | tail -80'

# режимы geyser (как и было по смыслу)
alias bot-mode='if grep -q "^GEYSER_API_KEY=" $BOT_DIR/.env; then echo "🟢 gRPC + Webhook"; else echo "🟡 Webhook only"; fi'
alias bot-webhook='sed -i "s/^GEYSER_API_KEY=/#GEYSER_API_KEY=/" $BOT_DIR/.env && sudo systemctl restart whale-bot && echo "🟡 Webhook-only mode"'
alias bot-ungeyser='sed -i "s/^#GEYSER_API_KEY=/GEYSER_API_KEY=/" $BOT_DIR/.env && echo "🔓 GEYSER_API_KEY uncommented"'
alias bot-geyser='grep -q "^GEYSER_API_KEY=" $BOT_DIR/.env && sudo systemctl restart whale-bot && echo "🟢 gRPC mode" || echo "❌ Run bot-ungeyser first"'

# ─── TRADING
alias buy='cd $BOT_DIR && ./venv/bin/python3 buy.py'
alias sell='cd $BOT_DIR && ./venv/bin/python3 sell.py'
alias wsync='cd $BOT_DIR && ./venv/bin/python3 wsync.py'

# ─── QUICK SELL
sell10()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 10; }
sell20()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 20; }
sell30()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 30; }
sell40()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 40; }
sell50()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 50; }
sell60()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 60; }
sell70()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 70; }
sell80()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 80; }
sell90()  { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 90; }
sell100() { cd "$BOT_DIR" && ./venv/bin/python3 sell.py "$1" 100; }

# ─── DUST (дефолт 0.40 как у тебя работало)
__dust()     { cd "$BOT_DIR" && ./venv/bin/python3 cleanup_dust.py "${1:-0.40}" "${@:2}"; }
__dust_dry() { cd "$BOT_DIR" && ./venv/bin/python3 cleanup_dust.py "${1:-0.40}" --dry; }
alias dust='__dust'
alias dust-dry='__dust_dry'

# ─── NO-SL / WHALE / BLACKLIST
alias no-sl='cd $BOT_DIR && ./venv/bin/python3 scripts/no_sl.py'
alias whale='cd $BOT_DIR && ./venv/bin/python3 scripts/whale_cli.py'
alias blacklist='cd $BOT_DIR && ./venv/bin/python3 scripts/blacklist_cli.py'

echo "✅ aliases.local loaded for whale-bot only"

alias token-add='cd $BOT_DIR && ./venv/bin/python3 scripts/token_add.py'
