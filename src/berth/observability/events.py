from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class Event:
    kind: str
    payload: dict
    ts: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


class EventBus:
    """Tiny asyncio fanout. Each subscriber gets its own queue; backpressure on
    a slow subscriber doesn't block publishing to others (we drop on full)."""

    def __init__(self, *, per_subscriber_buffer: int = 256):
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._buf = per_subscriber_buffer

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @contextlib.asynccontextmanager
    async def subscribe(self):
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._buf)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    async def publish(self, event: Event) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop on slow consumer; logs/metrics will reflect the lag.
                pass
