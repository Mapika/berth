from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import websockets
import yaml

from berth.cluster.metrics_collector import (
    InFlightCounter,
    LatencyRecorder,
    build_snapshot,
)
from berth.cluster import adopted as adopted_mod
from berth.cluster.protocol import (
    Frame,
    Heartbeat,
    Hello,
    HttpCancel,
    HttpChunk,
    HttpRequest,
    LogCancel,
    LogChunk,
    LogStream,
    OpResult,
    ReportAdopted,
    StartDeployment,
    StopDeployment,
    Welcome,
    decode_frame,
    encode_frame,
)


def build_heartbeat_frame(
    *,
    in_flight: InFlightCounter | None,
    latency: LatencyRecorder | None,
    deployment_models: dict[int, str],
    uptime_s: float,
) -> Heartbeat:
    """Assemble the Heartbeat frame sent by the agent's heartbeat task.

    Carries a metrics snapshot when collectors are provided, otherwise a
    bare ts-only heartbeat (for callers that haven't wired collectors
    yet). Kept module-level for unit testability without spinning up
    the WS loop in `run_agent`.
    """
    import time as _t
    if in_flight is None or latency is None:
        return Heartbeat(ts=_t.time())
    return Heartbeat(
        ts=_t.time(),
        metrics=build_snapshot(
            in_flight=in_flight,
            latency=latency,
            deployment_models=deployment_models,
            uptime_s=uptime_s,
        ),
    )

def build_adopted_report(
    entries: list, alive_by_cid: dict[str, bool]
) -> ReportAdopted:
    return ReportAdopted(endpoints=[
        e.to_report_dict(alive=alive_by_cid.get(e.container_id, True))
        for e in entries
    ])


def register_adopted_endpoints(disp, entries: list) -> None:
    for e in entries:
        disp.register_endpoint(
            container_id=e.container_id, address=e.address, port=e.port)


log = logging.getLogger(__name__)
StatusCallback = Callable[[str, dict[str, Any]], None]


class SerializedSender:
    """Serialize writes to a websocket-like send callable.

    The agent sends heartbeats, op results, HTTP chunks, and log chunks
    from different tasks. Keeping those writes behind one lock avoids
    concurrent websocket writes when the UI starts streaming through the
    tunnel.
    """

    def __init__(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send
        self._lock = asyncio.Lock()

    async def send(self, message: str) -> None:
        async with self._lock:
            await self._send(message)


def _emit_status(
    status_cb: StatusCallback | None,
    event: str,
    **payload: Any,
) -> None:
    if status_cb is None:
        return
    try:
        status_cb(event, payload)
    except Exception:
        log.debug("agent status callback failed", exc_info=True)


class AgentFrameDispatcher:
    """Handles inbound frames on the agent side.

    Side-effect-free except for the docker/http adapters it's constructed
    with — easy to unit-test with stubs.

    Expected adapter interfaces:
      docker.start(plan) -> awaitable returning (container_id, address, port)
      docker.stop(container_id, *, remove) -> awaitable
      http.stream(method, url, headers, body) -> awaitable returning an
        async iterable of (status_or_None, headers_or_None, chunk_bytes, eof)
    """

    def __init__(
        self,
        *,
        docker: Any,
        http: Any,
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        self._docker = docker
        self._http = http
        self._send = send
        self._endpoints: dict[str, tuple[str, int]] = {}
        self._inflight: dict[str, asyncio.Task[None]] = {}

    def register_endpoint(
        self, *, container_id: str, address: str, port: int,
    ) -> None:
        self._endpoints[container_id] = (address, port)

    async def handle(self, frame: Frame) -> None:
        if isinstance(frame, StartDeployment):
            await self._handle_start(frame)
        elif isinstance(frame, StopDeployment):
            await self._handle_stop(frame)
        elif isinstance(frame, HttpRequest):
            self._inflight[frame.stream_id] = asyncio.create_task(
                self._run_http_stream(frame)
            )
        elif isinstance(frame, HttpCancel):
            t = self._inflight.pop(frame.stream_id, None)
            if t is not None:
                t.cancel()
        elif isinstance(frame, LogStream):
            self._inflight[frame.stream_id] = asyncio.create_task(
                self._run_log_stream(frame)
            )
        elif isinstance(frame, LogCancel):
            t = self._inflight.pop(frame.stream_id, None)
            if t is not None:
                t.cancel()
        # Hello/Welcome/Heartbeat are handled by AgentClient before reaching
        # this dispatcher.

    async def _handle_start(self, frame: StartDeployment) -> None:
        try:
            cid, addr, port = await self._docker.start(frame.plan)
        except Exception as e:
            await self._send(encode_frame(OpResult(
                request_id=frame.request_id, ok=False, error=str(e),
            )))
            return
        self._endpoints[cid] = (addr, port)
        await self._send(encode_frame(OpResult(
            request_id=frame.request_id, ok=True,
            data={"container_id": cid, "address": "tunnel", "port": 0},
        )))

    async def _handle_stop(self, frame: StopDeployment) -> None:
        try:
            await self._docker.stop(frame.container_id, remove=True)
        except Exception as e:
            await self._send(encode_frame(OpResult(
                request_id=frame.request_id, ok=False, error=str(e),
            )))
            return
        self._endpoints.pop(frame.container_id, None)
        await self._send(encode_frame(OpResult(
            request_id=frame.request_id, ok=True,
        )))

    async def _run_http_stream(self, frame: HttpRequest) -> None:
        cid = frame.headers.get("x-berth-container-id")
        endpoint = self._endpoints.get(cid or "")
        if endpoint is None:
            await self._send(encode_frame(HttpChunk(
                stream_id=frame.stream_id, status=502,
                headers={"x-berth-error": "no-endpoint"},
                body_b64="", eof=True,
            )))
            return
        addr, port = endpoint
        body = base64.b64decode(frame.body_b64) if frame.body_b64 else b""
        url = f"http://{addr}:{port}{frame.path}"
        headers = {k: v for k, v in frame.headers.items()
                   if k != "x-berth-container-id"}
        try:
            agen = await self._http.stream(frame.method, url, headers, body)
            async for status, hdrs, chunk, eof in agen:
                await self._send(encode_frame(HttpChunk(
                    stream_id=frame.stream_id,
                    status=status,
                    headers=hdrs,
                    body_b64=(
                        base64.b64encode(chunk).decode("ascii") if chunk else ""
                    ),
                    eof=eof,
                )))
                if eof:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._send(encode_frame(HttpChunk(
                stream_id=frame.stream_id, status=502,
                headers={
                    "x-berth-error": "proxy-failed",
                    "x-berth-detail": str(e)[:200],
                },
                body_b64="", eof=True,
            )))
        finally:
            self._inflight.pop(frame.stream_id, None)

    async def _run_log_stream(self, frame: LogStream) -> None:
        """Stream docker logs for `frame.container_id` back as LogChunks.

        Bridges docker-py's blocking iterator on a worker thread into an
        asyncio.Queue the main task drains. Always emits a final eof
        chunk so the leader-side queue unblocks even on cancellation."""
        import threading

        q: asyncio.Queue = asyncio.Queue()
        sentinel: object = object()
        loop = asyncio.get_running_loop()

        def _pump() -> None:
            try:
                for chunk in self._docker.stream_logs_iter(
                    frame.container_id, tail=frame.tail, follow=frame.follow,
                ):
                    if not isinstance(chunk, bytes):
                        chunk = str(chunk).encode("utf-8", errors="replace")
                    asyncio.run_coroutine_threadsafe(q.put(chunk), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    q.put(f"[berth] log error: {e}\n".encode()), loop,
                )
            finally:
                asyncio.run_coroutine_threadsafe(q.put(sentinel), loop)

        threading.Thread(target=_pump, daemon=True).start()

        async def _emit(body: bytes, *, eof: bool) -> None:
            await self._send(encode_frame(LogChunk(
                stream_id=frame.stream_id,
                body_b64=base64.b64encode(body).decode("ascii") if body else "",
                eof=eof,
            )))

        try:
            while True:
                chunk = await q.get()
                if chunk is sentinel:
                    await _emit(b"", eof=True)
                    return
                await _emit(chunk, eof=False)
        except asyncio.CancelledError:
            try:
                await _emit(b"", eof=True)
            except Exception:
                pass  # nosec
            raise
        except Exception as e:
            try:
                await _emit(f"[berth] {e}\n".encode(), eof=True)
            except Exception:
                pass  # nosec
        finally:
            self._inflight.pop(frame.stream_id, None)


# ---------------------------------------------------------------------------
# Production runner — connects to the leader over mTLS WSS and reconnects
# on failure with exponential backoff.
# ---------------------------------------------------------------------------


def _rehydrate_docker_kwargs(kw: dict) -> dict:
    """Inverse of manager._json_safe_docker_kwargs — reconstruct
    docker.types.Ulimit objects from plain dicts so docker-py accepts them."""
    from docker.types import Ulimit  # type: ignore[import-untyped]
    out = dict(kw)
    ulimits = out.get("ulimits")
    if ulimits and isinstance(ulimits, list) and isinstance(ulimits[0], dict):
        out["ulimits"] = [
            Ulimit(name=u.get("name"), soft=u.get("soft"), hard=u.get("hard"))
            for u in ulimits
        ]
    return out


class _DockerAdapter:
    """Adapter from DockerClient's sync API to the awaitables the
    dispatcher expects.

    Handles two plan shapes:

    1. Local-style: caller pre-built `volumes` (full leader-side
       paths) and `command` argv (with absolute model path inside the
       container). We just hand it to docker.run.

    2. Remote-deploy: caller sends only HF coordinates + a `model_sentinel`
       placeholder in the argv. We download the model into the agent's
       own ~/.berth/models, materialise the engine config file inline if
       supplied, mount both as volumes, and substitute the sentinel with
       the in-container model path before running.
    """

    def __init__(
        self,
        dc,
        *,
        models_dir: Path | None = None,
        configs_dir: Path | None = None,
        status_cb: StatusCallback | None = None,
        quiet_downloads: bool = False,
    ):
        self._dc = dc
        self._status_cb = status_cb
        self._quiet_downloads = quiet_downloads
        # Default to the same on-disk layout the leader uses, but rooted
        # at the agent's own BERTH_HOME.
        from berth import config as _cfg
        self._models_dir = models_dir or _cfg.MODELS_DIR
        self._configs_dir = configs_dir or _cfg.CONFIGS_DIR

    async def start(self, plan):
        # Remote-style plan: HF download + sentinel substitution required.
        if "model_hf_repo" in plan:
            from berth.lifecycle.downloader import download_model

            self._models_dir.mkdir(parents=True, exist_ok=True)
            self._configs_dir.mkdir(parents=True, exist_ok=True)

            _emit_status(
                self._status_cb,
                "deployment.download_started",
                repo=plan["model_hf_repo"],
                revision=plan.get("model_revision", "main"),
            )
            local_path = await asyncio.to_thread(
                download_model,
                hf_repo=plan["model_hf_repo"],
                revision=plan.get("model_revision", "main"),
                cache_dir=self._models_dir,
                quiet=self._quiet_downloads,
            )
            _emit_status(
                self._status_cb,
                "deployment.download_finished",
                repo=plan["model_hf_repo"],
                local_path=str(local_path),
            )
            container_model_path = "/cache/" + str(
                Path(local_path).resolve().relative_to(self._models_dir.resolve())
            )
            sentinel = plan["model_sentinel"]
            command = [
                a.replace(sentinel, container_model_path)
                for a in plan["command"]
            ]
            volumes: dict[str, dict] = {
                str(self._models_dir.resolve()): {"bind": "/cache", "mode": "ro"},
            }
            # Materialise the per-deployment engine config locally so the
            # container can read it. The leader doesn't have a file path
            # to share; it shipped the YAML body inline.
            cfg_body = plan.get("engine_config_body")
            cfg_container_path = plan.get("engine_config_container_path")
            if cfg_body and cfg_container_path:
                dep_id = plan.get("deployment_id", "remote")
                host_cfg = self._configs_dir / f"{dep_id}.yml"
                host_cfg.write_text(cfg_body)
                volumes[str(self._configs_dir.resolve())] = {
                    "bind": "/berth/configs", "mode": "ro",
                }
            _emit_status(
                self._status_cb,
                "deployment.container_starting",
                name=plan["name"],
                image=plan["image"],
            )
            h = await asyncio.to_thread(
                self._dc.run,
                image=plan["image"],
                name=plan["name"],
                command=command,
                environment=plan["environment"],
                kwargs=_rehydrate_docker_kwargs(plan["kwargs"]),
                volumes=volumes,
                internal_port=plan["internal_port"],
            )
            _emit_status(
                self._status_cb,
                "deployment.container_started",
                name=plan["name"],
                container_id=h.id,
                address=h.address,
                port=h.port,
            )
            return (h.id, h.address, h.port)

        # Local-style plan — pass through unchanged.
        _emit_status(
            self._status_cb,
            "deployment.container_starting",
            name=plan["name"],
            image=plan["image"],
        )
        h = await asyncio.to_thread(
            self._dc.run,
            image=plan["image"],
            name=plan["name"],
            command=plan["command"],
            environment=plan["environment"],
            kwargs=plan["kwargs"],
            volumes=plan["volumes"],
            internal_port=plan["internal_port"],
        )
        _emit_status(
            self._status_cb,
            "deployment.container_started",
            name=plan["name"],
            container_id=h.id,
            address=h.address,
            port=h.port,
        )
        return (h.id, h.address, h.port)

    async def stop(self, cid, *, remove):
        await asyncio.to_thread(self._dc.stop, cid, remove=remove)

    def stream_logs_iter(self, container_id: str, *, tail: int, follow: bool):
        """Sync iterator (called from a worker thread by _run_log_stream)."""
        return self._dc.stream_logs(container_id, follow=follow, tail=tail)


class _HttpxAdapter:
    """Adapter that hands the dispatcher an async iterator of HTTP chunks."""

    def __init__(self):
        from berth.cluster.agent_link import ENGINE_TIMEOUT
        self._client = httpx.AsyncClient(timeout=ENGINE_TIMEOUT)

    async def stream(self, method, url, headers, body):
        async def gen():
            async with self._client.stream(
                method, url, headers=headers, content=body,
            ) as resp:
                first = True
                async for chunk in resp.aiter_raw():
                    yield (
                        resp.status_code if first else None,
                        dict(resp.headers) if first else None,
                        chunk,
                        False,
                    )
                    first = False
                yield (
                    resp.status_code if first else None,
                    dict(resp.headers) if first else None,
                    b"",
                    True,
                )
        return gen()


def _load_agent_config(berth_home: Path) -> dict:
    p = berth_home / "agent.yaml"
    if not p.exists():
        raise FileNotFoundError(
            f"agent config not found at {p}; run `berth agent register` first"
        )
    with p.open() as f:
        return yaml.safe_load(f)


def _build_ssl_context(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(cfg["ca_cert_path"]))
    ctx.load_cert_chain(
        certfile=str(cfg["agent_cert_path"]),
        keyfile=str(cfg["agent_key_path"]),
    )
    return ctx


async def run_agent(
    berth_home: Path,
    *,
    status_cb: StatusCallback | None = None,
    quiet_downloads: bool = False,
) -> None:
    """Run the agent loop forever: connect, handshake, dispatch, reconnect."""
    from berth import __version__ as _v
    from berth.cluster.host_info import collect_host_info
    from berth.lifecycle.docker_client import DockerClient

    cfg = _load_agent_config(berth_home)
    ssl_ctx = _build_ssl_context(cfg)
    _emit_status(
        status_cb,
        "agent.initializing",
        leader=cfg["leader_url"],
        node_id=cfg.get("node_id"),
    )
    dc = DockerClient(network_name="berth-engines")
    # Engine containers attach to a named bridge network the leader uses
    # by default. The leader's daemon ensures this at startup; the agent
    # must do the same on its own host, otherwise the first remote
    # deploy errors with "network berth-engines not found".
    try:
        dc.ensure_network()
    except Exception as e:
        log.warning("agent ensure_network failed (continuing): %s", e)
        _emit_status(status_cb, "agent.warning", message=f"Docker network: {e}")
    docker = _DockerAdapter(
        dc,
        status_cb=status_cb,
        quiet_downloads=quiet_downloads,
    )
    http = _HttpxAdapter()

    # Agent-process collectors. Populated as the agent serves
    # leader-proxied requests via AgentFrameDispatcher; instrumentation
    # of the dispatcher's request path is a follow-up. GPU stats already
    # flow via build_snapshot regardless.
    agent_in_flight = InFlightCounter()
    agent_latency = LatencyRecorder()
    agent_deployment_models: dict[int, str] = {}

    # Re-attach to running engine containers from a previous agent
    # process. The dispatcher's _endpoints dict is in-memory only — if
    # the agent restarted while remote deployments were live, those
    # rows on the leader stay 'ready' but every /v1/* proxy fails with
    # no-endpoint. Walk docker on startup and rebuild the map.
    preloaded_endpoints: dict[str, tuple[str, int]] = {}
    try:
        for c in dc._client.containers.list(filters={"name": "berth-"}):
            if c.status != "running":
                continue
            ports = (c.attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
            host_port: int | None = None
            host_addr = "127.0.0.1"
            for _internal, bindings in ports.items():
                if not bindings:
                    continue
                try:
                    host_port = int(bindings[0]["HostPort"])
                    host_addr = bindings[0].get("HostIp") or "127.0.0.1"
                    if host_addr in ("0.0.0.0", ""):  # nosec
                        host_addr = "127.0.0.1"
                    break
                except (KeyError, TypeError, ValueError):
                    continue
            if host_port is not None:
                preloaded_endpoints[c.id] = (host_addr, host_port)
                log.info(
                    "agent re-attached endpoint for container %s (%s:%d)",
                    c.id[:12], host_addr, host_port,
                )
                _emit_status(
                    status_cb,
                    "agent.endpoint_reattached",
                    container_id=c.id,
                    address=host_addr,
                    port=host_port,
                )
    except Exception as e:
        log.warning("agent endpoint re-attach failed (continuing): %s", e)
        _emit_status(
            status_cb,
            "agent.warning",
            message=f"endpoint re-attach: {e}",
        )

    ws_url = (
        cfg["leader_url"]
        .replace("https://", "wss://")
        .replace("http://", "ws://")
        .rstrip("/")
        + "/cluster/agent"
    )
    backoff = 1.0
    while True:
        try:
            _emit_status(
                status_cb,
                "agent.connecting",
                leader=cfg["leader_url"],
                node_id=cfg.get("node_id"),
            )
            async with websockets.connect(ws_url, ssl=ssl_ctx) as ws:
                backoff = 1.0
                sender = SerializedSender(ws.send)

                disp = AgentFrameDispatcher(
                    docker=docker, http=http, send=sender.send,
                )
                # Restore endpoints discovered at startup so existing
                # remote deployments answer immediately after a reconnect.
                for _cid, (_addr, _port) in preloaded_endpoints.items():
                    disp.register_endpoint(
                        container_id=_cid, address=_addr, port=_port,
                    )
                info = collect_host_info()
                await sender.send(encode_frame(Hello(
                    agent_version=_v,
                    host_info={
                        "cpu_count": info.cpu_count,
                        "total_ram_mb": info.total_ram_mb,
                        "gpu_count": info.gpu_count,
                        "total_vram_mb": info.total_vram_mb,
                        "gpus": [g.__dict__ for g in info.gpus],
                    },
                )))
                welcome = decode_frame(await ws.recv())
                if not isinstance(welcome, Welcome):
                    raise RuntimeError(
                        f"unexpected handshake reply: {welcome}"
                    )
                log.info(
                    "agent connected to leader as node_id=%s",
                    welcome.node_id,
                )
                _emit_status(
                    status_cb,
                    "agent.connected",
                    leader=cfg["leader_url"],
                    node_id=welcome.node_id,
                )

                _adopted = adopted_mod.load(berth_home)
                if _adopted:
                    register_adopted_endpoints(disp, _adopted)
                    await sender.send(encode_frame(
                        build_adopted_report(_adopted, alive_by_cid={})))

                import time as _t
                agent_started_at = _t.time()

                async def heartbeat(
                    started_at: float = agent_started_at,
                    sender: SerializedSender = sender,
                ):
                    while True:
                        frame = build_heartbeat_frame(
                            in_flight=agent_in_flight,
                            latency=agent_latency,
                            deployment_models=agent_deployment_models,
                            uptime_s=_t.time() - started_at,
                        )
                        await sender.send(encode_frame(frame))
                        await asyncio.sleep(5.0)

                hb = asyncio.create_task(heartbeat())

                async def watch_adopted(sender=sender, disp=disp):
                    from watchfiles import awatch
                    async for _ in awatch(str(berth_home)):
                        entries = adopted_mod.load(berth_home)
                        register_adopted_endpoints(disp, entries)
                        await sender.send(encode_frame(
                            build_adopted_report(entries, alive_by_cid={})))
                wa = asyncio.create_task(watch_adopted())

                try:
                    async for raw in ws:
                        try:
                            frame = decode_frame(raw)
                        except ValueError as e:
                            log.warning("dropping unknown frame: %s", e)
                            continue
                        if isinstance(frame, Heartbeat):
                            continue
                        await disp.handle(frame)
                finally:
                    hb.cancel()
                    with suppress(asyncio.CancelledError):
                        await hb
                    wa.cancel()
                    with suppress(asyncio.CancelledError):
                        await wa
        except (OSError, websockets.WebSocketException) as e:
            log.warning(
                "agent connection lost: %s; reconnecting in %.1fs",
                e, backoff,
            )
            _emit_status(
                status_cb,
                "agent.reconnecting",
                error=str(e) or e.__class__.__name__,
                delay_s=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
