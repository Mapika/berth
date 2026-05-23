from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from ipaddress import ip_address, ip_network

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from berth.cluster.agent_registry import AgentRegistry
from berth.cluster.protocol import (
    Heartbeat,
    Hello,
    ReportAdopted,
    Welcome,
    decode_frame,
    encode_frame,
)
from berth.cluster.remote_agent import RemoteAgentLink
from berth.daemon.metrics_aggregator import MetricsAggregator
from berth.store import deployments as dep_store
from berth.store import models as model_store
from berth.store import node_gpus as node_gpus_store
from berth.store import nodes as nodes_store

log = logging.getLogger(__name__)


def _effective_vram_mb(
    conn,
    node_id: int,
    gpu_ids: list[int],
    reported_mb: int,
) -> int:
    """Return the VRAM reservation to record for an adopted endpoint.

    If the operator supplied a non-zero value, honour it (operator wins).
    Otherwise look up the node's per-GPU totals and sum them for the
    endpoint's GPUs so placement treats those GPUs as fully occupied.
    Falls back to 0 when no node_gpus info is available."""
    if reported_mb:
        return reported_mb
    gpu_map = {g.gpu_index: g.total_vram_mb for g in node_gpus_store.list_for_node(conn, node_id)}
    return sum(gpu_map.get(idx, 0) for idx in gpu_ids)


def reconcile_adopted(conn, *, node_id: int, endpoints: list[dict]) -> None:
    """Make this node's source='adopted' rows equal `endpoints` (full state).

    Upserts present entries (creating the model row if needed), marks them
    ready/failed by `alive`, and deletes rows whose container_id is absent
    from the report. An endpoint whose GPUs collide with a *managed* ready
    deployment is skipped (logged); its row, if any, is removed."""
    managed_gpus: set[int] = set()
    for d in dep_store.list_all(conn):
        if d.source == "managed" and d.status in ("pending", "loading", "ready"):
            managed_gpus.update(d.gpu_ids)

    keep_cids: set[str] = set()
    for ep in endpoints:
        if managed_gpus.intersection(ep.get("gpu_ids") or []):
            log.warning(
                "adopted endpoint %s on node %s conflicts with managed GPUs %s; skipping",
                ep.get("container_id"), node_id,
                sorted(managed_gpus.intersection(ep.get("gpu_ids", []))),
            )
            continue
        model = model_store.get_by_name(conn, ep["served_model_name"])
        if model is None:
            try:
                model = model_store.add(
                    conn, name=ep["served_model_name"], hf_repo=ep["served_model_name"])
            except model_store.AlreadyExists:
                model = model_store.get_by_name(conn, ep["served_model_name"])
        if model is None:
            log.warning(
                "could not resolve model %r for adopted endpoint on node %s",
                ep["served_model_name"], node_id)
            continue
        ep_gpu_ids = list(ep.get("gpu_ids") or [])
        vram_mb = _effective_vram_mb(
            conn, node_id, ep_gpu_ids,
            int(ep.get("vram_reserved_mb") or 0),
        )
        dep_store.upsert_adopted(
            conn, model_id=model.id, node_id=node_id,
            container_id=ep["container_id"], address=ep["address"],
            port=int(ep["port"]), gpu_ids=ep_gpu_ids,
            vram_reserved_mb=vram_mb,
            image_tag=str(ep.get("image_tag") or "external"),
            status="ready" if ep.get("alive") else "failed",
        )
        keep_cids.add(ep["container_id"])

    for d in dep_store.list_adopted_for_node(conn, node_id):
        if d.container_id not in keep_cids:
            dep_store.delete_adopted(conn, d.id)


FingerprintResolver = Callable[[WebSocket], str | None]
_MAX_INVENTORY_INT = 1_000_000_000
# Hard cap on the serialised size of a single heartbeat metrics sample. A
# registered agent could otherwise store DEFAULT_WINDOW * sizeof(metrics) in
# the MetricsAggregator per node and trickle attacker-controlled JSON into
# the admin UI snapshot.
_MAX_HEARTBEAT_METRICS_BYTES = 32 * 1024


def _peer_cert_fingerprint(ws: WebSocket) -> str | None:
    """Pull the TLS peer-cert fingerprint from the ASGI scope.

    Requires the cluster listener to be started with our
    `TLSAwareWebSocketProtocol`, which injects the SSL object into
    `scope["extensions"]["berth.tls"]["ssl_object"]`. Returns None if no
    client cert was presented (callers reject the connection in that
    case)."""
    tls_ext = ws.scope.get("extensions", {}).get("berth.tls")
    if not tls_ext:
        return None
    ssl_obj = tls_ext.get("ssl_object")
    if ssl_obj is None:
        return None
    try:
        der = ssl_obj.getpeercert(binary_form=True)
    except Exception:
        return None
    if not der:
        return None
    return "sha256:" + hashlib.sha256(der).hexdigest()


def _default_fingerprint_resolver(ws: WebSocket) -> str | None:
    """Production resolver: trust the TLS layer's peer cert.

    Falls back to an `x-berth-client-fingerprint` header only if a
    proxy is explicitly configured to forward it via
    `BERTH_TRUST_FORWARDED_FP=1` and the direct TCP peer is in
    `BERTH_FORWARDED_ALLOW_IPS`."""
    fp = _peer_cert_fingerprint(ws)
    if fp is not None:
        return fp
    import os

    if os.environ.get("BERTH_TRUST_FORWARDED_FP") == "1":
        client_host = _websocket_client_host(ws)
        allowlist = os.environ.get("BERTH_FORWARDED_ALLOW_IPS") or "127.0.0.1"
        if client_host is not None and _allowed_proxy(client_host, allowlist):
            return ws.headers.get("x-berth-client-fingerprint")
        log.warning(
            "cluster ws reject: forwarded fingerprint from untrusted peer %s",
            client_host,
        )
    return None


def _allowed_proxy(host: str, allowlist: str) -> bool:
    for raw in allowlist.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if entry == "*":
            return True
        try:
            if ip_address(host) in ip_network(entry, strict=False):
                return True
        except ValueError:
            if host == entry:
                return True
    return False


def _websocket_client_host(ws: WebSocket) -> str | None:
    client = ws.client
    if client is None:
        client = ws.scope.get("client")
    if client is None:
        return None
    host = getattr(client, "host", None)
    if host is not None:
        return str(host)
    if isinstance(client, tuple) and client:
        return str(client[0])
    return str(client)


def _coerce_inventory_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        parsed = int(value)
    else:
        raise ValueError(f"{field} must be a non-negative integer")
    if parsed < 0 or parsed > _MAX_INVENTORY_INT:
        raise ValueError(f"{field} must be between 0 and {_MAX_INVENTORY_INT}")
    return parsed


def _parse_hello_inventory(host_info: dict[str, object]) -> dict[str, int]:
    return {
        "cpu_count": _coerce_inventory_int(host_info.get("cpu_count", 0), "cpu_count"),
        "total_ram_mb": _coerce_inventory_int(
            host_info.get("total_ram_mb", 0),
            "total_ram_mb",
        ),
        "gpu_count": _coerce_inventory_int(host_info.get("gpu_count", 0), "gpu_count"),
        "total_vram_mb": _coerce_inventory_int(
            host_info.get("total_vram_mb", 0),
            "total_vram_mb",
        ),
    }


class _WSAdapter:
    """Adapt FastAPI WebSocket to the duck type RemoteAgentLink expects."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send(self, msg: str) -> None:
        await self._ws.send_text(msg)

    async def recv(self) -> str | None:
        try:
            return await self._ws.receive_text()
        except WebSocketDisconnect:
            return None

    async def close(self, *, code: int = 1000) -> None:
        try:
            await self._ws.close(code=code)
        except Exception:  # nosec
            pass


class LeaderHub:
    """FastAPI WebSocket endpoint that accepts agent connections, verifies
    their cert fingerprint against the `nodes` table, completes a
    Hello/Welcome handshake, and registers a RemoteAgentLink with the
    AgentRegistry for the duration of the connection.

    The fingerprint check queries the DB on every connection (not the
    in-memory registry), so `berth nodes remove` takes effect on the
    next handshake."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        registry: AgentRegistry,
        fingerprint_resolver: FingerprintResolver = _default_fingerprint_resolver,
        aggregator: MetricsAggregator | None = None,
    ) -> None:
        self._conn = conn
        self._registry = registry
        self._resolve_fp = fingerprint_resolver
        self._aggregator = aggregator
        self.router = APIRouter()
        self.router.add_api_websocket_route("/cluster/agent", self._handle_agent)

    def _handle_heartbeat(self, *, node_id: int, frame: Heartbeat) -> None:
        nodes_store.set_status(
            self._conn, node_id, status="ready", last_seen=time.time(),
        )
        if frame.metrics is None or self._aggregator is None:
            return
        try:
            sample_size = len(
                json.dumps(frame.metrics, separators=(",", ":")).encode("utf-8")
            )
        except (TypeError, ValueError) as e:
            log.warning(
                "node %s heartbeat metrics not JSON-serialisable; dropping sample (%s)",
                node_id, e,
            )
            return
        if sample_size > _MAX_HEARTBEAT_METRICS_BYTES:
            log.warning(
                "node %s heartbeat metrics %d bytes exceeds %d byte cap; dropping sample",
                node_id, sample_size, _MAX_HEARTBEAT_METRICS_BYTES,
            )
            return
        self._aggregator.ingest(
            node_id=node_id, sample=frame.metrics, ts=frame.ts,
        )

    async def _handle_agent(self, ws: WebSocket) -> None:
        fp = self._resolve_fp(ws)
        if fp is None:
            log.warning(
                "cluster ws reject: no client cert presented (client=%s)",
                ws.client,
            )
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        node = nodes_store.find_by_fingerprint(self._conn, fp)
        if node is None:
            log.warning(
                "cluster ws reject: fingerprint %s not in nodes DB (client=%s)",
                fp[:23] + "…", ws.client,
            )
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await ws.accept()
        try:
            hello_text = await ws.receive_text()
            hello = decode_frame(hello_text)
        except (WebSocketDisconnect, ValueError):
            return

        if not isinstance(hello, Hello):
            await ws.close(code=status.WS_1003_UNSUPPORTED_DATA)
            return

        try:
            inventory = _parse_hello_inventory(hello.host_info)
        except ValueError as e:
            log.warning(
                "cluster ws reject: malformed hello inventory from node %s: %s",
                node.id, e,
            )
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        now = time.time()
        nodes_store.update_inventory(
            self._conn, node.id,
            agent_version=hello.agent_version,
            cpu_count=inventory["cpu_count"],
            total_ram_mb=inventory["total_ram_mb"],
            gpu_count=inventory["gpu_count"],
            total_vram_mb=inventory["total_vram_mb"],
        )
        nodes_store.set_status(self._conn, node.id, status="ready", last_seen=now)
        try:
            await ws.send_text(encode_frame(
                Welcome(node_id=node.id, server_time=now)
            ))
        except WebSocketDisconnect:
            return

        link = RemoteAgentLink(node_id=node.id, ws=_WSAdapter(ws))
        displaced = self._registry.register(link)
        if isinstance(displaced, RemoteAgentLink):
            # A previous remote WebSocket for this node is still attached. The
            # new connection wins (operators expect ``berth agent run``
            # reconnects after a dead-half-open TCP to take over), and we close
            # the old one cleanly so it can't keep racing on heartbeat state.
            log.info(
                "cluster ws: displacing existing link for node %s on reconnect",
                node.id,
            )
            try:
                await displaced.aclose(code=status.WS_1001_GOING_AWAY)
            except Exception:  # nosec
                pass
        try:
            while True:
                try:
                    raw = await ws.receive_text()
                except WebSocketDisconnect:
                    break
                try:
                    frame = decode_frame(raw)
                except ValueError:
                    log.warning("dropping malformed frame from node %s", node.id)
                    continue
                if isinstance(frame, Heartbeat):
                    self._handle_heartbeat(node_id=node.id, frame=frame)
                    continue
                if isinstance(frame, ReportAdopted):
                    try:
                        reconcile_adopted(
                            self._conn, node_id=node.id, endpoints=frame.endpoints)
                    except Exception:
                        log.exception("adopted reconcile failed for node %s", node.id)
                    continue
                await link.inbound(frame)
        finally:
            link.shutdown()
            # Only mark the node unreachable if we were the active link. If a
            # newer connection has displaced us, the agent is still online via
            # that newer link — clobbering status here would create a status
            # flap and a brief scheduler blackhole.
            if self._registry.unregister(link):
                if self._aggregator is not None:
                    self._aggregator.drop_node(node.id)
                nodes_store.set_status(
                    self._conn, node.id,
                    status="unreachable", last_seen=time.time(),
                )
