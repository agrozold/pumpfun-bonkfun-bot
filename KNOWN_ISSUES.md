# Known Issues & Design Decisions

## Entry Price on Re-Buy (FIXED 2026-02-08)
- **Problem**: When using `buy` command on existing position, entry_price was getting overwritten twice
- **Root cause**: buy.py updates positions.json directly, then tx_callback also fires and recalculates
- **Solution**: tx_callback SKIPS update if position already exists. buy.py is the single source of truth for manual buys
- **Files**: buy.py (lines 1080-1090), src/core/tx_callbacks.py (lines 80-85)
- **Rule**: NEVER add entry_price recalculation to tx_callbacks for existing positions

## NO_SL Protection (FIXED 2026-02-07)
- **Problem**: 7 different code paths could sell NO_SL tokens
- **Solution**: All 7 paths check NO_SL_MINTS before selling
- **Files**: src/trading/universal_trader.py (search NO_SL_MINTS)
- **Rule**: ANY new sell path MUST check NO_SL_MINTS first

## DCA Config (FIXED 2026-02-08)
- **Problem**: dca_enabled was hardcoded True in 3 places
- **Solution**: Moved to YAML config (bot-edit -> dca_enabled: true/false)
- **Files**: bots/bot-whale-copy.yaml, src/trading/universal_trader.py, src/core/tx_callbacks.py
- **Rule**: NEVER hardcode trading parameters, always read from self.config

## tsl_triggered Field (FIXED 2026-02-08)
- **Problem**: Field used in code but missing from Position dataclass
- **Solution**: Added tsl_triggered: bool = False to dataclass
- **Files**: src/trading/position.py
- **Rule**: ALL position fields must be in dataclass AND in to_dict/from_dict

## systemd KillMode (FIXED 2026-02-08)
- **Problem**: bot-restart left orphan child processes
- **Solution**: Added KillMode=control-group to whale-bot.service
- **Rule**: Always use systemd aliases (bot-start/stop/restart), never run bot_runner.py manually
