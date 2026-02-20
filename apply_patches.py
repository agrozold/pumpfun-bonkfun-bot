#!/usr/bin/env python3
"""Whale Copy Bot - Master Patch P1..P6 | 2026-02-19"""
import re, shutil, sys
from pathlib import Path

BASE   = Path("/opt/pumpfun-bonkfun-bot")
DRY    = "--dry" in sys.argv
PASSED = []
FAILED = []

def patch(file_rel, pattern, replacement, tag, flags=0):
    path = BASE / file_rel
    if not path.exists():
        FAILED.append(f"❌ {tag}: FILE NOT FOUND: {file_rel}")
        return
    txt = path.read_text()
    new = re.sub(pattern, replacement, txt, count=1, flags=flags)
    if new == txt:
        FAILED.append(f"❌ {tag}: pattern NOT FOUND in {file_rel}")
        return
    if DRY:
        print(f"\n{'─'*60}\n[DRY] {tag}\n{'─'*60}")
        old_lines = txt.splitlines()
        new_lines = new.splitlines()
        for i, (o, n) in enumerate(zip(old_lines, new_lines)):
            if o != n:
                print(f"  L{i+1} - {o}")
                print(f"  L{i+1} + {n}")
    else:
        shutil.copy(path, str(path) + ".bak")
        path.write_text(new)
    PASSED.append(f"✅ {tag}")

# ── P1: fallback_seller.py — всегда return actual_ui ──────────────────────────
patch(
    "src/trading/fallback_seller.py",
    r"(        logger\.info\(f\"\[POST-BUY VERIFY\] OK:.*?\n)\s*\nreturn expected_tokens,\s*sol_spent / expected_tokens[^\n]*\n",
    r"\1\n        return actual_ui, sol_spent / actual_ui if actual_ui > 0 else 0, actual_decimals  # P1 fix: always actual\n\nreturn expected_tokens, sol_spent / expected_tokens if expected_tokens > 0 else 0, actual_decimals\n",
    "P1 fallback_seller: return actual_ui inside ratio-ok branch",
    re.DOTALL
)

# ── P2: universal_trader.py — moonbag mismatch guard ──────────────────────────
patch(
    "src/trading/universal_trader.py",
    r"(# mismatch[^\n]*\n\s+sell_quantity\s*=\s*)actual_balance(\s+# 100%[^\n]*)",
    r"""\1(  # P2 fix: preserve moonbag on partial TP
                int(actual_balance * tp_sell_pct)
                if exit_reason in ('TP', 'PARTIAL_TP') and tp_sell_pct is not None and tp_sell_pct < 1.0
                else actual_balance
            )\2""",
    "P2 universal_trader: moonbag mismatch guard",
    re.DOTALL
)

# ── P3b: universal_trader.py — snapshot _pre_sell_wallet_balance ──────────────
patch(
    "src/trading/universal_trader.py",
    r"(actual_balance\s*=\s*await self\._get_token_balance[^\n]*\n)(\s+)(await self\._sell_via_jupiter)",
    r"\1\2_pre_sell_wallet_balance = actual_balance  # P3: snap for VERIFY\n\2\3",
    "P3b universal_trader: add _pre_sell_wallet_balance snapshot",
    re.DOTALL
)

# ── P3: universal_trader.py — VERIFY использует pre-sell balance ──────────────
patch(
    "src/trading/universal_trader.py",
    r"(self\._verify_sell_in_background\([^)]*original_qty\s*=\s*)position\.quantity",
    r"\1_pre_sell_wallet_balance  # P3 fix: snap before sell",
    "P3 universal_trader: pre-sell balance to VERIFY",
    re.DOTALL
)

# ── P5: whale_geyser.py — moonbag re-register после partial TP ────────────────
patch(
    "src/monitoring/whale_geyser.py",
    r"(position\.tp_partial_done\s*=\s*True\n)",
    r"""\1                # P5 fix: re-register moonbag in gRPC
                position.is_moonbag = True
                hmw = position.entry_price_high_water_mark or position.entry_price
                self._sl_tp_triggers[mint] = {
                    'triggered': False,
                    'entry_price': hmw,
                    'sl_price': hmw * 0.70,
                    'tp_price': None,
                }
                self.logger.info(f"[MOONBAG] {mint[:8]} re-registered sl={hmw*0.70:.8f}")
""",
    "P5 whale_geyser: moonbag re-register after partial TP",
)

# ── P6: bot-whale-copy.yaml — take_profit_percentage ─────────────────────────
patch(
    "bots/bot-whale-copy.yaml",
    r"take_profit_percentage:\s*0\.1\b",
    "take_profit_percentage: 0.5  # P6 fix: TP after TSL activation",
    "P6 yaml: take_profit_percentage 0.1→0.5",
)

# ── Итог ──────────────────────────────────────────────────────────────────────
print("\n" + "═"*60)
for m in PASSED: print(m)
for m in FAILED: print(m)
print("═"*60)
mode = "DRY RUN — изменения НЕ записаны" if DRY else "ПАТЧИ ПРИМЕНЕНЫ — бэкапы *.bak рядом с файлами"
print(f"\n{mode}")
