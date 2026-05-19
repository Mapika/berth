from __future__ import annotations

import secrets
import threading
import time


class StreamTokenStore:
    """Small in-memory ticket store for browser EventSource auth.

    EventSource cannot attach Authorization headers. We issue a short-lived,
    single-purpose ticket over an authenticated POST, then the browser uses
    that ticket in the stream URL instead of exposing the real API key.
    """

    def __init__(self, *, ttl_s: float = 60.0) -> None:
        self._ttl_s = ttl_s
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> tuple[str, float]:
        now = time.time()
        expires_at = now + self._ttl_s
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._gc(now)
            self._tokens[token] = expires_at
        return token, expires_at

    def validate(self, token: str) -> bool:
        now = time.time()
        with self._lock:
            expires_at = self._tokens.get(token)
            if expires_at is None:
                return False
            if expires_at <= now:
                self._tokens.pop(token, None)
                return False
            return True

    def _gc(self, now: float) -> None:
        expired = [token for token, expires_at in self._tokens.items() if expires_at <= now]
        for token in expired:
            self._tokens.pop(token, None)
