from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from serve_engine.observability.metrics import (
    format_cluster_metrics,
    format_daemon_metrics,
    gather_engine_metrics,
)
from serve_engine.store import api_keys as _ak_store
from serve_engine.store import deployments as dep_store
from serve_engine.store import models as model_store
from serve_engine.store import nodes as nodes_store

router = APIRouter()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> str:
    conn: sqlite3.Connection = request.app.state.conn
    by_status: dict[str, int] = {}
    for d in dep_store.list_all(conn):
        by_status[d.status] = by_status.get(d.status, 0) + 1
    daemon_text = format_daemon_metrics(
        deployments_by_status=by_status,
        models_total=len(model_store.list_all(conn)),
        api_keys_active=_ak_store.count_active(conn),
        request_count=request.app.state.request_count,
    )

    # Engine metrics
    backends_dict = request.app.state.backends
    engine_urls: list[tuple[int, str]] = []
    for d in dep_store.list_ready(conn):
        backend = backends_dict.get(d.backend)
        if backend is None or d.container_address is None:
            continue
        url = f"http://{d.container_address}:{d.container_port}{backend.metrics_path}"
        engine_urls.append((d.id, url))
    engine_text = await gather_engine_metrics(engine_urls)

    # Cluster metrics (per-node + per-deployment) from the aggregator.
    # Empty when no agents have reported yet — string is just appended.
    cluster_text = ""
    aggregator = getattr(request.app.state, "metrics_aggregator", None)
    if aggregator is not None:
        labels = {n.id: n.label for n in nodes_store.list_all(conn)}
        cluster_text = format_cluster_metrics(aggregator, node_labels=labels)
    return daemon_text + engine_text + cluster_text
