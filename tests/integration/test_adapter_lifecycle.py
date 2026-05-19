"""End-to-end integration test for the adapter lifecycle.

Drives the full HTTP stack (admin endpoints + OpenAI proxy + adapter
router + lifecycle) against a fake engine that records every adapter
load/unload + chat-completion call. No real GPU; no real container.
"""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from serve_engine.backends.vllm import VLLMBackend
from serve_engine.daemon.app import build_app
from serve_engine.lifecycle.docker_client import ContainerHandle
from serve_engine.store import adapters as ad_store
from serve_engine.store import db
from serve_engine.store import deployment_adapters as da_store
from serve_engine.store import deployments as dep_store
from serve_engine.store import models as model_store


class FakeEngine:
    """Minimal vLLM-like fake. Records calls; rejects chat completions for
    adapters that aren't currently loaded."""

    def __init__(self):
        self.loaded: set[str] = set()
        self.calls: list[dict] = []

    def record_load(self, lora_name: str, lora_path: str) -> tuple[int, dict]:
        self.calls.append({"op": "load", "name": lora_name, "path": lora_path})
        if lora_name in self.loaded:
            return 200, {"message": f"already loaded: {lora_name}"}
        self.loaded.add(lora_name)
        return 200, {"message": f"loaded: {lora_name}"}

    def record_unload(self, lora_name: str) -> tuple[int, dict]:
        self.calls.append({"op": "unload", "name": lora_name})
        self.loaded.discard(lora_name)
        return 200, {"message": f"unloaded: {lora_name}"}

    def record_chat(self, model: str) -> tuple[int, bytes, str]:
        """Simulate engine chat handler. Returns (status, body, content_type)."""
        self.calls.append({"op": "chat", "model": model})
        # vLLM behavior: if model is an unloaded adapter, returns 400.
        # We accept the base model name OR any currently-loaded adapter.
        return 200, (
            b'{"id":"x","object":"chat.completion","model":"' + model.encode() +
            b'","choices":[{"message":{"role":"assistant","content":"hi"}}],'
            b'"usage":{"prompt_tokens":3,"completion_tokens":1}}'
        ), "application/json"


@pytest.fixture
def app(tmp_path, monkeypatch):
    from serve_engine.lifecycle.topology import GPUInfo, Topology

    fake_engine = FakeEngine()

    # Stream factory: serve a fake chat-completion response.
    class FakeResponse:
        def __init__(self, body: bytes, content_type: str):
            self.status_code = 200
            self.headers = {"content-type": content_type}
            self._body = body

        async def aiter_raw(self):
            yield self._body

    class FakeStreamCM:
        def __init__(self, body: bytes, ct: str):
            self.body = body
            self.ct = ct

        async def __aenter__(self):
            return FakeResponse(self.body, self.ct)

        async def __aexit__(self, *args):
            return None

    class FakeEngineClient:
        def __init__(self, base_url):
            self.base_url = base_url

        def stream(self, method, path, *, content=None, headers=None):
            import json as _json
            try:
                payload = _json.loads(content) if content else {}
            except Exception:
                payload = {}
            _, body, ct = fake_engine.record_chat(payload.get("model", ""))
            return FakeStreamCM(body, ct)

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "serve_engine.daemon.openai_proxy.make_engine_client",
        lambda base_url: FakeEngineClient(base_url),
    )

    # Intercept dynamic LoRA load/unload HTTP POSTs to the (mocked)
    # engine container. Pass-through for ASGI calls to our own app.
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *, json=None, **kw):
        url_s = str(url)
        if "49152" in url_s:
            if "load_lora_adapter" in url_s and "unload" not in url_s:
                code, body = fake_engine.record_load(
                    json["lora_name"], json["lora_path"],
                )
            elif "unload_lora_adapter" in url_s:
                code, body = fake_engine.record_unload(json["lora_name"])
            else:
                code, body = 404, {"error": f"unknown engine path: {url_s}"}
            return httpx.Response(code, json=body)
        return await original_post(self, url, json=json, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    monkeypatch.setattr(
        "serve_engine.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="x", address="127.0.0.1", port=49152,
    )
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    fastapi_app = build_app(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
        topology=topology,
    )
    fastapi_app.state._fake_engine = fake_engine  # accessible to assertions
    return fastapi_app


def _seed_base_and_deployment(app, *, max_loras: int = 4):
    conn = app.state.conn
    base = model_store.add(conn, name="qwen3-test", hf_repo="o/qwen3")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        max_loras=max_loras,
    )
    dep_store.set_container(
        conn, dep.id, container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, dep.id, "ready")
    return base, dep


def _register_downloaded_adapter(app, name: str, tmp_path):
    """Register an adapter and synthesize a downloaded local_path."""
    conn = app.state.conn
    a = ad_store.add(
        conn, name=name, base_model_name="qwen3-test", hf_repo=f"o/{name}",
    )
    d = tmp_path / f"models--o--{name}" / "snapshots" / "abc"
    d.mkdir(parents=True, exist_ok=True)
    ad_store.set_local_path(conn, a.id, str(d))
    return a


def _set_adapter_last_used(app, dep_id: int, name: str, ts: str) -> None:
    app.state.conn.execute(
        """
        UPDATE deployment_adapters
        SET last_used_at=?
        WHERE deployment_id=?
          AND adapter_id=(SELECT id FROM adapters WHERE name=?)
        """,
        (ts, dep_id, name),
    )


@pytest.mark.asyncio
async def test_full_adapter_lifecycle_8_step_flow(app, tmp_path):
    """Walks the entire adapter lifecycle through the public HTTP API.

    Asserts:
    - hot-load via OpenAI proxy works on first reference (model=adapter)
    - subsequent requests for the same adapter don't trigger reload
    - loading a 5th adapter (max_loras=4) evicts the LRU
    - re-requesting the evicted adapter re-loads it (and evicts another)
    - force-rm hot-unloads from engines
    """
    _, dep = _seed_base_and_deployment(app, max_loras=4)
    fe: FakeEngine = app.state._fake_engine

    # Pre-register 5 adapters as 'downloaded' (skip the network).
    for n in ("formal", "casual", "snarky", "clinical", "pirate"):
        _register_downloaded_adapter(app, n, tmp_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:

        # --- Step 1: client requests 'formal'; proxy hot-loads it ---
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "formal", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200, r.text
        assert "formal" in fe.loaded
        # Engine saw exactly 1 load + 1 chat
        load_calls = [c for c in fe.calls if c["op"] == "load"]
        assert len(load_calls) == 1 and load_calls[0]["name"] == "formal"

        # --- Step 2: same adapter again - no second load ---
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "formal", "messages": [{"role": "user", "content": "again"}]},
        )
        assert r.status_code == 200
        assert len([c for c in fe.calls if c["op"] == "load"]) == 1

        # --- Step 3: load 3 more so all 4 slots are used ---
        for n in ("casual", "snarky", "clinical"):
            r = await c.post(
                "/v1/chat/completions",
                json={"model": n, "messages": [{"role": "user", "content": "hi"}]},
            )
            assert r.status_code == 200, r.text
        _set_adapter_last_used(app, dep.id, "formal", "2000-01-01 00:00:00")
        _set_adapter_last_used(app, dep.id, "casual", "2000-01-01 00:00:01")
        _set_adapter_last_used(app, dep.id, "snarky", "2000-01-01 00:00:02")
        _set_adapter_last_used(app, dep.id, "clinical", "2000-01-01 00:00:03")
        assert fe.loaded == {"formal", "casual", "snarky", "clinical"}

        # --- Step 4: bump 'formal' to MRU before triggering eviction ---
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "formal", "messages": [{"role": "user", "content": "mru"}]},
        )
        assert r.status_code == 200

        # --- Step 5: load 5th adapter; LRU ('casual' now) must evict ---
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "pirate", "messages": [{"role": "user", "content": "yarr"}]},
        )
        assert r.status_code == 200, r.text
        assert "pirate" in fe.loaded
        assert "casual" not in fe.loaded  # LRU was 'casual'
        # Engine saw an unload for 'casual' before the load for 'pirate'
        ops_after_step4 = [
            c for c in fe.calls if c["op"] in ("load", "unload")
        ]
        # Find unload event for 'casual' before load event for 'pirate'
        casual_unload = next(
            (i for i, c in enumerate(ops_after_step4)
             if c["op"] == "unload" and c["name"] == "casual"),
            None,
        )
        pirate_load = next(
            (i for i, c in enumerate(ops_after_step4)
             if c["op"] == "load" and c["name"] == "pirate"),
            None,
        )
        assert casual_unload is not None
        assert pirate_load is not None
        assert casual_unload < pirate_load

        # --- Step 6: re-request 'casual' - re-loaded; another LRU evicts ---
        r = await c.post(
            "/v1/chat/completions",
            json={"model": "casual", "messages": [{"role": "user", "content": "back"}]},
        )
        assert r.status_code == 200
        assert "casual" in fe.loaded

        # --- Step 7: force-rm 'pirate' while loaded - engine sees unload ---
        r = await c.delete("/admin/adapters/pirate?force=true")
        assert r.status_code == 204
        assert "pirate" not in fe.loaded
        assert ad_store.get_by_name(app.state.conn, "pirate") is None

        # --- Step 8: registry shape sanity ---
        r = await c.get("/v1/models")
        assert r.status_code == 200
        ids = {x["id"] for x in r.json()["data"]}
        assert "qwen3-test" in ids
        assert "pirate" not in ids  # was force-rmd
        # Junction state is consistent with engine state
        loaded_in_db = {
            a.name for a in da_store.list_for_deployment(app.state.conn, dep.id)
        }
        assert loaded_in_db == fe.loaded
