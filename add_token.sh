#!/bin/bash
# Использование: ./add_token.sh <MINT_ADDRESS> [ENTRY_PRICE_SOL]
# Пример: ./add_token.sh DRtvTCzfiKGhCVREmBbZdN9sB8PHeq9KdRZ3VmFhpump
# Пример с ценой: ./add_token.sh DRtvTCzfiKGhCVREmBbZdN9sB8PHeq9KdRZ3VmFhpump 0.000003366

cd /opt/pumpfun-bonkfun-bot

MINT="$1"
ENTRY_PRICE="$2"

if [ -z "$MINT" ]; then
    echo "❌ Укажи mint адрес токена!"
    echo "Использование: ./add_token.sh <MINT_ADDRESS> [ENTRY_PRICE_SOL]"
    exit 1
fi

echo "=== ДОБАВЛЕНИЕ ТОКЕНА ==="
echo "Mint: $MINT"

python3 << PYEOF
import json
import asyncio
import aiohttp
from datetime import datetime

MINT = "$MINT"
ENTRY_PRICE_ARG = "$ENTRY_PRICE"
WALLET = "BUjHaKKeEQ7PmaenX5CcDnVw1pRiYQJErr4gjAkvUzWN"
RPC = "https://lb.drpc.org/ogrpc?network=solana&dkey=AhgaFU4IRUa1ppdxz5AANAZ44rYj-6YR8LLieho1c5bd"

async def main():
    # 1. Получаем баланс токена (пробуем оба Token программы)
    balance = 0
    async with aiohttp.ClientSession() as session:
        for program in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [WALLET, {"mint": MINT, "programId": program}, {"encoding": "jsonParsed"}]
            }
            async with session.post(RPC, json=payload, timeout=30) as resp:
                data = await resp.json()
            accounts = data.get("result", {}).get("value", [])
            if accounts:
                info = accounts[0].get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                balance = float(info.get("tokenAmount", {}).get("uiAmount") or 0)
                if balance > 0:
                    break
    
    if balance <= 0:
        print(f"❌ Токен не найден в кошельке или баланс 0")
        return
    
    print(f"✅ Баланс: {balance:,.2f} токенов")
    
    # 2. Получаем символ и цену через DexScreener
    symbol = "UNKNOWN"
    current_price = 0
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{MINT}", timeout=10) as resp:
            data = await resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                symbol = pairs[0].get("baseToken", {}).get("symbol", "UNKNOWN")
                current_price = float(pairs[0].get("priceNative", 0) or 0)
    
    print(f"✅ Symbol: {symbol}")
    print(f"✅ Текущая цена: {current_price:.10f} SOL")
    
    # 3. Entry price - из аргумента или текущая
    if ENTRY_PRICE_ARG:
        entry_price = float(ENTRY_PRICE_ARG)
        print(f"✅ Entry price (из аргумента): {entry_price:.10f} SOL")
    else:
        entry_price = current_price
        print(f"✅ Entry price (текущая): {entry_price:.10f} SOL")
    
    # 4. Удаляем из Redis sold_mints
    import subprocess
    result = subprocess.run(["redis-cli", "SREM", "sold_mints", MINT], capture_output=True, text=True)
    if "1" in result.stdout:
        print(f"✅ Удалён из sold_mints")
    
    # 5. Загружаем и обновляем positions.json
    with open("positions.json", "r") as f:
        positions = json.load(f)
    
    # Удаляем старую позицию если есть
    positions = [p for p in positions if p.get("mint") != MINT]
    
    # Создаём новую позицию
    new_position = {
        "mint": MINT,
        "symbol": symbol,
        "entry_price": entry_price,
        "quantity": balance,
        "entry_time": datetime.now().isoformat(),
        "take_profit_price": entry_price * 10000,
        "stop_loss_price": entry_price * 0.7,
        "max_hold_time": 0,
        "tsl_enabled": True,
        "tsl_activation_pct": 0.3,
        "tsl_trail_pct": 0.5,
        "tsl_active": False,
        "high_water_mark": entry_price,
        "tsl_trigger_price": 0.0,
        "tsl_sell_pct": 0.7,
        "is_active": True,
        "is_moonbag": False,
        "dca_enabled": True,
        "dca_pending": False,
        "dca_trigger_pct": 0.2,
        "dca_bought": False,
        "dca_first_buy_pct": 0.5,
        "original_entry_price": entry_price,
        "state": "open",
        "platform": "pump_fun",
        "bonding_curve": None,
        "created_at": datetime.now().isoformat()
    }
    
    positions.append(new_position)
    
    with open("positions.json", "w") as f:
        json.dump(positions, f, indent=2, default=str)
    
    print()
    print("=" * 50)
    print(f"✅ ПОЗИЦИЯ ДОБАВЛЕНА!")
    print(f"   Symbol: {symbol}")
    print(f"   Quantity: {balance:,.2f}")
    print(f"   Entry: {entry_price:.10f} SOL")
    print(f"   SL: {entry_price * 0.7:.10f} SOL (-30%)")
    print(f"   TSL: +30%")
    print(f"   DCA: -20%")
    print("=" * 50)
    print()
    print("🔄 Перезапусти бота: bot-restart")

asyncio.run(main())
PYEOF
