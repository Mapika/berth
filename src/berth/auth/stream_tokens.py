from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class _StreamTicket:
    expires_at: float
    path: str


class StreamTokenStore:
    """Small in-memory ticket store for browser EventSource auth.

    EventSource cannot attach Authorization headers. We issue a short-lived,
    single-purpose ticket over an authenticated POST, then the browser uses
    that ticket in the stream URL instead of exposing the real API key.
    """

    def __init__(self, *, ttl_s: float = 60.0) -> None:
        self._ttl_s = ttl_s
        self._tokens: dict[str, _StreamTicket] = {}
        self._lock = threading.Lock()

    def issue(self, *, path: str) -> tuple[str, float]:
        now = time.time()
        expires_at = now + self._ttl_s
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._gc(now)
            self._tokens[token] = _StreamTicket(expires_at=expires_at, path=path)
        return token, expires_at

    def validate(self, token: str, *, path: str) -> bool:
        now = time.time()
        with self._lock:
            ticket = self._tokens.pop(token, None)
            if ticket is None:
                return False
            if ticket.expires_at <= now:
                return False
            return ticket.path == path

    def _gc(self, now: float) -> None:
        expired = [
            token
            for token, ticket in self._tokens.items()
            if ticket.expires_at <= now
        ]
        for token in expired:
            self._tokens.pop(token, None)
