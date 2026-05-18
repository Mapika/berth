from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from serve_engine import __version__ as _serve_version
from serve_engine.auth.stream_tokens import StreamTokenStore
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.leader_hub import LeaderHub
from serve_engine.cluster.local_agent import LocalAgentLink
from serve_engine.cluster.local_bootstrap import ensure_local_node
from serve_engine.store import nodes as nodes_store
from serve_engine.auth.tiers import load_tiers
from serve_engine.backends.base import Backend
from serve_engine.daemon.admin import router as admin_router
from serve_engine.daemon.metrics_router import router as metrics_router
from serve_engine.daemon.openai_proxy import router as openai_router
from serve_engine.daemon.ui_router import install_ui
from serve_engine.lifecycle.docker_client import DockerClient
from serve_engine.lifecycle.manager import LifecycleManager
from serve_engine.lifecycle.topology import Topology
from serve_engine.observability.events import EventBus

log = logging.getLogger(__name__)


def _attach_state(
    app: FastAPI,
    *,
    conn: sqlite3.Connection,
    backends: dict[str, Backend],
    manager: LifecycleManager,
    event_bus: EventBus,
    stream_tokens: StreamTokenStore,
    request_tracer: Any,
) -> None:
    app.state.conn = conn
    app.state.backends = backends
    app.state.manager = manager
    app.state.event_bus = event_bus
    app.state.stream_tokens = stream_tokens
    app.state.tier_cfg = load_tiers()
    app.state.request_count = 0
    app.state.request_tracer = request_tracer

    @app.get("/healthz")
    def healthz():
        return {"ok": True}


def build_apps(
    *,
    conn: sqlite3.Connection,
    docker_client: DockerClient,
    backends: dict[str, Backend],
    models_dir: Path,
    topology: Topology | None = None,
    configs_dir: Path | None = None,
) -> tuple[FastAPI, FastAPI]:
    """Returns (tcp_app, uds_app) sharing the same LifecycleManager.

    - tcp_app: public OpenAI-compatible API + admin routes. Owns the lifespan
      (reconcile on startup, stop_all on shutdown).
    - uds_app: full surface (admin + OpenAI) for the local CLI / future UI.
      No separate lifespan - shares the single Reaper and manager with tcp_app.
    """
    # Ensure the local-node row exists before anything that reads it.
    ensure_local_node(conn, agent_version=_serve_version)

    # Wire up the AgentLink registry. Local node first; remote agents join
    # via LeaderHub WS handshake.
    agent_registry = AgentRegistry()
    local_node = nodes_store.find_by_label(conn, "local")
    if local_node is None:
        raise RuntimeError("local node row missing after ensure_local_node")
    agent_registry.register(LocalAgentLink(
        node_id=local_node.id, docker_client=docker_client,
    ))

    event_bus = EventBus()
    stream_tokens = StreamTokenStore()
    from serve_engine.daemon.request_tracer import RequestTracer
    request_tracer = RequestTracer()
    manager = LifecycleManager(
        conn=conn,
        docker_client=docker_client,
        backends=backends,
        models_dir=models_dir,
        topology=topology,
        event_bus=event_bus,
        configs_dir=configs_dir,
    )

    from serve_engine.lifecycle.health_monitor import HealthMonitor
    from serve_engine.lifecycle.predictor_task import PredictorTask
    from serve_engine.lifecycle.reaper import Reaper
    from serve_engine.store import deployments as _dep_store
    reaper = Reaper(
        manager=manager,
        list_ready=lambda: _dep_store.list_ready(conn),
    )
    health_monitor = HealthMonitor(
        conn=conn,
        backends=backends,
        manager=manager,
    )
    # Predictor pre-warms likely-needed adapters on a fixed interval.
    # Operators tune it with ~/.serve/predictor.yaml.
    from serve_engine import config as _cfg
    from serve_engine.lifecycle.predictor import PredictorConfig
    from serve_engine.lifecycle.usage_rollup_task import UsageRollupTask
    predictor_cfg = PredictorConfig.load(_cfg.SERVE_DIR / "predictor.yaml")
    predictor_task = PredictorTask(
        conn=conn,
        backends=backends,
        models_dir=models_dir,
        config=predictor_cfg,
        manager=manager,
    )
    # Daily rollup: events older than predictor_cfg.retention_days get
    # aggregated into usage_aggregates and removed from usage_events.
    # Keeps the predictor's hot table bounded for long-running boxes.
    rollup_task = UsageRollupTask(conn=conn, config=predictor_cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Startup
        try:
            await manager.reconcile()
        except Exception:
            log.exception("reconcile failed; continuing")
        reaper.start()
        health_monitor.start()
        predictor_task.start()
        rollup_task.start()
        yield
        # Shutdown
        await rollup_task.stop()
        await predictor_task.stop()
        await health_monitor.stop()
        await reaper.stop()
        try:
            await manager.stop_all()
        except Exception:
            log.exception("stop_all on shutdown failed")

    tcp_app = FastAPI(title="serve-engine (public)", version="0.0.1", lifespan=lifespan)
    _attach_state(
        tcp_app,
        conn=conn,
        backends=backends,
        manager=manager,
        event_bus=event_bus,
        stream_tokens=stream_tokens,
        request_tracer=request_tracer,
    )
    tcp_app.state.predictor_task = predictor_task
    tcp_app.state.agent_registry = agent_registry
    tcp_app.include_router(openai_router)
    tcp_app.include_router(metrics_router)
    tcp_app.include_router(admin_router)
    tcp_app.include_router(LeaderHub(conn=conn, registry=agent_registry).router)
    install_ui(tcp_app)

    uds_app = FastAPI(title="serve-engine (control)", version="0.0.1")
    _attach_state(
        uds_app,
        conn=conn,
        backends=backends,
        manager=manager,
        event_bus=event_bus,
        stream_tokens=stream_tokens,
        request_tracer=request_tracer,
    )
    uds_app.state.predictor_task = predictor_task
    uds_app.state.agent_registry = agent_registry
    uds_app.include_router(openai_router)
    uds_app.include_router(admin_router)
    uds_app.include_router(metrics_router)

    return tcp_app, uds_app


def build_app(
    *,
    conn: sqlite3.Connection,
    docker_client: DockerClient,
    backends: dict[str, Backend],
    models_dir: Path,
    topology: Topology | None = None,
) -> FastAPI:
    """Single-app factory retained for tests that exercise the full surface."""
    _, uds_app = build_apps(
        conn=conn,
        docker_client=docker_client,
        backends=backends,
        models_dir=models_dir,
        topology=topology,
    )
    return uds_app
