from __future__ import annotations

import sqlite3
from dataclasses import asdict

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from berth import config as _cfg
from berth.daemon.admin import get_conn, render_metrics_snapshot, router
from berth.store import node_gpus as node_gpus_store
from berth.store import nodes as nodes_store


class EnrollBody(BaseModel):
    label: str


@router.post("/nodes/enroll")
def admin_nodes_enroll(body: EnrollBody, request: Request):
    token = request.app.state.enrollment_tokens.mint(label=body.label)
    return {
        "token": token,
        "leader_url": request.app.state.leader_url,
        "ca_cert": request.app.state.ca_cert_pem,
        "ca_fingerprint": request.app.state.ca_fingerprint,
    }


@router.get("/nodes")
def admin_nodes_list(conn: sqlite3.Connection = Depends(get_conn)):
    return {"nodes": [asdict(node) for node in nodes_store.list_all(conn)]}


@router.get("/metrics/snapshot")
def admin_metrics_snapshot(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    aggregator = getattr(request.app.state, "metrics_aggregator", None)
    if aggregator is None:
        return {"nodes": []}
    return render_metrics_snapshot(aggregator, nodes=nodes_store.list_all(conn))


@router.get("/nodes/{node_id}")
def admin_nodes_show(
    node_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
):
    node = nodes_store.get(conn, node_id)
    if node is None:
        raise HTTPException(404, f"node {node_id} not found")
    return {
        "node": asdict(node),
        "gpus": [asdict(gpu) for gpu in node_gpus_store.list_for_node(conn, node_id)],
    }


@router.delete("/nodes/{node_id}")
def admin_nodes_remove(
    node_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    node = nodes_store.get(conn, node_id)
    if node is None:
        raise HTTPException(404, f"node {node_id} not found")
    if node.label == "local":
        raise HTTPException(400, "cannot remove the local node")
    registry = request.app.state.agent_registry
    if registry.get(node_id) is not None:
        registry.unregister(node_id)
    nodes_store.delete(conn, node_id)
    return {"ok": True}


def _resolved_cfg(request: Request):
    cached = getattr(request.app.state, "resolved_cfg", None)
    if cached is not None:
        return cached
    return _cfg.resolve_config()


@router.get("/cluster")
def admin_cluster_info(request: Request):
    from datetime import UTC, datetime

    from cryptography import x509

    server_crt_path = _cfg.LEADER_DIR / "server.crt"
    server_info: dict[str, object] = {"present": False}
    if server_crt_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(server_crt_path.read_bytes())
            try:
                san = cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName,
                ).value
                sans = [
                    str(entry.value) if isinstance(entry, x509.IPAddress) else entry.value
                    for entry in san
                ]
            except x509.ExtensionNotFound:
                sans = []
            not_after = cert.not_valid_after_utc
            server_info = {
                "present": True,
                "san": sans,
                "not_after": not_after.isoformat(),
                "days_left": (not_after - datetime.now(UTC)).days,
            }
        except Exception as e:  # pragma: no cover
            server_info = {"present": True, "error": str(e)}

    cfg = _resolved_cfg(request)
    return {
        "leader_url": request.app.state.leader_url,
        "ca_fingerprint": request.app.state.ca_fingerprint,
        "public_url": cfg.public_url,
        "cluster_url": cfg.cluster_url,
        "public_bind": f"{cfg.public_bind}:{cfg.public_port}",
        "cluster_bind": f"{cfg.cluster_bind}:{cfg.cluster_port}",
        "public_tls_configured": cfg.public_cert_path is not None,
        "leader_server_cert": server_info,
    }


@router.get("/config")
def admin_config(request: Request):
    cfg = _resolved_cfg(request)
    return {
        "values": {
            "public_host": cfg.public_host,
            "public_port": cfg.public_port,
            "public_bind": cfg.public_bind,
            "cluster_host": cfg.cluster_host,
            "cluster_port": cfg.cluster_port,
            "cluster_bind": cfg.cluster_bind,
            "public_cert_path": (
                str(cfg.public_cert_path) if cfg.public_cert_path else None
            ),
            "public_key_path": (
                str(cfg.public_key_path) if cfg.public_key_path else None
            ),
            "leader_url_override": cfg.leader_url_override,
        },
        "sources": cfg.source,
        "config_file": str(_cfg.CONFIG_FILE),
        "config_file_exists": _cfg.CONFIG_FILE.exists(),
    }
