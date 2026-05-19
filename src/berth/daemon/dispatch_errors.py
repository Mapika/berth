from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class RetryableError:
    """Marker for errors a dispatch-retry-budget can swallow."""
    reason: str
    underlying: BaseException | None = None


class NodeUnreachableError(Exception):
    """Raised by the dispatch layer when the chosen node's AgentLink
    isn't ready (heartbeat stale, agent disconnected) at dispatch time.
    Carries the node id for logging and audit."""
    def __init__(self, node_id: int) -> None:
        super().__init__(f"node {node_id} unreachable")
        self.node_id = node_id


class UpstreamHttpError(Exception):
    """Raised by the dispatch layer when the upstream engine returns a
    retryable 5xx status before any body byte has been sent to the
    client. Carries the status so dispatch_with_retry can fall through
    to the next candidate."""
    def __init__(self, status: int, body_preview: bytes = b"") -> None:
        super().__init__(f"upstream returned HTTP {status}")
        self.status = status
        self.body_preview = body_preview


def classify_pre_first_byte(exc: BaseException) -> RetryableError | None:
    """Classify an exception raised before any byte was sent to the
    client. Returns RetryableError when the next candidate has a
    reasonable chance of succeeding, None otherwise."""
    if isinstance(exc, NodeUnreachableError):
        return RetryableError(reason="node_unreachable", underlying=exc)
    if isinstance(exc, UpstreamHttpError):
        if classify_pre_first_byte_status(exc.status) is not None:
            return RetryableError(reason="upstream_5xx", underlying=exc)
        return None
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return RetryableError(reason="connect", underlying=exc)
    if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return RetryableError(reason="timeout", underlying=exc)
    if isinstance(exc, httpx.RemoteProtocolError):
        return RetryableError(reason="remote_protocol", underlying=exc)
    return None


_RETRYABLE_STATUS = {502, 503, 504}


def classify_pre_first_byte_status(status: int) -> RetryableError | None:
    """Pre-first-byte status-code classification. Only 502/503/504 are
    retryable — a 500 from the engine typically means a model crash and
    retrying just adds load."""
    if status in _RETRYABLE_STATUS:
        return RetryableError(reason="upstream_5xx")
    return None
