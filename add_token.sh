#!/bin/bash
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
    balance = 0
    async with aiohttp.ClientSession() as session:
        # Проверяем ОБА Token программы
        for program in ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [WALLET, {"mint": MINT}, {"encoding": "jsonParsed"}]
            }
            try:
                async with session.post(RPC, json=payload, timeout=30) as resp:
                    data = await resp.json()
                accounts = data.get("result", {}).get("value", [])
                if accounts:
                    info = accounts[0].get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    balance = float(info.get("tokenAmount", {}).get("uiAmount") or 0)
                    if balance > 0:
                        print(f"✅ Баланс: {balance:,.2f} токенов (program: {program[:8]}...)")
                        break
            except Exception as e:
                pass
    
    if balance <= 0:
        print(f"❌ Токен не найден в кошельке или баланс 0")
        return
    
    # Получаем символ и цену через DexScreener
    symbol = "UNKNOWN"
    current_price = 0
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"https://api.dexscreener.com/latest/dex/tokens/{MINT}", timeout=10) as resp:
                data = await resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    symbol = pairs[0].get("baseToken", {}).get("symbol", "UNKNOWN")
                    current_price = float(pairs[0].get("priceNative", 0) or 0)
        except:
            pass
    
    print(f"✅ Symbol: {symbol}")
    print(f"✅ Текущая цена: {current_price:.10f} SOL")
    
    # Entry price
    if ENTRY_PRICE_ARG:
        entry_price = float(ENTRY_PRICE_ARG)
    else:
        entry_price = current_price
    print(f"✅ Entry price: {entry_price:.10f} SOL")
    
    # Удаляем из Redis sold_mints
    import subprocess
    subprocess.run(["redis-cli", "SREM", "sold_mints", MINT], capture_output=True)
    
    # Загружаем и обновляем positions.json
    with open("positions.json", "r") as f:
        positions = json.load(f)
    
    positions = [p for p in positions if p.get("mint") != MINT]
    
    new_position = {
        "mint": MINT, "symbol": symbol, "entry_price": entry_price,
        "quantity": balance, "entry_time": datetime.now().isoformat(),
        "take_profit_price": entry_price * 10000, "stop_loss_price": entry_price * 0.7,
        "max_hold_time": 0, "tsl_enabled": True, "tsl_activation_pct": 0.3,
        "tsl_trail_pct": 0.5, "tsl_active": False, "high_water_mark": entry_price,
        "tsl_trigger_price": 0.0, "tsl_sell_pct": 0.7, "is_active": True,
        "is_moonbag": False, "dca_enabled": True, "dca_pending": False,
        "dca_trigger_pct": 0.2, "dca_bought": False, "dca_first_buy_pct": 0.5,
        "original_entry_price": entry_price, "state": "open",
        "platform": "pump_fun", "bonding_curve": None,
        "created_at": datetime.now().isoformat()
    }
    
    positions.append(new_position)
    
    with open("positions.json", "w") as f:
        json.dump(positions, f, indent=2, default=str)
    
    print()
    print("=" * 50)
    print(f"✅ ПОЗИЦИЯ ДОБАВЛЕНА: {symbol}")
    print(f"   Quantity: {balance:,.2f}")
    print(f"   Entry: {entry_price:.10f} SOL")
    print(f"   SL: -30% | TSL: +30% | DCA: -20%")
    print("=" * 50)
    print("🔄 Перезапусти бота: bot-restart")

asyncio.run(main())
PYEOF
