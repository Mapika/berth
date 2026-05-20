from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from berth.daemon.dispatch_errors import classify_pre_first_byte

log = logging.getLogger(__name__)

T = TypeVar("T")


async def dispatch_with_retry(
    *,
    ranked: list,
    open_stream: Callable[..., Awaitable[T]],
    budget: int = 2,
    **open_stream_kwargs: Any,
) -> T:
    """Walk `ranked` calling `open_stream(deployment, **kwargs)`,
    retrying on retryable pre-first-byte errors until either:
      - one succeeds (returned),
      - the budget is exhausted (`budget` retries → `budget + 1` attempts max),
      - all distinct nodes have been tried,
      - a non-retryable error is hit (re-raised immediately).

    Each node is tried at most once per call — multiple candidate
    deployments on the same node don't burn extra attempts.
    """
    if not ranked:
        raise RuntimeError("dispatch_with_retry: no candidates")

    attempts = 0
    tried_nodes: set[int] = set()
    last_err: BaseException | None = None
    max_attempts = budget + 1

    for deployment in ranked:
        if deployment.node_id in tried_nodes:
            continue
        if attempts >= max_attempts:
            break
        tried_nodes.add(deployment.node_id)
        attempts += 1
        try:
            return await open_stream(deployment, **open_stream_kwargs)
        except BaseException as exc:
            classified = classify_pre_first_byte(exc)
            if classified is None:
                raise
            log.warning(
                "dispatch_retry node=%s reason=%s attempt=%d",
                deployment.node_id, classified.reason, attempts,
            )
            last_err = exc
            continue

    if last_err is None:
        raise RuntimeError("dispatch_with_retry exhausted candidates without an error")
    raise last_err
