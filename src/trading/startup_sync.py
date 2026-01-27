"""Startup sync - вызывается при старте бота."""
import asyncio
from trading.wallet_sync import sync_wallet

async def run_startup_sync():
    """Запустить синхронизацию кошелька."""
    try:
        await sync_wallet()
    except Exception as e:
        print(f"[STARTUP SYNC] Error: {e}")

if __name__ == "__main__":
    asyncio.run(run_startup_sync())
