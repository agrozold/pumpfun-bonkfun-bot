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
async def get_token_info_dexscreener(mint: str) -> dict | None:
    """Получить информацию о токене с DexScreener."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs", [])
                if not pairs:
                    return None
                # Берём первую пару
                pair = pairs[0]
                return {
                    "symbol": pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
                    "name": pair.get("baseToken", {}).get("name", "Unknown"),
                    "price_sol": float(pair.get("priceNative", 0) or 0),
                    "dex": pair.get("dexId", "unknown"),
                }
    except Exception as e:
        print(f"[DEXSCREENER] Error for {mint[:12]}...: {e}")
        return None


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
        # SL = -20% от текущей, TP = +100%
        new_position = {
            "mint": mint,
            "symbol": symbol,
            "entry_price": price,
            "quantity": amount,
            "entry_time": datetime.utcnow().isoformat(),
            "take_profit_price": price * 2.0,  # +100%
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
        print(f"          TP: {price * 2.0:.10f} (+100%)")
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
