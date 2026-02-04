#!/usr/bin/env python3
import os
import sys
os.chdir('/opt/pumpfun-bonkfun-bot')
sys.path.insert(0, '/opt/pumpfun-bonkfun-bot/src')
from dotenv import load_dotenv
load_dotenv()
import asyncio
from trading.wallet_sync import sync_wallet
asyncio.run(sync_wallet())
