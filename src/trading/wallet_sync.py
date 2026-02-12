"""
Wallet Sync - автоматическая синхронизация кошелька с positions.json

ПРОБЛЕМА: Бот покупает токены, но не сохраняет их в positions.json
РЕШЕНИЕ: При старте сканируем кошелёк и добавляем потерянные токены

Запуск: python -m src.trading.wallet_sync
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58

POSITIONS_FILE = Path("positions.json")

# Import real entry price finder
try:
    from trading.wallet_sync_fix import get_real_entry_price
except ImportError:
    get_real_entry_price = None

# === STRATEGY CONFIG (from yaml — single source of truth) ===
def _load_strategy_config():
    """Load strategy params from yaml. NO hardcoded defaults."""
    import yaml as _yaml
    from pathlib import Path as _Path
    for yp in [_Path("bots/bot-whale-copy.yaml"), _Path("/opt/pumpfun-bonkfun-bot/bots/bot-whale-copy.yaml")]:
        if yp.exists():
            with open(yp) as _f:
                _cfg = _yaml.safe_load(_f)
            _t = _cfg.get("trade", {})
            return {
                "stop_loss_pct": _t["stop_loss_percentage"],
                "take_profit_pct": _t["take_profit_percentage"],
                "tsl_activation_pct": _t["tsl_activation_pct"],
                "tsl_trail_pct": _t["tsl_trail_pct"],
                "tsl_sell_pct": _t["tsl_sell_pct"],
            }
    raise FileNotFoundError("bot-whale-copy.yaml not found for strategy config")

_STRAT = _load_strategy_config()
# === END STRATEGY CONFIG ===


# DexScreener для получения цен и символов
async def get_token_info_dexscreener(mint: str, max_retries: int = 3) -> dict | None:
    """Получить информацию о токене с DexScreener с retry."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status == 429:
                        wait = 2 ** attempt
                        print(f"[DEXSCREENER] Rate limited, waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status != 200:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(1)
                            continue
                        return None
                    data = await resp.json()
                    pairs = data.get("pairs", [])
                    if not pairs:
                        return await _get_token_info_birdeye(mint)
                    pair = pairs[0]
                    return {
                        "symbol": pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                        "name": pair.get("baseToken", {}).get("name", "Unknown"),
                        "price_sol": float(pair.get("priceNative", 0) or 0),
                        "dex": pair.get("dexId", "unknown"),
                    }
        except asyncio.TimeoutError:
            print(f"[DEXSCREENER] Timeout, retry {attempt+1}")
            await asyncio.sleep(1)
        except Exception as e:
            print(f"[DEXSCREENER] Error: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
    return None


async def _get_token_info_birdeye(mint: str) -> dict | None:
    """Fallback to Birdeye API."""
    import os
    api_key = os.getenv("BIRDEYE_API_KEY")
    if not api_key:
        return {"symbol": "UNKNOWN", "name": "Unknown", "price_sol": 0.0001, "dex": "unknown"}
    
    url = f"https://public-api.birdeye.so/defi/token_overview?address={mint}"
    headers = {"X-API-KEY": api_key}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return {"symbol": "UNKNOWN", "name": "Unknown", "price_sol": 0.0001, "dex": "unknown"}
                data = await resp.json()
                token_data = data.get("data", {})
                return {
                    "symbol": token_data.get("symbol", "UNKNOWN"),
                    "name": token_data.get("name", "Unknown"),
                    "price_sol": float(token_data.get("price", 0) or 0),
                    "dex": "birdeye",
                }
    except Exception:
        return {"symbol": "UNKNOWN", "name": "Unknown", "price_sol": 0.0001, "dex": "unknown"}



async def get_wallet_tokens(rpc: str, wallet: str) -> list[dict]:
    """Получить все токены в кошельке."""
    tokens = []
    
    for prog_name, prog_id in [
        ("Token", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"),
        ("Token2022", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
    ]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet,
                {"programId": prog_id},
                {"encoding": "jsonParsed"}
            ]
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc, json=payload, timeout=30) as resp:
                data = await resp.json()
        
        accounts = data.get("result", {}).get("value", [])
        
        for acc in accounts:
            try:
                parsed = acc.get("account", {}).get("data", {}).get("parsed", {})
                info = parsed.get("info", {})
                mint = info.get("mint", "")
                token_amount = info.get("tokenAmount", {})
                ui_amount = float(token_amount.get("uiAmount") or 0)
                decimals = token_amount.get("decimals", 6)
                
                # Пропускаем пустые и dust (< 1 токен)
                if ui_amount >= 1:
                    tokens.append({
                        "mint": mint,
                        "amount": ui_amount,
                        "decimals": decimals,
                        "program": prog_name
                    })
            except Exception:
                pass
    
    return tokens


def load_positions() -> list[dict]:
    """Загрузить текущие позиции."""
    if not POSITIONS_FILE.exists():
        return []
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except:
        return []


def save_positions(positions: list[dict]):
    """Сохранить позиции."""
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


async def sync_wallet():
    """Основная функция синхронизации."""
    print("=" * 60)
    print("[WALLET SYNC] Starting wallet synchronization...")
    print("=" * 60)
    
    # Получаем креды из env
    rpc = os.getenv("ALCHEMY_RPC_ENDPOINT") or os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    pk = os.getenv("SOLANA_PRIVATE_KEY")
    
    if not pk:
        print("[ERROR] SOLANA_PRIVATE_KEY not set")
        return
    
    kp = Keypair.from_bytes(base58.b58decode(pk))
    wallet = str(kp.pubkey())
    
    print(f"[WALLET] {wallet}")
    print(f"[RPC] {rpc[:50]}...")
    print()
    
    # Получаем токены в кошельке
    wallet_tokens = await get_wallet_tokens(rpc, wallet)
    print(f"[WALLET] Found {len(wallet_tokens)} tokens with balance")
    
    # Загружаем текущие позиции
    positions = load_positions()
    position_mints = {p.get("mint") for p in positions}
    print(f"[POSITIONS] Current: {len(positions)} positions")

    # === CLEANUP: Удаляем фантомные позиции (токенов нет на кошельке) ===
    wallet_mints = {t["mint"] for t in wallet_tokens}
    phantom_positions = [p for p in positions if p.get("mint") not in wallet_mints]
    
    if phantom_positions:
        print(f"\n[CLEANUP] Found {len(phantom_positions)} PHANTOM positions (no tokens on wallet)")
        for p in phantom_positions:
            print(f"  [REMOVE] {p.get('symbol', '?')} | {p.get('mint', '')[:20]}...")
        positions = [p for p in positions if p.get("mint") in wallet_mints]
        position_mints = {p.get("mint") for p in positions}
        print(f"[CLEANUP] Removed {len(phantom_positions)} phantom positions")
    
    # Находим потерянные токены
    # Import Jupiter price
    from utils.jupiter_price import get_token_price
    
    # Load sold_mints from Redis to avoid re-adding moonbags
    import subprocess as _sp
    _sold_raw = _sp.run(['redis-cli', 'SMEMBERS', 'sold_mints'], capture_output=True, text=True)
    sold_mints = set(_sold_raw.stdout.strip().split('\n')) if _sold_raw.stdout.strip() else set()
    if sold_mints:
        print(f'[SOLD_MINTS] {len(sold_mints)} tokens in sold list (will skip)')

    lost_tokens = []
    for token in wallet_tokens:
        if token["mint"] not in position_mints:
            # Skip sold tokens (moonbags, etc.)
            if token["mint"] in sold_mints:
                print(f'  [SKIP] {token["mint"][:16]}... is in sold_mints (moonbag/sold)')
                continue
            # DUST FILTER: Get real price via Jupiter
            token_price = token.get("price", 0)
            if token_price <= 0:
                try:
                    token_price, _ = await get_token_price(token["mint"])
                    token_price = token_price or 0
                except:
                    token_price = 0
            
            token_value = token.get("amount", 0) * token_price
            if token_value < 0.003:  # < ~$0.60
                print(f"  [DUST] Skipping {token.get('symbol', token['mint'][:8])} - value {token_value:.6f} SOL")
                continue
            lost_tokens.append(token)
    
    # UPDATE existing positions with real wallet balance
    updated = 0
    wallet_tokens_dict = {t["mint"]: t for t in wallet_tokens}
    for p in positions:
        mint = p.get("mint")
        if mint in wallet_tokens_dict:
            real_qty = wallet_tokens_dict[mint]["amount"]
            old_qty = p.get("quantity", 0)
            if abs(real_qty - old_qty) > 0.01:
                p["quantity"] = real_qty
                updated += 1
                print(f"  [UPDATE] {p.get('symbol', mint[:8]+'...')} qty: {old_qty:.2f} -> {real_qty:.2f}")
    if updated:
        print(f"[UPDATED] {updated} positions with new quantities")
        save_positions(positions)

    if not lost_tokens:
        # Sync Redis even if no new tokens
        if updated:
            import subprocess
            import json as js
            # Update ONLY changed positions in Redis (preserve runtime state like tsl_active, HWM)
            for p in positions:
                mint = p.get("mint")
                if not mint:
                    continue
                # Get existing Redis data and merge quantity update
                existing_raw = subprocess.run(
                    ["redis-cli", "HGET", "whale:positions", mint],
                    capture_output=True, text=True
                )
                if existing_raw.stdout.strip():
                    try:
                        existing = js.loads(existing_raw.stdout.strip())
                        # Only update quantity from wallet scan, keep all runtime state
                        existing["quantity"] = p["quantity"]
                        subprocess.run(["redis-cli", "HSET", "whale:positions", mint, js.dumps(existing)], capture_output=True)
                    except:
                        subprocess.run(["redis-cli", "HSET", "whale:positions", mint, js.dumps(p)], capture_output=True)
                else:
                    subprocess.run(["redis-cli", "HSET", "whale:positions", mint, js.dumps(p)], capture_output=True)
            print(f"[SYNCED] Redis updated (incremental, {updated} positions changed)")
        print("\n[OK] All tokens are tracked!")
        return
    
    print(f"\n[ALERT] Found {len(lost_tokens)} UNTRACKED tokens!")
    print()
    
    # Получаем информацию и добавляем позиции
    added = 0
    for token in lost_tokens:
        mint = token["mint"]
        amount = token["amount"]
        
        print(f"[SYNC] Processing {mint[:16]}...")
        
        # Получаем цену и символ
        info = await get_token_info_dexscreener(mint)
        
        if info:
            symbol = info["symbol"]
            price = info["price_sol"]
            dex = info["dex"]
        else:
            symbol = "UNKNOWN"
            price = 0.0001  # placeholder
            dex = "unknown"
        
        # Определяем платформу по суффиксу mint
        if mint.endswith("pump"):
            platform = "pump_fun"
        elif mint.endswith("bonk"):
            platform = "lets_bonk"
        elif mint.endswith("BAGS"):
            platform = "bags"
        else:
            platform = "unknown"
        
        # Получаем РЕАЛЬНУЮ цену покупки из истории транзакций
        wallet_address = str(wallet.pubkey()) if hasattr(wallet, 'pubkey') else wallet
        if get_real_entry_price:
            entry_price, price_source = await get_real_entry_price(mint, wallet_address, price)
            print(f"          Entry price source: {price_source}")
        else:
            entry_price = price
            price_source = "current_fallback"
            print(f"          WARNING: Using current price (no history lookup)")
        
        # Создаём позицию с SL/TP
        # entry_price = реальная цена покупки (или текущая как fallback)
        # SL = -20%, TP = +10000%
        new_position = {
            "mint": mint,
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": amount,
            "entry_time": datetime.utcnow().isoformat(),
            "take_profit_price": entry_price * (1 + _STRAT["take_profit_pct"]),  # from yaml config
            "stop_loss_price": entry_price * (1 - _STRAT["stop_loss_pct"]),    # from yaml config
            "max_hold_time": 0,
            "tsl_enabled": True,
            "tsl_activation_pct": _STRAT["tsl_activation_pct"],
            "tsl_trail_pct": _STRAT["tsl_trail_pct"],
            "tsl_active": False,
            "high_water_mark": entry_price,
            "tsl_trigger_price": 0.0,
            "tsl_sell_pct": _STRAT["tsl_sell_pct"],
            "is_active": True,
            "state": "open",
            "platform": platform,
            "bonding_curve": None,  # Will be derived when selling
            "is_moonbag": next((p.get("is_moonbag", False) for p in positions if p.get("mint") == mint), False),
        }
        
        positions.append(new_position)
        added += 1
        # NOTE: Do NOT remove from sold_mints — it protects against moonbag re-add
        # subprocess.run(["redis-cli", "SREM", "sold_mints", mint], capture_output=True)
        
        print(f"  [ADDED] {symbol}")
        print(f"          Entry Price: {entry_price:.10f} SOL (source: {price_source})")
        print(f"          Current Price: {price:.10f} SOL")
        print(f"          Amount: {amount:,.2f}")
        print(f"          Platform: {platform}")
        sl_pct = _STRAT["stop_loss_pct"]
        print(f"          SL: {price * (1 - sl_pct):.10f} (-{sl_pct*100:.0f}%)")
        tp_pct = _STRAT["take_profit_pct"]
        print(f"          TP: {price * (1 + tp_pct):.10f} (+{tp_pct*100:.0f}%)")
        print()
        
        # Rate limit для DexScreener
        await asyncio.sleep(0.5)
    
    # Сохраняем
    save_positions(positions)
    
    print("=" * 60)
    print(f"[DONE] Added {added} positions")
    print(f"[DONE] Total positions: {len(positions)}")
    print(f"[SAVED] {POSITIONS_FILE}")
    # Sync Redis with positions.json (incremental — preserve runtime state)
    import subprocess
    import json as js

    # Only ADD new positions and UPDATE quantities — never DEL whale:positions
    for p in positions:
        mint = p.get("mint", "")
        if not mint:
            continue
        existing_raw = subprocess.run(
            ["redis-cli", "HGET", "whale:positions", mint],
            capture_output=True, text=True
        )
        if existing_raw.stdout.strip():
            try:
                existing = js.loads(existing_raw.stdout.strip())
                # Update only quantity and entry_price from file, keep runtime state
                existing["quantity"] = p.get("quantity", existing.get("quantity", 0))
                subprocess.run(["redis-cli", "HSET", "whale:positions", mint, js.dumps(existing)], capture_output=True)
            except:
                subprocess.run(["redis-cli", "HSET", "whale:positions", mint, js.dumps(p)], capture_output=True)
        else:
            # New position — add as-is
            subprocess.run(["redis-cli", "HSET", "whale:positions", mint, js.dumps(p)], capture_output=True)

    # Remove phantoms from Redis
    redis_keys_raw = subprocess.run(["redis-cli", "HKEYS", "whale:positions"], capture_output=True, text=True)
    redis_mints = set(redis_keys_raw.stdout.strip().split("\n")) if redis_keys_raw.stdout.strip() else set()
    json_mints = {p.get("mint") for p in positions if p.get("mint")}
    for orphan in redis_mints - json_mints:
        if orphan:
            subprocess.run(["redis-cli", "HDEL", "whale:positions", orphan], capture_output=True)
            print(f"  [REDIS] Removed orphan: {orphan[:16]}...")

    print(f"[SYNCED] Redis whale:positions ({len(positions)} positions, incremental)")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(sync_wallet())
