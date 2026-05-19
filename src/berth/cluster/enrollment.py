from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Entry:
    label: str
    expires_at: float


class EnrollmentTokens:
    """In-memory single-use enrollment token store.

    Tokens are minted by the leader (e.g. via `berth nodes enroll`) and
    consumed once by an agent during `berth agent register`. Successful
    consumption returns the label the token was bound to and discards the
    token. Tokens expire after `ttl_seconds`.
    """

    def __init__(
        self, *,
        ttl_seconds: int = 600,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._tokens: dict[str, _Entry] = {}

    def mint(self, *, label: str) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = _Entry(
            label=label, expires_at=self._now() + self._ttl,
        )
        return token

    def consume(self, token: str) -> str | None:
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        if entry.expires_at < self._now():
            return None
        return entry.label
