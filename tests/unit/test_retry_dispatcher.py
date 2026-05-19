from __future__ import annotations

from dataclasses import dataclass

import pytest

from serve_engine.daemon.dispatch_errors import NodeUnreachableError
from serve_engine.daemon.retry_dispatcher import dispatch_with_retry


@dataclass
class FakeDep:
    id: int
    node_id: int


@pytest.mark.asyncio
async def test_first_candidate_succeeds_no_retry():
    calls: list[int] = []

    async def open_stream(deployment):
        calls.append(deployment.id)
        return ("ok", deployment)

    result = await dispatch_with_retry(
        ranked=[FakeDep(1, 10), FakeDep(2, 11)],
        open_stream=open_stream,
        budget=2,
    )
    assert result == ("ok", FakeDep(1, 10))
    assert calls == [1]


@pytest.mark.asyncio
async def test_retries_on_node_unreachable_then_succeeds():
    calls: list[int] = []

    async def open_stream(deployment):
        calls.append(deployment.id)
        if deployment.id == 1:
            raise NodeUnreachableError(node_id=10)
        return ("ok", deployment)

    result = await dispatch_with_retry(
        ranked=[FakeDep(1, 10), FakeDep(2, 11)],
        open_stream=open_stream,
        budget=2,
    )
    assert result == ("ok", FakeDep(2, 11))
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_non_retryable_error_propagates_immediately():
    async def open_stream(deployment):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await dispatch_with_retry(
            ranked=[FakeDep(1, 10), FakeDep(2, 11)],
            open_stream=open_stream,
            budget=2,
        )


@pytest.mark.asyncio
async def test_budget_exhausted_propagates_last_error():
    async def open_stream(deployment):
        raise NodeUnreachableError(node_id=deployment.node_id)

    with pytest.raises(NodeUnreachableError):
        await dispatch_with_retry(
            ranked=[FakeDep(1, 10), FakeDep(2, 11), FakeDep(3, 12)],
            open_stream=open_stream,
            budget=2,
        )


@pytest.mark.asyncio
async def test_each_node_tried_at_most_once():
    calls: list[int] = []

    async def open_stream(deployment):
        calls.append(deployment.id)
        raise NodeUnreachableError(node_id=deployment.node_id)

    with pytest.raises(NodeUnreachableError):
        await dispatch_with_retry(
            ranked=[FakeDep(1, 10), FakeDep(2, 10), FakeDep(3, 11)],
            open_stream=open_stream,
            budget=5,
        )
    # deployment 1 (node 10), then deployment 3 (node 11). Deployment 2
    # (node 10 again) is skipped.
    assert calls == [1, 3]


@pytest.mark.asyncio
async def test_empty_ranked_raises():
    async def open_stream(_):
        return ("never", None)

    with pytest.raises(RuntimeError):
        await dispatch_with_retry(
            ranked=[], open_stream=open_stream, budget=2,
        )
