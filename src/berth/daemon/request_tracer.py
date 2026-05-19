"""In-memory tracing for proxied OpenAI requests.

Captures per-request timing checkpoints and route-resolution metadata so the
admin UI can render a live request inspector and per-request waterfalls. No
persistence: everything is a deque(maxlen=N) plus a fanout to subscribed SSE
clients. If the daemon dies the traces die with it; that's intentional —
detailed per-request audit belongs in usage_events, not here.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

_MAX_TRACES = 256
_MAX_SUBSCRIBERS = 32


@dataclass
class RequestTrace:
    request_id: str
    method: str
    path: str
    model_requested: str | None = None
    api_key_id: int | None = None
    api_key_name: str | None = None
    # Monotonic timestamps (seconds since arbitrary epoch). The deltas are
    # what matter; absolute values are only useful to compute the waterfall.
    arrived_at: float = 0.0
    route_resolved_at: float | None = None
    dispatched_at: float | None = None
    first_byte_at: float | None = None
    completed_at: float | None = None
    # Route resolution outcome.
    route_name: str | None = None
    profile_name: str | None = None
    target_model: str | None = None
    deployment_id: int | None = None
    backend: str | None = None
    cold_loaded: bool = False
    # Result.
    status_code: int | None = None
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Subscriber:
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)


class RequestTracer:
    """Ring buffer of recent traces + async fanout to SSE subscribers.

    Thread-safety: traces are only mutated from the FastAPI event loop, so no
    lock needed. The deque's append/popleft are atomic.
    """

    def __init__(self, capacity: int = _MAX_TRACES) -> None:
        self._buffer: deque[RequestTrace] = deque(maxlen=capacity)
        self._subscribers: list[_Subscriber] = []

    def start(self, *, method: str, path: str) -> RequestTrace:
        trace = RequestTrace(
            request_id=uuid.uuid4().hex[:12],
            method=method,
            path=path,
            arrived_at=time.monotonic(),
        )
        self._buffer.append(trace)
        self._publish(trace, "started")
        return trace

    def update(self, trace: RequestTrace, **fields: Any) -> None:
        for k, v in fields.items():
            setattr(trace, k, v)
        self._publish(trace, "updated")

    def finalize(self, trace: RequestTrace, **fields: Any) -> None:
        trace.completed_at = time.monotonic()
        for k, v in fields.items():
            setattr(trace, k, v)
        self._publish(trace, "completed")

    def snapshot(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._buffer]

    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        if len(self._subscribers) >= _MAX_SUBSCRIBERS:
            # Drop the oldest subscriber to make room. Each SSE client is
            # cheap but unbounded growth would let a leaking client wedge us.
            old = self._subscribers.pop(0)
            try:
                old.queue.put_nowait({"event": "evicted"})
            except asyncio.QueueFull:
                pass
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        try:
            self._subscribers.remove(sub)
        except ValueError:
            pass

    def _publish(self, trace: RequestTrace, event: str) -> None:
        msg = {"event": event, "trace": trace.to_dict()}
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                # Subscriber is too slow; drop them rather than block the
                # proxy hot path.
                self.unsubscribe(sub)
