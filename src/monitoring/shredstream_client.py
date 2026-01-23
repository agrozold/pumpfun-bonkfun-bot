"""
Shredstream/Geyser gRPC Client для ultra-low latency данных.
"""

import asyncio
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from utils.logger import get_logger

logger = get_logger(__name__)

try:
    import grpc
    from grpc import aio as grpc_aio
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

class ShredstreamProvider(Enum):
    HELIUS = "helius"
    TRITON = "triton"
    JITO = "jito"

@dataclass
class ShredData:
    slot: int
    signature: str | None
    data: bytes
    timestamp: float
    is_transaction: bool = False

class ShredstreamClient:
    def __init__(self, endpoint: str, api_token: str, provider: ShredstreamProvider = ShredstreamProvider.HELIUS, auth_type: str = "x-token"):
        self.endpoint = endpoint
        self.api_token = api_token
        self.provider = provider
        self.auth_type = auth_type
        self._channel = None
        self._running = False
        self._callbacks = []
        self._reconnect_delay = 1.0
        self._messages_received = 0
        self._last_message_time = 0.0

    def add_callback(self, callback: Callable[[ShredData], Any]) -> None:
        self._callbacks.append(callback)

    async def connect(self) -> bool:
        if not GRPC_AVAILABLE:
            return False
        try:
            if self.auth_type == "x-token":
                call_creds = grpc.metadata_call_credentials(lambda ctx, cb: cb([("x-token", self.api_token)], None))
            elif self.auth_type == "bearer":
                call_creds = grpc.access_token_call_credentials(self.api_token)
            else:
                call_creds = grpc.metadata_call_credentials(lambda ctx, cb: cb([("authorization", f"Basic {self.api_token}")], None))
            channel_creds = grpc.composite_channel_credentials(grpc.ssl_channel_credentials(), call_creds)
            self._channel = grpc_aio.secure_channel(self.endpoint, channel_creds, options=[("grpc.keepalive_time_ms", 10000), ("grpc.keepalive_timeout_ms", 5000), ("grpc.keepalive_permit_without_calls", True), ("grpc.max_receive_message_length", 50 * 1024 * 1024)])
            self._reconnect_delay = 1.0
            logger.info(f"[SHREDSTREAM] Connected to {self.provider.value}: {self.endpoint}")
            return True
        except Exception as e:
            logger.error(f"[SHREDSTREAM] Connection failed: {e}")
            return False

    async def subscribe_transactions(self, program_ids: list[str] | None = None) -> None:
        if not self._channel and not await self.connect():
            return
        self._running = True
        while self._running:
            try:
                from geyser.generated import geyser_pb2, geyser_pb2_grpc
                stub = geyser_pb2_grpc.GeyserStub(self._channel)
                request = geyser_pb2.SubscribeRequest()
                if program_ids:
                    for i, pid in enumerate(program_ids):
                        request.transactions[f"prog_{i}"].account_include.append(pid)
                        request.transactions[f"prog_{i}"].failed = False
                else:
                    request.transactions["all"].failed = False
                request.commitment = geyser_pb2.CommitmentLevel.PROCESSED
                logger.info(f"[SHREDSTREAM] Subscribing (programs: {program_ids or 'ALL'})")
                stream = stub.Subscribe(iter([request]))
                async for response in stream:
                    self._last_message_time = time.time()
                    self._messages_received += 1
                    if response.HasField("transaction"):
                        tx = response.transaction
                        sig = tx.transaction.signatures[0].hex() if tx.transaction.signatures else None
                        shred = ShredData(slot=tx.slot, signature=sig, data=tx.transaction.SerializeToString(), timestamp=time.time(), is_transaction=True)
                        for cb in self._callbacks:
                            try:
                                if asyncio.iscoroutinefunction(cb):
                                    await cb(shred)
                                else:
                                    cb(shred)
                            except Exception as e:
                                logger.error(f"[SHREDSTREAM] Callback error: {e}")
            except Exception as e:
                logger.error(f"[SHREDSTREAM] Error: {e}")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)

    async def close(self) -> None:
        self._running = False
        if self._channel:
            await self._channel.close()

    def get_stats(self) -> dict:
        return {"provider": self.provider.value, "messages_received": self._messages_received}

async def create_shredstream_client() -> ShredstreamClient | None:
    if os.getenv("USE_SHREDSTREAM", "false").lower() != "true":
        return None
    endpoint = os.getenv("SHREDSTREAM_ENDPOINT")
    token = os.getenv("SHREDSTREAM_TOKEN")
    if not endpoint or not token:
        return None
    provider_str = os.getenv("SHREDSTREAM_PROVIDER", "helius").lower()
    try:
        provider = ShredstreamProvider(provider_str)
    except ValueError:
        provider = ShredstreamProvider.HELIUS
    client = ShredstreamClient(endpoint=endpoint, api_token=token, provider=provider, auth_type=os.getenv("SHREDSTREAM_AUTH_TYPE", "x-token"))
    return client if await client.connect() else None
