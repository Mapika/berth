from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response

from berth import __version__ as _berth_version
from berth.auth.stream_tokens import StreamTokenStore
from berth.auth.tiers import load_tiers
from berth.backends.base import Backend
from berth.cluster.agent_registry import AgentRegistry
from berth.cluster.health_watcher import run_health_watcher
from berth.cluster.leader_hub import LeaderHub
from berth.cluster.local_agent import LocalAgentLink
from berth.cluster.local_bootstrap import ensure_local_node
from berth.daemon.admin import router as admin_router
from berth.daemon.admin import unauthed_router as admin_unauthed_router
from berth.daemon.metrics_router import router as metrics_router
from berth.daemon.openai_proxy import router as openai_router
from berth.daemon.security_headers import SecurityHeadersMiddleware
from berth.daemon.ui_router import install_ui
from berth.lifecycle.docker_client import DockerClient
from berth.lifecycle.manager import LifecycleManager
from berth.lifecycle.topology import Topology
from berth.observability.events import EventBus
from berth.store import nodes as nodes_store

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
    in_flight: Any = None,
    latency: Any = None,
    local_control_surface: bool = False,
) -> None:
    from berth.cluster.metrics_collector import (
        InFlightCounter,
        LatencyRecorder,
    )

    app.state.conn = conn
    app.state.backends = backends
    app.state.manager = manager
    app.state.event_bus = event_bus
    app.state.stream_tokens = stream_tokens
    app.state.tier_cfg = load_tiers()
    app.state.request_count = 0
    app.state.request_tracer = request_tracer
    app.state.local_control_surface = local_control_surface
    # Collectors default to fresh instances for callers that build a
    # single app (build_app / legacy tests). The multi-app build_apps
    # path passes one shared pair so any of the three listeners feeds
    # the same metrics aggregator.
    app.state.in_flight = in_flight if in_flight is not None else InFlightCounter()
    app.state.latency = latency if latency is not None else LatencyRecorder()
    # Readiness flag. Flipped to True by the lifespan after reconcile +
    # background tasks have started. /readyz consults this in addition
    # to a DB liveness check.
    app.state.ready = False

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz(response: Response):
        if not app.state.ready:
            response.status_code = 503
            return {"ready": False, "reason": "lifespan not yet complete"}
        try:
            app.state.conn.execute("SELECT 1").fetchone()
        except Exception as e:
            response.status_code = 503
            return {"ready": False, "reason": f"db: {e!r}"}
        return {"ready": True}


def build_apps(
    *,
    conn: sqlite3.Connection,
    docker_client: DockerClient | None,
    backends: dict[str, Backend],
    models_dir: Path,
    topology: Topology | None = None,
    configs_dir: Path | None = None,
    serve_home: Path | None = None,
    leader_url: str | None = None,
    resolved_cfg: object | None = None,
    leader_only: bool = False,
) -> tuple[FastAPI, FastAPI, FastAPI]:
    """Returns (public_app, cluster_app, uds_app) sharing one LifecycleManager.

    - public_app: external-client surface. /v1/*, /admin/* (bearer auth),
      /healthz, /metrics. Owns the lifespan (reconcile on startup, stop_all
      on shutdown). Does NOT host /cluster/agent or /admin/nodes/register —
      those are on cluster_app only so they can be firewalled separately
      from the public API.
    - cluster_app: agent-transport surface. /cluster/agent (mTLS WS),
      /admin/nodes/register (token-gated), /admin/ca.pem (unauth pin),
      /healthz. Designed to be exposed only to known agent hosts.
    - uds_app: full surface for the local CLI. Shares the single Reaper
      and manager with public_app.
    """
    # Ensure the local-node row exists before anything that reads it.
    ensure_local_node(conn, agent_version=_berth_version)

    # Configure the API-key pepper so /v1/* + /admin/* auth uses HMAC-SHA256
    # rather than plain SHA-256. The pepper lives at BERTH_DIR/key_pepper
    # (mode 0600); it's generated on first start. The file must be
    # backed up alongside db.sqlite — if it's lost, every key falls and
    # operators must re-mint.
    from berth import config as _cfg
    from berth.store import api_keys as _ak_setup
    _ak_setup.configure_pepper(_cfg.BERTH_DIR / "key_pepper")

    # Startup warning: local control can create the first key without a
    # bearer when no API keys exist. TCP listeners still require auth, so
    # an explicitly exposed daemon does not become an open endpoint.
    from berth.store import api_keys as _ak_store
    if _ak_store.count_active(conn) == 0:
        log.warning(
            "no API keys exist; create the first key over the local control "
            "socket with `berth key create`; TCP /v1/*, /admin/*, and "
            "/metrics require bearer auth",
        )

    # Wire up the AgentLink registry. Local node first; remote agents join
    # via LeaderHub WS handshake.
    agent_registry = AgentRegistry()
    from berth.cluster.metrics_collector import (
        InFlightCounter,
        LatencyRecorder,
    )
    from berth.daemon.metrics_aggregator import MetricsAggregator
    from berth.routing.affinity import RoutingAffinity
    metrics_aggregator = MetricsAggregator()
    routing_affinity = RoutingAffinity(capacity=10_000)
    # Single pair of collectors shared by all three apps so a request
    # over any listener feeds the same metrics aggregator.
    shared_in_flight = InFlightCounter()
    shared_latency = LatencyRecorder()
    local_node = nodes_store.find_by_label(conn, "local")
    if local_node is None:
        raise RuntimeError("local node row missing after ensure_local_node")
    if docker_client is not None:
        agent_registry.register(LocalAgentLink(
            node_id=local_node.id, docker_client=docker_client,
        ))
    elif not leader_only:
        # Defensive: callers can pass docker_client=None today (e.g. tests
        # that mock the whole manager), so don't force leader_only on every
        # such path — but flag it loudly when it isn't intentional.
        log.warning(
            "build_apps called without a docker_client and leader_only=False; "
            "local deployments will fail until an agent enrolls"
        )

    # CA + enrollment-token store for agent enrollment.
    from berth import config as _cfg
    from berth.cluster.ca import generate_ca, load_ca
    from berth.cluster.enrollment import EnrollmentTokens
    home = serve_home or _cfg.BERTH_DIR
    ca_dir = home / "ca"
    if not (ca_dir / "ca.crt").exists():
        generate_ca(ca_dir, common_name="berth-ca")
    ca = load_ca(ca_dir)
    enrollment_tokens = EnrollmentTokens()

    event_bus = EventBus()
    stream_tokens = StreamTokenStore()
    from berth.daemon.request_tracer import RequestTracer
    request_tracer = RequestTracer()
    manager = LifecycleManager(
        conn=conn,
        docker_client=docker_client,
        backends=backends,
        models_dir=models_dir,
        topology=topology,
        event_bus=event_bus,
        configs_dir=configs_dir,
        agent_registry=agent_registry,
        resolved_cfg=resolved_cfg,
    )

    from berth.lifecycle.health_monitor import HealthMonitor
    from berth.lifecycle.predictor_task import PredictorTask
    from berth.lifecycle.reaper import Reaper
    from berth.store import deployments as _dep_store
    reaper = Reaper(
        manager=manager,
        list_ready=lambda: _dep_store.list_ready(conn),
    )
    health_monitor = HealthMonitor(
        conn=conn,
        backends=backends,
        manager=manager,
        agent_registry=agent_registry,
    )
    # Predictor pre-warms likely-needed adapters on a fixed interval.
    # Operators tune it with ~/.berth/predictor.yaml.
    from berth import config as _cfg
    from berth.lifecycle.predictor import PredictorConfig
    from berth.lifecycle.usage_rollup_task import UsageRollupTask
    predictor_cfg = PredictorConfig.load(_cfg.BERTH_DIR / "predictor.yaml")
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
        import asyncio as _asyncio

        async def _local_metrics_tick() -> None:
            """Push the leader's own collector data into the aggregator
            on the same cadence as the agent heartbeat. The leader does
            not heartbeat itself, so without this its local-node metrics
            never reach /metrics, /admin/metrics/snapshot, or the
            scorer's signals_by_node."""
            import time as _time

            from berth.cluster.metrics_collector import build_snapshot
            started = _time.time()
            local_node = nodes_store.find_by_label(conn, "local")
            if local_node is None:
                return
            # Bound to the shared collector instances from build_apps's
            # local scope — same objects every listener increments.
            in_flight = shared_in_flight
            latency = shared_latency
            while True:
                try:
                    deployment_models = {}
                    from berth.store import models as _model_store
                    for d in _dep_store.list_all(conn):
                        model = _model_store.get_by_id(conn, d.model_id)
                        deployment_models[d.id] = (
                            model.name if model is not None else f"model#{d.model_id}"
                        )
                    sample = build_snapshot(
                        in_flight=in_flight,
                        latency=latency,
                        deployment_models=deployment_models,
                        uptime_s=_time.time() - started,
                    )
                    metrics_aggregator.ingest(
                        node_id=local_node.id,
                        sample=sample,
                        ts=_time.time(),
                    )
                except Exception:
                    log.exception("local metrics tick failed")
                await _asyncio.sleep(5.0)

        cluster_watcher = _asyncio.create_task(
            run_health_watcher(
                conn, agent_registry, affinity=routing_affinity,
            )
        )
        local_metrics_task = _asyncio.create_task(_local_metrics_tick())
        # All startup tasks launched — flip the readiness flag for /readyz.
        # All three apps share the same conn + lifecycle here.
        public_app.state.ready = True
        cluster_app.state.ready = True
        uds_app.state.ready = True
        yield
        # Shutdown
        local_metrics_task.cancel()
        try:
            await local_metrics_task
        except (Exception, _asyncio.CancelledError):
            pass
        cluster_watcher.cancel()
        try:
            await cluster_watcher
        except (Exception, _asyncio.CancelledError):
            pass
        await rollup_task.stop()
        await predictor_task.stop()
        await health_monitor.stop()
        await reaper.stop()
        try:
            await manager.stop_all()
        except Exception:
            log.exception("stop_all on shutdown failed")

    import os as _os

    from berth.cluster.ca import fingerprint_ca_pem
    from berth.config import _env_get as _berth_env_get
    from berth.daemon.admin import cluster_router as admin_cluster_router
    resolved_leader_url = (
        leader_url
        or _berth_env_get(_os.environ, "SERVE_LEADER_URL")
        or "https://127.0.0.1:11501"
    )
    ca_fingerprint = fingerprint_ca_pem(ca.cert_pem)

    def _wire_common_state(app: FastAPI) -> None:
        app.state.predictor_task = predictor_task
        app.state.agent_registry = agent_registry
        app.state.metrics_aggregator = metrics_aggregator
        app.state.routing_affinity = routing_affinity
        # The rate limiter checks these flags to decide whether to honour
        # X-Forwarded-For. Defaults to False (legacy direct-TLS mode).
        app.state.trust_proxy_headers = bool(
            getattr(resolved_cfg, "trust_proxy_headers", False)
        )
        app.state.forwarded_allow_ips = str(
            getattr(resolved_cfg, "forwarded_allow_ips", "127.0.0.1")
        )
        app.state.ca = ca
        app.state.ca_cert_pem = ca.cert_pem.decode("ascii")
        app.state.ca_fingerprint = ca_fingerprint
        app.state.enrollment_tokens = enrollment_tokens
        app.state.leader_url = resolved_leader_url
        # Stash the resolved config so /admin/cluster + /admin/config
        # don't re-parse config.toml on every poll. None for the
        # build_app legacy path (tests) — admin routes fall back to a
        # fresh resolve when absent.
        app.state.resolved_cfg = resolved_cfg

    # public_app: external client surface. Owns the lifespan.
    public_app = FastAPI(
        title="berth (public)",
        version=_berth_version,
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    _attach_state(
        public_app,
        conn=conn, backends=backends, manager=manager,
        event_bus=event_bus, stream_tokens=stream_tokens,
        request_tracer=request_tracer,
        in_flight=shared_in_flight, latency=shared_latency,
    )
    _wire_common_state(public_app)
    # Public listener gets a body-size cap. Without one a single multi-GB
    # POST to /v1/* will OOM a small VPS. uds_app deliberately omits the
    # cap — operator endpoints upload adapter weights and such.
    from berth.daemon.body_size_limit import BodySizeLimitMiddleware
    public_max_body_size = getattr(
        resolved_cfg, "max_body_size_bytes", 10 * 1024 * 1024,
    )
    public_app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=public_max_body_size,
    )
    public_app.add_middleware(SecurityHeadersMiddleware)
    public_app.include_router(openai_router)
    public_app.include_router(metrics_router)
    public_app.include_router(admin_router)
    install_ui(public_app)

    # cluster_app: agent transport. Hosts the WS hub, registration, and
    # CA endpoint. Does NOT host /v1/* or general admin routes.
    cluster_app = FastAPI(
        title="berth (cluster)",
        version=_berth_version,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    _attach_state(
        cluster_app,
        conn=conn, backends=backends, manager=manager,
        event_bus=event_bus, stream_tokens=stream_tokens,
        request_tracer=request_tracer,
        in_flight=shared_in_flight, latency=shared_latency,
    )
    _wire_common_state(cluster_app)
    cluster_app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=getattr(
            resolved_cfg,
            "cluster_max_body_size_bytes",
            min(public_max_body_size, 1024 * 1024),
        ),
    )
    cluster_app.add_middleware(SecurityHeadersMiddleware)
    cluster_app.include_router(admin_unauthed_router)
    cluster_app.include_router(admin_cluster_router)
    cluster_app.include_router(
        LeaderHub(
            conn=conn, registry=agent_registry,
            aggregator=metrics_aggregator,
        ).router
    )

    # uds_app: full local surface for the CLI.
    uds_app = FastAPI(title="berth (control)", version=_berth_version)
    _attach_state(
        uds_app,
        conn=conn, backends=backends, manager=manager,
        event_bus=event_bus, stream_tokens=stream_tokens,
        request_tracer=request_tracer,
        in_flight=shared_in_flight, latency=shared_latency,
        local_control_surface=True,
    )
    _wire_common_state(uds_app)
    uds_app.include_router(openai_router)
    uds_app.include_router(admin_router)
    uds_app.include_router(admin_unauthed_router)
    uds_app.include_router(admin_cluster_router)
    uds_app.include_router(metrics_router)

    return public_app, cluster_app, uds_app


def build_app(
    *,
    conn: sqlite3.Connection,
    docker_client: DockerClient | None,
    backends: dict[str, Backend],
    models_dir: Path,
    topology: Topology | None = None,
    serve_home: Path | None = None,
) -> FastAPI:
    """Single-app factory retained for tests that exercise the full surface."""
    _public, _cluster, uds_app = build_apps(
        conn=conn,
        docker_client=docker_client,
        backends=backends,
        models_dir=models_dir,
        topology=topology,
        serve_home=serve_home,
    )
    return uds_app
