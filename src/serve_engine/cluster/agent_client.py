from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import websockets
import yaml

from serve_engine.cluster.protocol import (
    Frame,
    Heartbeat,
    Hello,
    HttpCancel,
    HttpChunk,
    HttpRequest,
    OpResult,
    StartDeployment,
    StopDeployment,
    Welcome,
    decode_frame,
    encode_frame,
)

log = logging.getLogger(__name__)


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
        cid = frame.headers.get("x-serve-container-id")
        endpoint = self._endpoints.get(cid or "")
        if endpoint is None:
            await self._send(encode_frame(HttpChunk(
                stream_id=frame.stream_id, status=502,
                headers={"x-serve-error": "no-endpoint"},
                body_b64="", eof=True,
            )))
            return
        addr, port = endpoint
        body = base64.b64decode(frame.body_b64) if frame.body_b64 else b""
        url = f"http://{addr}:{port}{frame.path}"
        headers = {k: v for k, v in frame.headers.items()
                   if k != "x-serve-container-id"}
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
                    "x-serve-error": "proxy-failed",
                    "x-serve-detail": str(e)[:200],
                },
                body_b64="", eof=True,
            )))
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
    if ulimits and isinstance(ulimits, list) and ulimits and isinstance(ulimits[0], dict):
        out["ulimits"] = [
            Ulimit(name=u.get("name"), soft=u.get("soft"), hard=u.get("hard"))
            for u in ulimits
        ]
    return out


class _DockerAdapter:
    """Adapter from DockerClient's sync API to the awaitables the
    dispatcher expects.

    Handles two plan shapes:

    1. Legacy / local-style: caller pre-built `volumes` (full leader-side
       paths) and `command` argv (with absolute model path inside the
       container). We just hand it to docker.run.

    2. Remote-deploy: caller sends only HF coordinates + a `model_sentinel`
       placeholder in the argv. We download the model into the agent's
       own ~/.serve/models, materialise the engine config file inline if
       supplied, mount both as volumes, and substitute the sentinel with
       the in-container model path before running.
    """

    def __init__(self, dc, *, models_dir: Path | None = None, configs_dir: Path | None = None):
        self._dc = dc
        # Default to the same on-disk layout the leader uses, but rooted
        # at the agent's own SERVE_HOME.
        from serve_engine import config as _cfg
        self._models_dir = models_dir or _cfg.MODELS_DIR
        self._configs_dir = configs_dir or _cfg.CONFIGS_DIR

    async def start(self, plan):
        # Remote-style plan: HF download + sentinel substitution required.
        if "model_hf_repo" in plan:
            from serve_engine.lifecycle.downloader import download_model

            self._models_dir.mkdir(parents=True, exist_ok=True)
            self._configs_dir.mkdir(parents=True, exist_ok=True)

            local_path = await asyncio.to_thread(
                download_model,
                hf_repo=plan["model_hf_repo"],
                revision=plan.get("model_revision", "main"),
                cache_dir=self._models_dir,
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
                    "bind": "/serve/configs", "mode": "ro",
                }
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
            return (h.id, h.address, h.port)

        # Legacy / local-style plan — pass through unchanged.
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
        return (h.id, h.address, h.port)

    async def stop(self, cid, *, remove):
        await asyncio.to_thread(self._dc.stop, cid, remove=remove)


class _HttpxAdapter:
    """Adapter that hands the dispatcher an async iterator of HTTP chunks."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0),
        )

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


def _load_agent_config(serve_home: Path) -> dict:
    p = serve_home / "agent.yaml"
    if not p.exists():
        raise FileNotFoundError(
            f"agent config not found at {p}; run `serve agent register` first"
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


async def run_agent(serve_home: Path) -> None:
    """Run the agent loop forever: connect, handshake, dispatch, reconnect."""
    from serve_engine import __version__ as _v
    from serve_engine.cluster.host_info import collect_host_info
    from serve_engine.lifecycle.docker_client import DockerClient

    cfg = _load_agent_config(serve_home)
    ssl_ctx = _build_ssl_context(cfg)
    docker = _DockerAdapter(DockerClient(network_name="serve-engines"))
    http = _HttpxAdapter()

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
            async with websockets.connect(ws_url, ssl=ssl_ctx) as ws:
                backoff = 1.0

                async def send(s):
                    await ws.send(s)

                disp = AgentFrameDispatcher(
                    docker=docker, http=http, send=send,
                )
                info = collect_host_info()
                await ws.send(encode_frame(Hello(
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

                async def heartbeat():
                    import time as _t
                    while True:
                        await ws.send(encode_frame(Heartbeat(ts=_t.time())))
                        await asyncio.sleep(5.0)

                hb = asyncio.create_task(heartbeat())
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
        except (OSError, websockets.WebSocketException) as e:
            log.warning(
                "agent connection lost: %s; reconnecting in %.1fs",
                e, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
