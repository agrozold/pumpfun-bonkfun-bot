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
    lost_tokens = []
    for token in wallet_tokens:
        if token["mint"] not in position_mints:
            lost_tokens.append(token)
    
    if not lost_tokens:
        print("\n[OK] All tokens are tracked! No sync needed.")
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
        
        # Создаём позицию с SL/TP
        # ВАЖНО: entry_price = текущая цена (мы не знаем реальную цену покупки)
        # SL = -20%, TP = +10000%
        new_position = {
            "mint": mint,
            "symbol": symbol,
            "entry_price": price,
            "quantity": amount,
            "entry_time": datetime.utcnow().isoformat(),
            "take_profit_price": price * 100.0,  # +10000% (from config)
            "stop_loss_price": price * 0.8,    # -20%
            "max_hold_time": 0,
            "tsl_enabled": True,
            "tsl_activation_pct": 0.3,
            "tsl_trail_pct": 0.3,
            "tsl_active": False,
            "high_water_mark": price,
            "tsl_trigger_price": 0.0,
            "tsl_sell_pct": 0.9,
            "is_active": True,
            "state": "open",
            "platform": platform,
            "bonding_curve": None,  # Will be derived when selling
        }
        
        positions.append(new_position)
        added += 1
        
        print(f"  [ADDED] {symbol}")
        print(f"          Price: {price:.10f} SOL")
        print(f"          Amount: {amount:,.2f}")
        print(f"          Platform: {platform}")
        print(f"          SL: {price * 0.8:.10f} (-20%)")
        print(f"          TP: {price * 100.0:.10f} (+10000%)")
        print()
        
        # Rate limit для DexScreener
        await asyncio.sleep(0.5)
    
    # Сохраняем
    save_positions(positions)
    
    print("=" * 60)
    print(f"[DONE] Added {added} positions")
    print(f"[DONE] Total positions: {len(positions)}")
    print(f"[SAVED] {POSITIONS_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(sync_wallet())
