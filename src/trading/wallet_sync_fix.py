"""
Функция для получения реальной цены покупки токена из истории транзакций.
"""

import aiohttp
import asyncio
import os
from datetime import datetime


async def get_real_entry_price(mint: str, wallet: str, current_price: float) -> tuple[float, str]:
    """
    Получить реальную цену покупки токена из истории транзакций.
    
    Returns:
        tuple[float, str]: (entry_price, source)
        source: "helius", "solscan", "purchase_history", "current" (fallback)
    """
    
    # 1. Сначала проверяем purchase_history (самый надёжный источник)
    try:
        from trading.purchase_history import load_purchase_history_full
        history = load_purchase_history_full()
        if mint in history:
            price = history[mint].get("price")
            if price and price > 0:
                print(f"    [PRICE] Found in purchase_history: {price:.10f}")
                return price, "purchase_history"
    except Exception as e:
        print(f"    [PRICE] purchase_history error: {e}")
    
    # 2. Пробуем Helius API (parsed transactions)
    helius_key = os.getenv("HELIUS_API_KEY")
    if helius_key:
        try:
            price = await _get_entry_price_helius(mint, wallet, helius_key)
            if price and price > 0:
                print(f"    [PRICE] Found via Helius: {price:.10f}")
                return price, "helius"
        except Exception as e:
            print(f"    [PRICE] Helius error: {e}")
    
    # 3. Пробуем Solscan API
    try:
        price = await _get_entry_price_solscan(mint, wallet)
        if price and price > 0:
            print(f"    [PRICE] Found via Solscan: {price:.10f}")
            return price, "solscan"
    except Exception as e:
        print(f"    [PRICE] Solscan error: {e}")
    
    # 4. Fallback - используем текущую цену (НЕ ИДЕАЛЬНО!)
    print(f"    [PRICE] WARNING: Using current price as fallback: {current_price:.10f}")
    return current_price, "current_fallback"


async def _get_entry_price_helius(mint: str, wallet: str, api_key: str) -> float | None:
    """Получить цену покупки через Helius parsed transactions."""
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
    params = {
        "api-key": api_key,
        "type": "SWAP",
        "limit": 100,
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=30) as resp:
            if resp.status != 200:
                return None
            txs = await resp.json()
    
    # Ищем транзакцию покупки этого токена
    for tx in txs:
        try:
            # Helius возвращает parsed swap данные
            token_transfers = tx.get("tokenTransfers", [])
            native_transfers = tx.get("nativeTransfers", [])
            
            for transfer in token_transfers:
                if transfer.get("mint") == mint and transfer.get("toUserAccount") == wallet:
                    # Это покупка - получили токены
                    token_amount = float(transfer.get("tokenAmount", 0))
                    
                    # Ищем сколько SOL потратили
                    sol_spent = 0
                    for nt in native_transfers:
                        if nt.get("fromUserAccount") == wallet:
                            sol_spent += float(nt.get("amount", 0)) / 1e9
                    
                    if token_amount > 0 and sol_spent > 0:
                        entry_price = sol_spent / token_amount
                        return entry_price
        except Exception:
            continue
    
    return None


async def _get_entry_price_solscan(mint: str, wallet: str) -> float | None:
    """Получить цену покупки через Solscan API."""
    # Solscan token transfers API
    url = f"https://api.solscan.io/account/token/txs"
    params = {
        "address": wallet,
        "token_address": mint,
        "offset": 0,
        "limit": 20,
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, headers=headers, timeout=15) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    
    txs = data.get("data", [])
    
    # Ищем первую транзакцию где мы ПОЛУЧИЛИ токены (покупка)
    for tx in txs:
        try:
            # change_type: "inc" = получили токены
            if tx.get("change_type") == "inc":
                token_amount = float(tx.get("change_amount", 0))
                # К сожалению Solscan не даёт SOL amount напрямую
                # Нужно смотреть детали транзакции
                
                # Пробуем получить детали транзакции
                sig = tx.get("signature") or tx.get("txHash")
                if sig:
                    price = await _get_swap_price_from_tx(sig, mint, wallet)
                    if price:
                        return price
        except Exception:
            continue
    
    return None


async def _get_swap_price_from_tx(signature: str, mint: str, wallet: str) -> float | None:
    """Получить цену свопа из конкретной транзакции."""
    # Используем публичный RPC для получения транзакции
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com")
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
        ]
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(rpc, json=payload, timeout=15) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    
    result = data.get("result")
    if not result:
        return None
    
    meta = result.get("meta", {})
    
    # Анализируем pre/post balances для SOL
    pre_sol = 0
    post_sol = 0
    account_keys = result.get("transaction", {}).get("message", {}).get("accountKeys", [])
    
    for i, key in enumerate(account_keys):
        pubkey = key.get("pubkey") if isinstance(key, dict) else key
        if pubkey == wallet:
            pre_sol = meta.get("preBalances", [])[i] if i < len(meta.get("preBalances", [])) else 0
            post_sol = meta.get("postBalances", [])[i] if i < len(meta.get("postBalances", [])) else 0
            break
    
    sol_spent = (pre_sol - post_sol) / 1e9
    if sol_spent <= 0:
        return None
    
    # Анализируем token balances
    pre_token = 0
    post_token = 0
    
    for bal in meta.get("preTokenBalances", []):
        if bal.get("mint") == mint and bal.get("owner") == wallet:
            pre_token = float(bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
    
    for bal in meta.get("postTokenBalances", []):
        if bal.get("mint") == mint and bal.get("owner") == wallet:
            post_token = float(bal.get("uiTokenAmount", {}).get("uiAmount") or 0)
    
    tokens_received = post_token - pre_token
    if tokens_received <= 0:
        return None
    
    entry_price = sol_spent / tokens_received
    return entry_price
