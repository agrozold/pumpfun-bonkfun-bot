"""
TraceRecorder - запись трейсов в JSONL файлы
Асинхронная буферизованная запись для минимального влияния на event loop
"""

import json
import asyncio
import aiofiles
from pathlib import Path
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass

from .trace_context import TraceContext


@dataclass
class TraceRecorder:
    """Рекордер трейсов с буферизацией"""
    
    output_dir: Path
    buffer_size: int = 10           # Сброс после N записей
    flush_interval: float = 5.0     # Сброс каждые N секунд
    
    _buffer: List[dict] = None
    _flush_task: Optional[asyncio.Task] = None
    _lock: asyncio.Lock = None
    
    def __post_init__(self):
        self._buffer = []
        self._lock = asyncio.Lock()
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    async def start(self) -> None:
        """Запустить фоновый flush"""
        self._flush_task = asyncio.create_task(self._flush_loop())
    
    async def stop(self) -> None:
        """Остановить и сбросить буфер"""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()
    
    async def record(self, trace: TraceContext) -> None:
        """Добавить трейс в буфер"""
        async with self._lock:
            self._buffer.append(trace.to_dict())
            if len(self._buffer) >= self.buffer_size:
                await self._flush_unlocked()
    
    async def _flush_loop(self) -> None:
        """Периодический сброс буфера"""
        while True:
            await asyncio.sleep(self.flush_interval)
            await self._flush()
    
    async def _flush(self) -> None:
        """Сбросить буфер с блокировкой"""
        async with self._lock:
            await self._flush_unlocked()
    
    async def _flush_unlocked(self) -> None:
        """Сбросить буфер (без блокировки, вызывать внутри lock)"""
        if not self._buffer:
            return
        
        # Файл по дате
        date_str = datetime.utcnow().strftime('%Y-%m-%d')
        filepath = self.output_dir / f'traces_{date_str}.jsonl'
        
        # Атомарная запись
        lines = [json.dumps(record, ensure_ascii=False) + '\n' for record in self._buffer]
        
        async with aiofiles.open(filepath, 'a') as f:
            await f.writelines(lines)
        
        self._buffer.clear()


# Глобальный рекордер
_recorder: Optional[TraceRecorder] = None


async def init_trace_recorder(output_dir: str = 'logs/traces') -> TraceRecorder:
    """Инициализировать глобальный рекордер"""
    global _recorder
    _recorder = TraceRecorder(output_dir=Path(output_dir))
    await _recorder.start()
    return _recorder


async def record_trace(trace: TraceContext) -> None:
    """Записать трейс в глобальный рекордер"""
    if _recorder:
        await _recorder.record(trace)


async def shutdown_trace_recorder() -> None:
    """Остановить рекордер"""
    if _recorder:
        await _recorder.stop()
