"""
Bot-specific RPC Configuration
Распределение нагрузки с учётом реальных лимитов

ВАЖНО:
- Helius: ТОЛЬКО HTTP (нет WSS), 0.08 req/s
- Chainstack: HTTP + WSS (primary), 0.12 req/s  
- dRPC: HTTP + WSS (fallback), 0.15 req/s
- Alchemy: только HTTP, 0.05 req/s
- Public: HTTP + WSS (last resort), 0.02 req/s
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import Enum


class BotType(Enum):
    PUMP = "pump"
    BONK = "bonk"
    BAGS = "bags"
    WHALE = "whale"
    COPY = "copy"
    VOLUME = "volume"


class RPCProvider(Enum):
    HELIUS = "helius"          # HTTP only, 0.08 req/s
    CHAINSTACK = "chainstack"  # HTTP + WSS, 0.12 req/s
    DRPC = "drpc"              # HTTP + WSS, 0.15 req/s
    ALCHEMY = "alchemy"        # HTTP only, 0.05 req/s
    PUBLIC = "public_solana"   # HTTP + WSS, 0.02 req/s


# Провайдеры с WSS поддержкой
WSS_PROVIDERS = [RPCProvider.CHAINSTACK, RPCProvider.DRPC, RPCProvider.PUBLIC]

# Провайдеры только HTTP
HTTP_ONLY_PROVIDERS = [RPCProvider.HELIUS, RPCProvider.ALCHEMY]


@dataclass  
class BotRPCProfile:
    bot_type: BotType
    primary_http: List[RPCProvider]   # Для HTTP запросов
    primary_wss: List[RPCProvider]    # Для WebSocket (только с WSS!)
    max_requests_per_minute: int
    cache_ttl_seconds: int
    description: str


BOT_RPC_PROFILES: Dict[BotType, BotRPCProfile] = {
    # PUMP - критичен для скорости, Helius первый для HTTP
    BotType.PUMP: BotRPCProfile(
        bot_type=BotType.PUMP,
        primary_http=[RPCProvider.HELIUS, RPCProvider.CHAINSTACK, RPCProvider.DRPC],
        primary_wss=[RPCProvider.CHAINSTACK, RPCProvider.DRPC, RPCProvider.PUBLIC],  # БЕЗ Helius!
        max_requests_per_minute=120,
        cache_ttl_seconds=3,
        description="Pump.fun sniper - Helius HTTP, Chainstack WSS",
    ),
    
    # BONK - Chainstack первый (хороший для Raydium)
    BotType.BONK: BotRPCProfile(
        bot_type=BotType.BONK,
        primary_http=[RPCProvider.CHAINSTACK, RPCProvider.HELIUS, RPCProvider.DRPC],
        primary_wss=[RPCProvider.CHAINSTACK, RPCProvider.DRPC, RPCProvider.PUBLIC],
        max_requests_per_minute=100,
        cache_ttl_seconds=3,
        description="Bonk.fun sniper - Chainstack primary",
    ),
    
    # BAGS - баланс между провайдерами
    BotType.BAGS: BotRPCProfile(
        bot_type=BotType.BAGS,
        primary_http=[RPCProvider.CHAINSTACK, RPCProvider.DRPC, RPCProvider.HELIUS],
        primary_wss=[RPCProvider.CHAINSTACK, RPCProvider.DRPC, RPCProvider.PUBLIC],
        max_requests_per_minute=80,
        cache_ttl_seconds=5,
        description="Bags.fm sniper - balanced",
    ),
    
    # WHALE - менее критичен, экономим лимиты основных
    BotType.WHALE: BotRPCProfile(
        bot_type=BotType.WHALE,
        primary_http=[RPCProvider.DRPC, RPCProvider.ALCHEMY, RPCProvider.CHAINSTACK],
        primary_wss=[RPCProvider.DRPC, RPCProvider.CHAINSTACK, RPCProvider.PUBLIC],
        max_requests_per_minute=60,
        cache_ttl_seconds=10,
        description="Whale tracker - uses dRPC/Alchemy to save limits",
    ),
    
    # COPY - средний приоритет
    BotType.COPY: BotRPCProfile(
        bot_type=BotType.COPY,
        primary_http=[RPCProvider.DRPC, RPCProvider.CHAINSTACK, RPCProvider.ALCHEMY],
        primary_wss=[RPCProvider.DRPC, RPCProvider.CHAINSTACK, RPCProvider.PUBLIC],
        max_requests_per_minute=80,
        cache_ttl_seconds=5,
        description="Copy trading - dRPC primary",
    ),
    
    # VOLUME - средний приоритет
    BotType.VOLUME: BotRPCProfile(
        bot_type=BotType.VOLUME,
        primary_http=[RPCProvider.CHAINSTACK, RPCProvider.DRPC, RPCProvider.HELIUS],
        primary_wss=[RPCProvider.CHAINSTACK, RPCProvider.DRPC, RPCProvider.PUBLIC],
        max_requests_per_minute=60,
        cache_ttl_seconds=5,
        description="Volume sniper",
    ),
}


def get_bot_type_from_name(bot_name: str) -> Optional[BotType]:
    name_lower = bot_name.lower()
    for bt in BotType:
        if bt.value in name_lower:
            return bt
    return None


def get_bot_rpc_profile(bot_name: str) -> BotRPCProfile:
    bot_type = get_bot_type_from_name(bot_name)
    if bot_type and bot_type in BOT_RPC_PROFILES:
        return BOT_RPC_PROFILES[bot_type]
    return BOT_RPC_PROFILES[BotType.PUMP]


def print_profiles():
    print("\n" + "=" * 65)
    print("BOT RPC PROFILES (Helius=HTTP only, Chainstack/dRPC=HTTP+WSS)")
    print("=" * 65)
    for bt, p in BOT_RPC_PROFILES.items():
        print(f"\n{bt.value.upper()}: {p.description}")
        print(f"  HTTP: {' -> '.join(pr.value for pr in p.primary_http)}")
        print(f"  WSS:  {' -> '.join(pr.value for pr in p.primary_wss)}")
        print(f"  Rate: {p.max_requests_per_minute} req/min, Cache: {p.cache_ttl_seconds}s")
    print("\n" + "=" * 65)


if __name__ == "__main__":
    print_profiles()
