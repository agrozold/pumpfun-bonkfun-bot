#!/usr/bin/env python3
"""Show current strategy parameters from yaml config."""
import yaml

with open("/opt/pumpfun-bonkfun-bot/bots/bot-whale-copy.yaml") as f:
    cfg = yaml.safe_load(f)

t = cfg.get("trade", {})
sl = t.get("stop_loss_percentage", 0)
tp = t.get("take_profit_percentage", 0)
tsl_a = t.get("tsl_activation_pct", 0)
tsl_t = t.get("tsl_trail_pct", 0)
tsl_s = t.get("tsl_sell_pct", 0)
tp_s = t.get("tp_sell_pct", 0)
mb = t.get("moon_bag_percentage", 0)
dca = t.get("dca_enabled", False)

print("=== WHALE COPY BOT STRATEGY ===")
print(f"  Buy Amount:        {t.get('buy_amount', '?')} SOL")
print(f"  Stop Loss:         -{sl*100:.0f}%")
print(f"  Take Profit:       +{tp*100:.0f}%")
print(f"  TP Sell Pct:       {tp_s*100:.0f}%")
print(f"  TSL Enabled:       {t.get('tsl_enabled', False)}")
print(f"  TSL Activation:    +{tsl_a*100:.0f}%")
print(f"  TSL Trail:         -{tsl_t*100:.0f}% from HWM")
print(f"  TSL Sell Pct:      {tsl_s*100:.0f}%")
print(f"  Moon Bag:          {mb*100:.0f}%")
print(f"  DCA Enabled:       {dca}")
print(f"  Price Check:       {t.get('price_check_interval', '?')}s")
print("  ─────────────────────────────")
print("  HARD SL:           -35% (code)")
print("  EMERGENCY SL:      -45% (code)")
print("  TSL floor:         entry price (never sells at loss)")
