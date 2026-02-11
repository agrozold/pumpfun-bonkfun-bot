#!/usr/bin/env python3
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

async def main():
    from solana.rpc.async_api import AsyncClient
    from solders.pubkey import Pubkey
    from solders.keypair import Keypair
    import base58
    
    pk = os.getenv("SOLANA_PRIVATE_KEY")
    rpc = os.getenv("SOLANA_NODE_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com")
    
    kp = Keypair.from_bytes(base58.b58decode(pk))
    
    async with AsyncClient(rpc) as client:
        resp = await client.get_balance(kp.pubkey())
        sol = resp.value / 1e9
        print(f"Wallet: {kp.pubkey()}")
        print(f"Balance: {sol:.4f} SOL")

asyncio.run(main())
