#!/usr/bin/env python3
"""
Helius Webhook Server - принимает real-time уведомления о свопах китов
"""

import json
import asyncio
import logging
from datetime import datetime
from aiohttp import web

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Очередь для передачи сигналов боту
swap_queue = asyncio.Queue()

# Auth токен (опционально, для первого webhook)
AUTH_TOKEN = "my_secret_bot_token"


async def handle_webhook(request: web.Request) -> web.Response:
    """Обработчик входящих webhook от Helius"""
    try:
        # Проверка auth header (опционально)
        # auth = request.headers.get('Authorization')
        # if auth and auth != AUTH_TOKEN:
        #     logger.warning(f"Invalid auth header: {auth}")
        #     return web.Response(status=401)
        
        data = await request.json()
        
        # Helius отправляет массив транзакций
        if isinstance(data, list):
            transactions = data
        else:
            transactions = [data]
        
        for tx in transactions:
            await process_swap(tx)
        
        # Важно: вернуть 200 чтобы Helius не ретраил
        return web.Response(status=200, text="OK")
        
    except json.JSONDecodeError:
        logger.error("Invalid JSON received")
        return web.Response(status=400, text="Invalid JSON")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text=str(e))


async def process_swap(tx: dict):
    """Обработка SWAP транзакции от кита"""
    try:
        tx_type = tx.get('type', 'UNKNOWN')
        signature = tx.get('signature', 'no-sig')[:20]
        source = tx.get('source', 'unknown')
        timestamp = tx.get('timestamp', 0)
        
        # Получаем информацию о свопе
        description = tx.get('description', '')
        fee_payer = tx.get('feePayer', '')
        
        # Native/Token transfers
        native_transfers = tx.get('nativeTransfers', [])
        token_transfers = tx.get('tokenTransfers', [])
        
        # Логируем
        time_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S') if timestamp else 'N/A'
        
        logger.info(f"{'='*60}")
        logger.info(f"🐋 WHALE SWAP DETECTED!")
        logger.info(f"   Time: {time_str}")
        logger.info(f"   Sig: {signature}...")
        logger.info(f"   Source: {source}")
        logger.info(f"   Whale: {fee_payer[:20]}..." if fee_payer else "   Whale: unknown")
        logger.info(f"   Desc: {description[:100]}..." if description else "   Desc: N/A")
        
        # Token transfers - здесь самое интересное
        if token_transfers:
            for tt in token_transfers[:3]:  # Первые 3
                mint = tt.get('mint', '')
                amount = tt.get('tokenAmount', 0)
                logger.info(f"   Token: {mint[:20]}... Amount: {amount}")
        
        # Кладём в очередь для бота
        await swap_queue.put({
            'signature': tx.get('signature'),
            'whale': fee_payer,
            'token_transfers': token_transfers,
            'native_transfers': native_transfers,
            'timestamp': timestamp,
            'source': source,
            'raw': tx
        })
        
    except Exception as e:
        logger.error(f"Process swap error: {e}")


async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint"""
    return web.Response(text="OK", status=200)


async def start_server(host='0.0.0.0', port=8000):
    """Запуск webhook сервера"""
    app = web.Application()
    app.router.add_post('/webhook', handle_webhook)
    app.router.add_get('/health', health_check)
    app.router.add_get('/', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    
    logger.info(f"🚀 Webhook server started on http://{host}:{port}")
    logger.info(f"   Endpoint: POST /webhook")
    logger.info(f"   Waiting for whale swaps...")
    
    # Держим сервер запущенным
    while True:
        await asyncio.sleep(3600)


if __name__ == '__main__':
    print("=" * 60)
    print("  HELIUS WEBHOOK SERVER")
    print("  Listening for whale SWAP transactions")
    print("=" * 60)
    
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        print("\n👋 Server stopped")
