"""
SenderRegistry - управление провайдерами отправки
Поддержка стратегий: race, fallback, broadcast
"""

import asyncio
import logging
from typing import List, Optional, Dict
from enum import Enum
from dataclasses import dataclass

from .sender import SendProvider, SendResult, SendStatus

logger = logging.getLogger(__name__)


class SendStrategy(Enum):
    RACE = 'race'           # Первый успешный
    FALLBACK = 'fallback'   # По приоритету
    BROADCAST = 'broadcast' # Все параллельно


@dataclass
class SenderRegistry:
    """
    Реестр провайдеров отправки.
    
    Использование:
        registry = SenderRegistry()
        registry.register(HeliusSender(...))
        registry.register(JitoSender(...))
        
        result = await registry.send(tx_bytes, strategy=SendStrategy.RACE)
    """
    
    strategy: SendStrategy = SendStrategy.RACE
    
    def __post_init__(self):
        self._providers: Dict[str, SendProvider] = {}
        self._disabled: set = set()
    
    def register(self, provider: SendProvider) -> None:
        """Зарегистрировать провайдера"""
        self._providers[provider.name] = provider
        logger.info(f"Registered sender: {provider.name} (priority={provider.priority})")
    
    def disable(self, name: str) -> None:
        """Отключить провайдера"""
        self._disabled.add(name)
        logger.info(f"Disabled sender: {name}")
    
    def enable(self, name: str) -> None:
        """Включить провайдера"""
        self._disabled.discard(name)
        logger.info(f"Enabled sender: {name}")
    
    def get_active_providers(self) -> List[SendProvider]:
        """Получить активных провайдеров (отсортированных по приоритету)"""
        providers = [
            p for name, p in self._providers.items()
            if name not in self._disabled and p.is_healthy()
        ]
        return sorted(providers, key=lambda p: p.priority)
    
    async def send(
        self, 
        tx_bytes: bytes, 
        trace_id: str = None,
        strategy: SendStrategy = None
    ) -> SendResult:
        """
        Отправить транзакцию через зарегистрированных провайдеров.
        """
        strategy = strategy or self.strategy
        providers = self.get_active_providers()
        
        if not providers:
            return SendResult(
                status=SendStatus.FAILED,
                error='No active providers available'
            )
        
        if strategy == SendStrategy.RACE:
            return await self._send_race(providers, tx_bytes, trace_id)
        elif strategy == SendStrategy.FALLBACK:
            return await self._send_fallback(providers, tx_bytes, trace_id)
        elif strategy == SendStrategy.BROADCAST:
            return await self._send_broadcast(providers, tx_bytes, trace_id)
        else:
            return await self._send_fallback(providers, tx_bytes, trace_id)
    
    async def _send_race(
        self, 
        providers: List[SendProvider], 
        tx_bytes: bytes,
        trace_id: str
    ) -> SendResult:
        """Отправить через всех, вернуть первый успешный"""
        
        tasks = [
            asyncio.create_task(p.send(tx_bytes, trace_id))
            for p in providers
        ]
        
        try:
            # Ждём первый успешный
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Проверяем результат
            for task in done:
                result = task.result()
                if result.is_success:
                    # Отменяем остальные
                    for p in pending:
                        p.cancel()
                    return result
            
            # Если первый не успешный, ждём остальных
            if pending:
                done2, _ = await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)
                for task in done2:
                    try:
                        result = task.result()
                        if result.is_success:
                            return result
                    except asyncio.CancelledError:
                        pass
            
            # Все провалились - возвращаем последний результат
            for task in done:
                return task.result()
                
        except Exception as e:
            logger.error(f"Race send error: {e}")
            return SendResult(status=SendStatus.FAILED, error=str(e))
        
        return SendResult(status=SendStatus.FAILED, error='All providers failed')
    
    async def _send_fallback(
        self, 
        providers: List[SendProvider], 
        tx_bytes: bytes,
        trace_id: str
    ) -> SendResult:
        """Отправить по приоритету до первого успеха"""
        
        last_result = None
        
        for provider in providers:
            try:
                result = await provider.send(tx_bytes, trace_id)
                if result.is_success:
                    return result
                last_result = result
                logger.warning(f"Provider {provider.name} failed: {result.error}")
            except Exception as e:
                logger.error(f"Provider {provider.name} error: {e}")
                last_result = SendResult(
                    status=SendStatus.FAILED,
                    provider=provider.name,
                    error=str(e)
                )
        
        return last_result or SendResult(
            status=SendStatus.FAILED,
            error='All providers failed'
        )
    
    async def _send_broadcast(
        self, 
        providers: List[SendProvider], 
        tx_bytes: bytes,
        trace_id: str
    ) -> SendResult:
        """Отправить через всех, вернуть первый успешный"""
        
        tasks = [
            asyncio.create_task(p.send(tx_bytes, trace_id))
            for p in providers
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Ищем успешный
        for result in results:
            if isinstance(result, SendResult) and result.is_success:
                return result
        
        # Возвращаем первый результат (или ошибку)
        for result in results:
            if isinstance(result, SendResult):
                return result
            elif isinstance(result, Exception):
                return SendResult(status=SendStatus.FAILED, error=str(result))
        
        return SendResult(status=SendStatus.FAILED, error='All providers failed')
    
    async def close(self) -> None:
        """Закрыть всех провайдеров"""
        for provider in self._providers.values():
            try:
                await provider.close()
            except Exception as e:
                logger.error(f"Error closing {provider.name}: {e}")
