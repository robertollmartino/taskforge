import asyncio
import json
from typing import Any, AsyncGenerator, List
from loguru import logger


class SSEManager:
    """Singleton SSE manager for Delta-Only payloads and Zero-Flicker UI updates."""

    def __init__(self) -> None:
        self._queues: List[asyncio.Queue] = []

    async def subscribe(self) -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.append(queue)
        try:
            # Initial handshake
            yield f"data: {json.dumps({'type': 'connected', 'status': 'ok'})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in self._queues:
                self._queues.remove(queue)

    def broadcast_delta(self, event_type: str, delta: dict[str, Any]) -> None:
        payload = {"type": event_type, "delta": delta}
        dead_queues = []
        for q in self._queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead_queues.append(q)
            except Exception as exc:
                logger.warning(f"SSE broadcast error: {exc}")
                dead_queues.append(q)
        for q in dead_queues:
            if q in self._queues:
                self._queues.remove(q)


sse_manager = SSEManager()
