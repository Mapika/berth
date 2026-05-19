"""Admin endpoint tests for adapter lifecycle (Workstream A).

Covers /admin/adapters CRUD + the per-deployment hot-load/unload endpoints.
Engine HTTP calls (load_lora_adapter / unload_lora_adapter on the
container side) are mocked at the httpx layer.
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


@pytest.fixture
def app(tmp_path, monkeypatch):
    from serve_engine.lifecycle.topology import GPUInfo, Topology

    monkeypatch.setattr(
        "serve_engine.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "serve_engine.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "weights")),
    )
    monkeypatch.setattr(
        "serve_engine.lifecycle.manager.estimate_vram_mb",
        lambda inp: 20_000,
    )
    (tmp_path / "weights").mkdir(exist_ok=True)
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
    return build_app(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
        topology=topology,
    )


def _seed_base_and_deployment(app, *, max_loras: int = 4):
    """Helper: register a base model and create a ready vLLM deployment
    with `max_loras` LoRA slots."""
    conn = app.state.conn
    base = model_store.add(conn, name="qwen3-test", hf_repo="o/qwen3")
    dep = dep_store.create(
        conn, model_id=base.id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        max_loras=max_loras,
    )
    dep_store.set_container(
        conn, dep.id,
        container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, dep.id, "ready")
    return base, dep


def _write_local_adapter_dir(root, *, rank: int = 8) -> str:
    """Create a fake on-disk adapter dir with the minimum files vLLM needs."""
    import json
    d = root / "src-adapter"
    d.mkdir()
    (d / "adapter_config.json").write_text(json.dumps({
        "r": rank,
        "target_modules": ["q_proj", "v_proj"],
        "peft_type": "LORA",
    }))
    # vLLM also wants the weights file; a 1-byte placeholder is enough for
    # the registry path - the engine call is mocked out at the httpx layer.
    (d / "adapter_model.safetensors").write_bytes(b"\x00")
    return str(d)


@pytest.mark.asyncio
async def test_add_local_adapter_happy_path(app, tmp_path):
    _seed_base_and_deployment(app)
    src = _write_local_adapter_dir(tmp_path, rank=8)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters/local",
            json={
                "name": "from-disk",
                "base_model_name": "qwen3-test",
                "local_path": src,
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "from-disk"
    assert body["base"] == "qwen3-test"
    assert body["lora_rank"] == 8
    assert body["size_mb"] >= 0
    # local_path must live under the managed cache so the engine's /cache
    # bind-mount can resolve it. tmp_path is the models_dir in the fixture.
    assert body["local_path"].startswith(str(tmp_path / "local-adapters"))
    # Registry row reflects all three setters fired.
    a = ad_store.get_by_name(app.state.conn, "from-disk")
    assert a is not None
    assert a.local_path == body["local_path"]
    assert a.lora_rank == 8
    assert a.hf_repo.startswith("local:")
    assert a.revision == "local"


@pytest.mark.asyncio
async def test_add_local_adapter_rejects_missing_dir(app, tmp_path):
    _seed_base_and_deployment(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters/local",
            json={
                "name": "x", "base_model_name": "qwen3-test",
                "local_path": str(tmp_path / "does-not-exist"),
            },
        )
    assert r.status_code == 400
    assert "not a directory" in r.json()["detail"]


@pytest.mark.asyncio
async def test_add_local_adapter_rejects_missing_config(app, tmp_path):
    _seed_base_and_deployment(app)
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "random.bin").write_bytes(b"\x00")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters/local",
            json={
                "name": "x", "base_model_name": "qwen3-test",
                "local_path": str(bare),
            },
        )
    assert r.status_code == 400
    assert "adapter_config.json" in r.json()["detail"]


@pytest.mark.asyncio
async def test_add_local_adapter_rejects_invalid_rank(app, tmp_path):
    """adapter_config.json with no `r` (or non-positive) is refused so the
    rank pre-flight at hot-load time doesn't get a free pass."""
    import json as _json
    _seed_base_and_deployment(app)
    d = tmp_path / "no-rank"
    d.mkdir()
    (d / "adapter_config.json").write_text(_json.dumps({"peft_type": "LORA"}))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters/local",
            json={
                "name": "x", "base_model_name": "qwen3-test",
                "local_path": str(d),
            },
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_add_local_adapter_name_collision_409(app, tmp_path):
    _seed_base_and_deployment(app)
    src = _write_local_adapter_dir(tmp_path)
    # Pre-existing registry row with same name (via HF path).
    ad_store.add(
        app.state.conn, name="dup", base_model_name="qwen3-test", hf_repo="o/x",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters/local",
            json={
                "name": "dup", "base_model_name": "qwen3-test",
                "local_path": src,
            },
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_add_local_adapter_unknown_base_404(app, tmp_path):
    src = _write_local_adapter_dir(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters/local",
            json={
                "name": "x", "base_model_name": "no-such-base",
                "local_path": src,
            },
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_add_local_adapter_rolls_back_on_copy_failure(app, tmp_path, monkeypatch):
    """If shutil.copytree fails after the registry row is inserted, the row
    must be deleted so a retry isn't blocked by a name collision."""
    import shutil

    _seed_base_and_deployment(app)
    src = _write_local_adapter_dir(tmp_path)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", boom)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters/local",
            json={
                "name": "rollback-me", "base_model_name": "qwen3-test",
                "local_path": src,
            },
        )
    assert r.status_code == 500
    assert ad_store.get_by_name(app.state.conn, "rollback-me") is None


@pytest.mark.asyncio
async def test_create_adapter_happy_path(app):
    _seed_base_and_deployment(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters",
            json={
                "name": "tone-formal",
                "base_model_name": "qwen3-test",
                "hf_repo": "o/lora",
            },
        )
    assert r.status_code == 201, r.text
    assert r.json()["base"] == "qwen3-test"


@pytest.mark.asyncio
async def test_create_adapter_collision_with_base_returns_409(app):
    _seed_base_and_deployment(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters",
            json={
                "name": "qwen3-test",  # collides with base name
                "base_model_name": "qwen3-test",
                "hf_repo": "o/lora",
            },
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_create_adapter_unknown_base_returns_404(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/admin/adapters",
            json={
                "name": "x", "base_model_name": "no-such-base",
                "hf_repo": "o/lora",
            },
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_adapters_includes_loaded_into(app):
    _, dep = _seed_base_and_deployment(app)
    a = ad_store.add(
        app.state.conn, name="tone-formal", base_model_name="qwen3-test",
        hf_repo="o/lora",
    )
    da_store.attach(app.state.conn, dep.id, a.id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/adapters")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["loaded_into"] == [dep.id]
    assert body[0]["downloaded"] is False


@pytest.mark.asyncio
async def test_delete_adapter_refuses_when_loaded_without_force(app):
    _, dep = _seed_base_and_deployment(app)
    a = ad_store.add(
        app.state.conn, name="x", base_model_name="qwen3-test", hf_repo="o/lora",
    )
    da_store.attach(app.state.conn, dep.id, a.id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.delete("/admin/adapters/x")
    assert r.status_code == 409
    # Force succeeds.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.delete("/admin/adapters/x?force=true")
    assert r.status_code == 204
    assert ad_store.get_by_name(app.state.conn, "x") is None


@pytest.mark.asyncio
async def test_hot_load_succeeds_calls_engine_load_endpoint(app, tmp_path, monkeypatch):
    _, dep = _seed_base_and_deployment(app)
    a = ad_store.add(
        app.state.conn, name="x", base_model_name="qwen3-test", hf_repo="o/lora",
    )
    # Simulate downloaded adapter.
    adapter_dir = tmp_path / "models--o--lora" / "snapshots" / "abc"
    adapter_dir.mkdir(parents=True)
    ad_store.set_local_path(app.state.conn, a.id, str(adapter_dir))

    posted = {}
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *, json=None, **kw):
        # Only intercept calls to the (mocked) engine container; let
        # everything else (ASGITransport to our own app) pass through.
        if "49152" in str(url):
            posted["url"] = str(url)
            posted["json"] = json
            return httpx.Response(200, json={"message": "ok"})
        return await original_post(self, url, json=json, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(f"/admin/deployments/{dep.id}/adapters/x")
    assert r.status_code == 201, r.text
    assert "/v1/load_lora_adapter" in posted["url"]
    assert posted["json"]["lora_name"] == "x"
    assert posted["json"]["lora_path"].startswith("/cache/")
    # Junction row was created.
    assert da_store.list_for_deployment(app.state.conn, dep.id)[0].name == "x"


@pytest.mark.asyncio
async def test_hot_load_rejects_undownloaded_adapter(app):
    _, dep = _seed_base_and_deployment(app)
    ad_store.add(
        app.state.conn, name="x", base_model_name="qwen3-test", hf_repo="o/lora",
    )  # NOT downloaded
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(f"/admin/deployments/{dep.id}/adapters/x")
    assert r.status_code == 409
    assert "not downloaded" in r.json()["detail"]


@pytest.mark.asyncio
async def test_hot_load_evicts_lru_when_slots_full(
    app, tmp_path, monkeypatch,
):
    """Deployment with max_loras=2 already has 2 adapters; loading a third
    causes the LRU one to be hot-unloaded first."""
    _, dep = _seed_base_and_deployment(app, max_loras=2)
    conn = app.state.conn

    def _make_loaded_adapter(name: str):
        a = ad_store.add(
            conn, name=name, base_model_name="qwen3-test", hf_repo=f"o/{name}",
        )
        d = tmp_path / f"models--o--{name}" / "snapshots" / "abc"
        d.mkdir(parents=True)
        ad_store.set_local_path(conn, a.id, str(d))
        return a

    a1 = _make_loaded_adapter("first")
    a2 = _make_loaded_adapter("second")
    _make_loaded_adapter("third")  # not pre-attached; loaded by the test
    da_store.attach(conn, dep.id, a1.id)
    da_store.attach(conn, dep.id, a2.id)
    conn.execute(
        """
        UPDATE deployment_adapters
        SET last_used_at=CASE adapter_id
            WHEN ? THEN ?
            WHEN ? THEN ?
            ELSE last_used_at
        END
        WHERE deployment_id=?
        """,
        (
            a1.id, "2000-01-01 00:00:00",
            a2.id, "2000-01-01 00:00:01",
            dep.id,
        ),
    )

    posted_urls: list[str] = []
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *, json=None, **kw):
        if "49152" in str(url):
            posted_urls.append(str(url))
            return httpx.Response(200, json={"message": "ok"})
        return await original_post(self, url, json=json, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(f"/admin/deployments/{dep.id}/adapters/third")
    assert r.status_code == 201, r.text

    # Two POSTs: unload `first` (LRU), then load `third`.
    assert any("/v1/unload_lora_adapter" in u for u in posted_urls)
    assert any("/v1/load_lora_adapter" in u for u in posted_urls)
    # `first` is gone, `third` is loaded.
    names = {a.name for a in da_store.list_for_deployment(conn, dep.id)}
    assert "first" not in names
    assert "third" in names
    assert "second" in names  # not evicted


@pytest.mark.asyncio
async def test_hot_unload_calls_engine_unload(app, monkeypatch, tmp_path):
    _, dep = _seed_base_and_deployment(app)
    a = ad_store.add(
        app.state.conn, name="x", base_model_name="qwen3-test", hf_repo="o/lora",
    )
    d = tmp_path / "models--o--lora" / "snapshots" / "abc"
    d.mkdir(parents=True)
    ad_store.set_local_path(app.state.conn, a.id, str(d))
    da_store.attach(app.state.conn, dep.id, a.id)

    posted = {}
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *, json=None, **kw):
        if "49152" in str(url):
            posted["url"] = str(url)
            posted["json"] = json
            return httpx.Response(200, json={"message": "ok"})
        return await original_post(self, url, json=json, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.delete(f"/admin/deployments/{dep.id}/adapters/x")
    assert r.status_code == 204
    assert "/v1/unload_lora_adapter" in posted["url"]
    assert posted["json"]["lora_name"] == "x"
    assert da_store.list_for_deployment(app.state.conn, dep.id) == []


@pytest.mark.asyncio
async def test_hot_load_against_trtllm_returns_409(app, monkeypatch, tmp_path):
    """TRT-LLM doesn't support hot-load; the lifecycle refuses cleanly."""
    from serve_engine.backends.trtllm import TRTLLMBackend

    # Replace backends with TRT-LLM only.
    app.state.backends = {"trtllm": TRTLLMBackend()}
    base = model_store.add(app.state.conn, name="qwen3-test", hf_repo="o/qwen3")
    dep = dep_store.create(
        app.state.conn, model_id=base.id, backend="trtllm",
        image_tag="trt:test", gpu_ids=[0], tensor_parallel=1,
        max_model_len=4096, dtype="auto", max_loras=0,
    )
    dep_store.set_container(
        app.state.conn, dep.id,
        container_id="cid", container_name="x",
        container_port=49152, container_address="127.0.0.1",
    )
    dep_store.update_status(app.state.conn, dep.id, "ready")
    a = ad_store.add(
        app.state.conn, name="x", base_model_name="qwen3-test", hf_repo="o/lora",
    )
    d = tmp_path / "x"
    d.mkdir()
    ad_store.set_local_path(app.state.conn, a.id, str(d))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(f"/admin/deployments/{dep.id}/adapters/x")
    assert r.status_code == 409
    assert "does not support" in r.json()["detail"]
