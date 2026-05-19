from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from berth.backends.base import ContainerBackend
from berth.backends.manifest import EngineManifest, Headroom
from berth.lifecycle.health_monitor import HealthMonitor
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store


class _StubBackend(ContainerBackend):
    """Minimal backend stub. We only need health_path and a name; the
    HealthMonitor never invokes build_argv/container_env/etc.
    """
    name = "stub"

    def __init__(self):
        self.manifest = EngineManifest(
            name="stub",
            image="stub",
            pinned_tag="v1",
            health_path="/health",
            openai_base="/v1",
            metrics_path="/metrics",
            internal_port=8000,
            headroom=Headroom(factor=1.5, min_extra_mb=2048, min_floor_pct=15),
        )

    def build_argv(self, plan, *, local_model_path, config_path=None):
        return []


def _make_client_factory(status_code: int):
    """Build a client factory that returns an AsyncClient backed by a
    MockTransport which answers every request with the given status_code.
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    def factory() -> httpx.AsyncClient:
        transport = httpx.MockTransport(handler)
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    return factory


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    return c


def _seed_ready_deployment(conn) -> int:
    m = model_store.add(conn, name="stub-model", hf_repo="org/stub")
    d = dep_store.create(
        conn,
        model_id=m.id,
        backend="stub",
        image_tag="stub:v1",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=4096,
        dtype="auto",
    )
    dep_store.set_container(
        conn, d.id,
        container_id="cid", container_name="cname",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, d.id, "ready")
    return d.id


@pytest.mark.asyncio
async def test_health_monitor_marks_failed_after_k_failures(conn):
    """503 responses for max_consecutive_failures consecutive ticks must
    transition the row from 'ready' to 'failed' with the expected
    last_error and emit deployment.unhealthy on the event bus.
    """
    dep_id = _seed_ready_deployment(conn)

    manager = MagicMock()
    manager._emit = AsyncMock()

    monitor = HealthMonitor(
        conn=conn,
        backends={"stub": _StubBackend()},
        manager=manager,
        interval_s=0.01,
        max_consecutive_failures=2,
        client_factory=_make_client_factory(503),
    )

    # Two ticks: first increments to 1, second hits threshold (2) -> failed.
    await monitor.tick_once()
    refreshed = dep_store.get_by_id(conn, dep_id)
    assert refreshed.status == "ready"
    manager._emit.assert_not_called()

    await monitor.tick_once()
    refreshed = dep_store.get_by_id(conn, dep_id)
    assert refreshed.status == "failed"
    assert refreshed.last_error == "health probe failed 2 times"

    manager._emit.assert_awaited_once_with("deployment.unhealthy", dep_id=dep_id)


@pytest.mark.asyncio
async def test_health_monitor_keeps_ready_when_healthy(conn):
    """200 responses leave the row in 'ready' and never emit unhealthy."""
    dep_id = _seed_ready_deployment(conn)

    manager = MagicMock()
    manager._emit = AsyncMock()

    monitor = HealthMonitor(
        conn=conn,
        backends={"stub": _StubBackend()},
        manager=manager,
        interval_s=0.01,
        max_consecutive_failures=2,
        client_factory=_make_client_factory(200),
    )

    for _ in range(3):
        await monitor.tick_once()

    refreshed = dep_store.get_by_id(conn, dep_id)
    assert refreshed.status == "ready"
    assert refreshed.last_error is None
    manager._emit.assert_not_called()


@pytest.mark.asyncio
async def test_health_monitor_resets_counter_on_recovery(conn):
    """A successful probe between failures resets the counter so a flapping
    deployment doesn't accumulate failures across non-consecutive ticks.
    """
    dep_id = _seed_ready_deployment(conn)

    manager = MagicMock()
    manager._emit = AsyncMock()

    # Programmable transport that alternates 503, 200, 503, 503, 503...
    responses = iter([503, 200, 503, 503, 503])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(responses))

    def factory():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0,
        )

    monitor = HealthMonitor(
        conn=conn,
        backends={"stub": _StubBackend()},
        manager=manager,
        interval_s=0.01,
        max_consecutive_failures=3,
        client_factory=factory,
    )

    # 503 -> count=1
    await monitor.tick_once()
    assert monitor._failures[dep_id] == 1
    # 200 -> reset
    await monitor.tick_once()
    assert dep_id not in monitor._failures
    # 503, 503, 503 -> count hits 3
    await monitor.tick_once()
    await monitor.tick_once()
    assert dep_store.get_by_id(conn, dep_id).status == "ready"
    await monitor.tick_once()
    assert dep_store.get_by_id(conn, dep_id).status == "failed"


@pytest.mark.asyncio
async def test_health_monitor_prunes_stale_counter_entries(conn):
    """Entries for deployments no longer in ready must be dropped, otherwise
    the counter dict would grow unbounded across churn.
    """
    dep_id = _seed_ready_deployment(conn)

    manager = MagicMock()
    manager._emit = AsyncMock()

    monitor = HealthMonitor(
        conn=conn,
        backends={"stub": _StubBackend()},
        manager=manager,
        interval_s=0.01,
        max_consecutive_failures=10,
        client_factory=_make_client_factory(503),
    )

    await monitor.tick_once()
    assert dep_id in monitor._failures

    # Move the deployment out of 'ready' (operator stopped it).
    dep_store.update_status(conn, dep_id, "stopped")

    await monitor.tick_once()
    assert dep_id not in monitor._failures
