"""Integration test that exercises the proxy over real HTTP, not ASGI in-process.

This catches transport-layer issues hidden by ASGITransport: HTTP framing,
header parsing, chunked transfer encoding, connection lifetime, etc.
"""

import socket
import threading
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import uvicorn

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.lifecycle.docker_client import ContainerHandle
from berth.lifecycle.topology import GPUInfo, Topology
from berth.store import db


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerThread(threading.Thread):
    def __init__(self, app, port):
        super().__init__(daemon=True)
        self.config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio",
        )
        self.server = uvicorn.Server(self.config)

    def run(self):
        self.server.run()


@contextmanager
def _serve(app, port):
    t = _ServerThread(app, port)
    t.start()
    # Wait for server to come up
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            import time
            time.sleep(0.05)
    try:
        yield
    finally:
        t.server.should_exit = True
        t.join(timeout=5)


def _make_fake_engine_app():
    """Tiny FastAPI app pretending to be the upstream engine."""
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse

    engine = FastAPI()

    @engine.post("/v1/chat/completions")
    async def chat(request: Request):
        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":" world"}}],'
            yield b'"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return engine


@pytest.mark.asyncio
async def test_proxy_round_trip_over_real_http(tmp_path, monkeypatch):
    # 1. Spin up the fake engine on a free port
    engine_port = _free_port()
    engine_app = _make_fake_engine_app()
    # 2. Spin up the berth daemon on another free port
    daemon_port = _free_port()

    # Manager.load is bypassed; we manually seed a "ready" deployment that
    # points at engine_port. That lets the proxy resolve a target without
    # needing Docker or HF.
    monkeypatch.setattr(
        "berth.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "w")),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.estimate_vram_mb",
        lambda inp: 20_000,
    )
    (tmp_path / "w").mkdir(exist_ok=True)

    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="engine", address="127.0.0.1", port=engine_port,
    )
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    daemon_app = build_app(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path, topology=topology,
    )

    with _serve(engine_app, engine_port), _serve(daemon_app, daemon_port):
        async with httpx.AsyncClient(timeout=10) as c:
            # Register a deployment that targets the running fake engine
            r = await c.post(
                f"http://127.0.0.1:{daemon_port}/admin/deployments",
                    json={
                        "model_name": "llama-1b",
                        "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                        "gpu_ids": [0],
                        "max_model_len": 8192,
                    },
            )
            assert r.status_code == 201

            # Now do the real proxy call over actual TCP
            async with c.stream(
                "POST", f"http://127.0.0.1:{daemon_port}/v1/chat/completions",
                json={
                    "model": "llama-1b",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as r:
                assert r.status_code == 200
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
            assert b"hello" in body
            assert b" world" in body
            assert b"[DONE]" in body
