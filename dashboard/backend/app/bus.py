"""In-process pub/sub for the live SSE stream. One queue per connected browser tab."""

import asyncio
import json


class Bus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict) -> None:
        data = json.dumps(message, default=str)
        for q in list(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow client; it will catch up from the REST API


bus = Bus()
