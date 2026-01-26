#!/bin/bash
# =============================================================
# CLEANUP SCRIPT: Remove all sniper bots, keep only Whale Copy
# =============================================================

set -e
cd /opt/pumpfun-bonkfun-bot

echo "=========================================="
echo "  SNIPER CLEANUP - WHALE COPY ONLY MODE"
echo "=========================================="

# 1. Stop any running bot processes
echo "[1/7] Stopping any running bot processes..."
pkill -f "python.*bot_runner" 2>/dev/null || true
pkill -f "python.*universal_trader" 2>/dev/null || true
sleep 2

# 2. Backup before cleanup
echo "[2/7] Creating backup..."
BACKUP_DIR="backups/pre_cleanup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp -r bots/*.yaml "$BACKUP_DIR/" 2>/dev/null || true
cp -r bots/*.yaml.disabled "$BACKUP_DIR/" 2>/dev/null || true
echo "    Backup saved to: $BACKUP_DIR"

# 3. Remove sniper config files (both active and disabled)
echo "[3/7] Removing sniper bot configs..."
rm -f bots/bot-sniper-0-pump.yaml
rm -f bots/bot-sniper-0-pump.yaml.disabled
rm -f bots/bot-sniper-0-bonkfun.yaml
rm -f bots/bot-sniper-0-bonkfun.yaml.disabled
rm -f bots/bot-sniper-0-bags.yaml
rm -f bots/bot-sniper-0-bags.yaml.disabled
rm -f bots/bot-volume-sniper.yaml.disabled
rm -f bots/bot-whale-copy.yaml.disabled
echo "    Removed all sniper YAML files"

# 4. Update whale copy config with correct settings
echo "[4/7] Updating bot-whale-copy.yaml..."
cat > bots/bot-whale-copy.yaml << 'YAML'
enabled: true
env_file: .env
name: bot-whale-copy
platform: pump_fun
separate_process: true

filters:
  bro_address: null
  listener_type: fallback
  match_string: null
  max_token_age: 315360000
  yolo_mode: true
  sniper_enabled: false

rpc_endpoint: ${SOLANA_NODE_RPC_ENDPOINT}
wss_endpoint: ${CHAINSTACK_WSS_ENDPOINT}
private_key: ${SOLANA_PRIVATE_KEY}

priority_fees:
  enable_dynamic: false
  enable_fixed: true
  fixed_amount: 500000
  max_fee: 5000000

pumpportal:
  url: wss://pumpportal.fun/api/data

trade:
  buy_amount: 0.02
  buy_slippage: 0.3
  exit_strategy: tp_sl
  max_hold_time: 0
  min_sol_balance: 0.03
  moon_bag_percentage: 50
  price_check_interval: 1
  sell_slippage: 0.35
  stop_loss_percentage: 0.20
  tsl_enabled: true
  tsl_activation_pct: 0.3
  tsl_trail_pct: 0.3
  tsl_sell_pct: 0.5
  take_profit_percentage: 1.0

pattern_detection:
  enabled: false

scoring:
  enabled: false
  min_score: 75
  volume_weight: 30
  buy_pressure_weight: 35
  momentum_weight: 20
  liquidity_weight: 15

whale_copy:
  enabled: true
  wallets_file: smart_money_wallets.json
  min_buy_amount: 0.4
  helius_api_key: ''

dev_check:
  enabled: false
  max_tokens_created: 20
  min_account_age_days: 1

trending_scanner:
  enabled: false

whale_all_platforms: true

stablecoin_filter:
  - EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
  - Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB
  - USDH1SM1ojwWUga67PGrgFWUHibbjqMvuMaDkRJTgkX
  - USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA
  - 2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo
  - F3hW1kkYVXhMz9FRV8t3mEfwmLQygF7PtPSsofPCdmXR
  - So11111111111111111111111111111111111111112

token_vetting:
  enabled: false
YAML
echo "    bot-whale-copy.yaml updated"

# 5. Verify cleanup
echo "[5/7] Verifying cleanup..."
echo "    Remaining bot configs:"
ls -la bots/*.yaml 2>/dev/null || echo "    No .yaml files"

# 6. Create startup script
echo "[6/7] Creating whale-only startup script..."
cat > start_whale_copy.sh << 'STARTUP'
#!/bin/bash
# Start Whale Copy Bot only (no snipers)
cd /opt/pumpfun-bonkfun-bot
source venv/bin/activate

echo "============================================"
echo "  WHALE COPY BOT - Starting..."
echo "============================================"
echo "Config: bots/bot-whale-copy.yaml"
echo "Wallets: smart_money_wallets.json"
echo ""

# Check if already running
if [ -f whale_copy.pid ]; then
    OLD_PID=$(cat whale_copy.pid)
    if ps -p $OLD_PID > /dev/null 2>&1; then
        echo "Bot already running with PID: $OLD_PID"
        echo "To restart: kill $OLD_PID && ./start_whale_copy.sh"
        exit 1
    fi
fi

# Run in background with nohup
LOG_FILE="logs/whale_copy_$(date +%Y%m%d_%H%M%S).log"
nohup python -m src.bot_runner bots/bot-whale-copy.yaml > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > whale_copy.pid

sleep 2

# Verify started
if ps -p $PID > /dev/null 2>&1; then
    echo "Started successfully!"
    echo "PID: $PID (saved to whale_copy.pid)"
    echo "Log: $LOG_FILE"
    echo ""
    echo "Commands:"
    echo "  Monitor:  tail -f $LOG_FILE"
    echo "  Stop:     kill \$(cat whale_copy.pid)"
    echo "  Status:   ps aux | grep whale"
else
    echo "ERROR: Failed to start! Check logs:"
    tail -20 "$LOG_FILE"
    exit 1
fi
STARTUP
chmod +x start_whale_copy.sh

# 7. Create stop script
echo "[7/7] Creating stop script..."
cat > stop_whale_copy.sh << 'STOP'
#!/bin/bash
cd /opt/pumpfun-bonkfun-bot

if [ -f whale_copy.pid ]; then
    PID=$(cat whale_copy.pid)
    if ps -p $PID > /dev/null 2>&1; then
        echo "Stopping Whale Copy Bot (PID: $PID)..."
        kill $PID
        sleep 2
        if ps -p $PID > /dev/null 2>&1; then
            echo "Force killing..."
            kill -9 $PID
        fi
        rm -f whale_copy.pid
        echo "Stopped."
    else
        echo "Process not running (stale PID file)"
        rm -f whale_copy.pid
    fi
else
    echo "No PID file found. Checking for running processes..."
    pkill -f "python.*bot-whale-copy" 2>/dev/null && echo "Killed." || echo "No process found."
fi
STOP
chmod +x stop_whale_copy.sh

echo ""
echo "=========================================="
echo "  CLEANUP COMPLETE!"
echo "=========================================="
echo ""
echo "Remaining files in bots/:"
ls -la bots/
echo ""
echo "To start Whale Copy Bot:"
echo "  ./start_whale_copy.sh"
echo ""
echo "To stop:"
echo "  ./stop_whale_copy.sh"
echo ""
echo "To monitor:"
echo "  tail -f logs/whale_copy_*.log"
