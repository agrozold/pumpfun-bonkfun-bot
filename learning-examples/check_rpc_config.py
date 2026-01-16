#!/usr/bin/env python3
"""
Check RPC configuration - verify which RPC endpoints are being used.

This script helps diagnose RPC configuration issues by showing:
1. What environment variables are set
2. Which RPC endpoints will be used
3. Whether Helius is properly configured

Run: uv run learning-examples/check_rpc_config.py
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"✓ Loaded .env from: {env_path}")
else:
    print(f"⚠ No .env file found at: {env_path}")
    print("  Looking for .env in current directory...")
    load_dotenv()


def mask_key(value: str | None, show_chars: int = 8) -> str:
    """Mask sensitive values, showing only first N chars."""
    if not value:
        return "NOT SET"
    if len(value) <= show_chars:
        return value
    return f"{value[:show_chars]}...{value[-4:]}"


def check_helius_in_url(url: str | None) -> bool:
    """Check if URL contains Helius."""
    if not url:
        return False
    return "helius" in url.lower()


def main():
    print("\n" + "=" * 60)
    print("RPC CONFIGURATION CHECK")
    print("=" * 60)

    # Check main RPC endpoints
    rpc_endpoint = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
    wss_endpoint = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
    helius_key = os.getenv("HELIUS_API_KEY")
    alchemy_endpoint = os.getenv("ALCHEMY_RPC_ENDPOINT")

    print("\n📡 MAIN RPC ENDPOINTS (used by bots):")
    print("-" * 40)

    if rpc_endpoint:
        is_helius = check_helius_in_url(rpc_endpoint)
        status = "✓ HELIUS" if is_helius else "⚠ NOT HELIUS"
        print(f"  SOLANA_NODE_RPC_ENDPOINT: {status}")
        print(f"    URL: {mask_key(rpc_endpoint, 40)}")
    else:
        print("  SOLANA_NODE_RPC_ENDPOINT: ❌ NOT SET")

    if wss_endpoint:
        is_helius = check_helius_in_url(wss_endpoint)
        status = "✓ HELIUS" if is_helius else "⚠ NOT HELIUS"
        print(f"  SOLANA_NODE_WSS_ENDPOINT: {status}")
        print(f"    URL: {mask_key(wss_endpoint, 40)}")
    else:
        print("  SOLANA_NODE_WSS_ENDPOINT: ❌ NOT SET")

    print("\n🔑 API KEYS:")
    print("-" * 40)
    print(f"  HELIUS_API_KEY: {mask_key(helius_key)}")

    if alchemy_endpoint:
        print(f"  ALCHEMY_RPC_ENDPOINT: {mask_key(alchemy_endpoint, 40)}")
    else:
        print("  ALCHEMY_RPC_ENDPOINT: NOT SET (optional)")

    # Analysis
    print("\n📊 ANALYSIS:")
    print("-" * 40)

    issues = []
    recommendations = []

    if not rpc_endpoint:
        issues.append("SOLANA_NODE_RPC_ENDPOINT is not set!")
        recommendations.append(
            "Set SOLANA_NODE_RPC_ENDPOINT to your Helius RPC URL"
        )
    elif not check_helius_in_url(rpc_endpoint):
        issues.append("SOLANA_NODE_RPC_ENDPOINT is NOT using Helius!")
        recommendations.append(
            "Change SOLANA_NODE_RPC_ENDPOINT to: "
            "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
        )

    if not helius_key:
        issues.append("HELIUS_API_KEY is not set!")
        recommendations.append("Get a free API key from https://helius.dev")
    elif rpc_endpoint and helius_key not in rpc_endpoint:
        issues.append("HELIUS_API_KEY doesn't match the key in RPC URL!")
        recommendations.append(
            "Make sure the same Helius API key is used in both "
            "HELIUS_API_KEY and SOLANA_NODE_RPC_ENDPOINT"
        )

    if issues:
        print("\n❌ ISSUES FOUND:")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")

        print("\n💡 RECOMMENDATIONS:")
        for i, rec in enumerate(recommendations, 1):
            print(f"  {i}. {rec}")
    else:
        print("  ✓ Configuration looks correct!")
        print("  ✓ Helius is being used as the main RPC")

    # Show expected .env format
    print("\n📝 EXPECTED .env FORMAT:")
    print("-" * 40)
    print("""
SOLANA_NODE_RPC_ENDPOINT=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
SOLANA_NODE_WSS_ENDPOINT=wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY
HELIUS_API_KEY=YOUR_KEY
""")

    print("=" * 60)


if __name__ == "__main__":
    main()
