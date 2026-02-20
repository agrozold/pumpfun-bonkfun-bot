import json, asyncio, aiohttp
from pathlib import Path
from datetime import datetime

CHAINSTACK = "https://solana-mainnet.core.chainstack.com/28c858a6ec92aafc2569516da978dfb8"
WALLET = "BUjHaKKeEQ7PmaenX5CcDnVw1pRiYQJErr4gjAkvUzWN"
POS_FILE = Path("/opt/pumpfun-bonkfun-bot/positions.json")

# PUCK данные с on-chain
PUCK = {
    "mint": "BQJcX1P5yfiiB3yyqSQPNHMSaQw2xuGCqkXAuwXoDaAJ",
    "symbol": "PUCK",
    "quantity": 29308.486799,
    "entry_price": 0.0,        # заполним ниже
    "original_entry_price": 0.0,
    "entry_time": "2026-02-19T08:35:18.000000",
    "entry_price_source": "cost_fallback",
    "entry_price_provisional": True,
    "platform": "pump_fun_direct",
    "state": "open",
    "is_active": True,
    "buy_confirmed": True,
    "tokens_arrived": True,
    "tsl_enabled": True,
    "tsl_activation_pct": 0.15,
    "tsl_trail_pct": 0.3,
    "tsl_active": False,
    "tsl_triggered": False,
    "tsl_sell_pct": 0.5,
    "tp_sell_pct": 0.9,
    "is_moonbag": False,
    "tp_partial_done": False,
    "dca_enabled": False,
    "dca_pending": False,
    "dca_bought": False,
    "dca_trigger_pct": 0.2,
    "dca_first_buy_pct": 0.5,
    "take_profit_price": None,
    "stop_loss_price": None,
    "max_hold_time": 0,
    "high_water_mark": 0.0,
    "tsl_trigger_price": None,
    "whale_wallet": "BAr5csYtpWoNpwhUjixX7ZPHXkUciFZzjBp9uNxZXJPh",
    "whale_label": "tracked_whale",
    "bonding_curve": None,
    "pool_base_vault": None,
    "pool_quote_vault": None,
    "pool_address": None,
}

async def get_token_balances(session, program_id):
    payload = {"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
               "params":[WALLET,{"programId":program_id},{"encoding":"jsonParsed"}]}
    async with session.post(CHAINSTACK, json=payload) as r:
        d = await r.json()
    result = {}
    for acc in d.get("result",{}).get("value",[]):
        info = acc["account"]["data"]["parsed"]["info"]
        amt = float(info["tokenAmount"]["uiAmount"] or 0)
        if amt > 0:
            result[info["mint"]] = amt
    return result

async def get_dexscreener_price(session, mint):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            d = await r.json()
        pairs = d.get("pairs") or []
        if pairs:
            return float(pairs[0].get("priceUsd", 0) or 0)
    except:
        pass
    return 0.0

async def main():
    positions = json.loads(POS_FILE.read_text())
    if not isinstance(positions, list):
        positions = []

    print(f"Позиций до синхронизации: {len(positions)}")

    async with aiohttp.ClientSession() as session:
        # Получить все реальные балансы (оба program)
        bal_keg = await get_token_balances(session, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        bal_2022 = await get_token_balances(session, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
        real_balances = {**bal_keg, **bal_2022}
        print(f"Токенов на кошельке: {len(real_balances)}")
        for m,a in real_balances.items():
            print(f"  {m[:20]}... = {a}")

        # Фильтруем позиции — оставляем только с реальным балансом
        live = []
        removed = []
        for p in positions:
            mint = p.get("mint","")
            if mint in real_balances:
                p["quantity"] = real_balances[mint]  # sync quantity
                p["buy_confirmed"] = True
                p["tokens_arrived"] = True
                live.append(p)
                print(f"  LIVE: {p.get('symbol',mint[:8])} qty={real_balances[mint]:.2f}")
            else:
                removed.append(p.get("symbol", mint[:8]))

        print(f"\nУдалено зомби: {len(removed)}: {removed[:10]}")

        # Проверить есть ли PUCK уже в live
        live_mints = {p["mint"] for p in live}
        if "BQJcX1P5yfiiB3yyqSQPNHMSaQw2xuGCqkXAuwXoDaAJ" not in live_mints:
            # Получить цену PUCK с DexScreener
            price = await get_dexscreener_price(
                session, "BQJcX1P5yfiiB3yyqSQPNHMSaQw2xuGCqkXAuwXoDaAJ"
            )
            if price > 0:
                # Рассчитать entry_price как cost_fallback (0.001 SOL / tokens)
                # Реальная покупка: ~0.001 SOL за 282287 tokens (из TX 11:35:18)
                PUCK["entry_price"] = 0.001 / 282287.416786
                PUCK["original_entry_price"] = PUCK["entry_price"]
                PUCK["high_water_mark"] = price
                PUCK["stop_loss_price"] = PUCK["entry_price"] * 0.8
            else:
                PUCK["entry_price"] = 0.001 / 282287.416786
                PUCK["original_entry_price"] = PUCK["entry_price"]
            live.append(PUCK)
            print(f"\nДобавлен PUCK: qty={PUCK['quantity']}, entry={PUCK['entry_price']:.12f}, dex_price={price:.8f}")
        else:
            print("PUCK уже в positions.json")

    # Бэкап и запись
    backup = POS_FILE.with_suffix(".json.bak_sync")
    backup.write_text(POS_FILE.read_text())
    POS_FILE.write_text(json.dumps(live, indent=2, default=str))
    print(f"\nГотово. Позиций после синхронизации: {len(live)}")
    print(f"Бэкап: {backup}")

asyncio.run(main())
