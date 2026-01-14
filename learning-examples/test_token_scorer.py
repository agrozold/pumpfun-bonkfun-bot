#!/usr/bin/env python3
"""
Test token scorer with real tokens.
"""

import asyncio
import sys
sys.path.insert(0, "src")

from monitoring.token_scorer import TokenScorer


TOKENS = [
    # Recent pumps
    ("jk1T35eWK41MBMM8AWoYVaNbjHEEQzMDetTsfnqpump", "SOL Trophy"),
    # Old successful ones
    ("61V8vBaqAGMpgDQi4JcAwo1dmBGHsyhzodcPqnEVpump", "ARC"),
    ("DKu9kykSfbN5LBfFXtNNDPaX35o4Fv6vJ9FKk7pZpump", "AVA"),
    # Cabal runners
    ("CSrwNk6B1DwWCHRMsaoDVUfD5bBMQCJPY72ZG3Nnpump", "Franklin"),
]


async def main():
    print("=" * 60)
    print("TOKEN SCORER TEST")
    print("=" * 60)
    
    scorer = TokenScorer(min_score=70)
    
    for mint, name in TOKENS:
        print(f"\n📊 Scoring {name}...")
        
        should_buy, score = await scorer.should_buy(mint, name)
        
        print(f"   Symbol: {score.symbol}")
        print(f"   Total Score: {score.total_score}/100")
        print(f"   - Volume: {score.volume_score}")
        print(f"   - Buy Pressure: {score.buy_pressure_score}")
        print(f"   - Momentum: {score.momentum_score}")
        print(f"   - Liquidity: {score.liquidity_score}")
        print(f"   Recommendation: {score.recommendation}")
        print(f"   Should Buy: {'✅ YES' if should_buy else '❌ NO'}")
        
        if score.details:
            print(f"   Details:")
            print(f"     Price: ${score.details.get('price_usd', 'N/A')}")
            print(f"     Volume 5m: ${score.details.get('volume_5m', 0):.2f}")
            print(f"     Buys/Sells 5m: {score.details.get('buys_5m', 0)}/{score.details.get('sells_5m', 0)}")
        
        print("-" * 60)
        await asyncio.sleep(0.5)
    
    await scorer.close()
    print("\n✅ Test complete!")


if __name__ == "__main__":
    asyncio.run(main())
