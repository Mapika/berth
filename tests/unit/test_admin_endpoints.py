from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from berth.backends.vllm import VLLMBackend
from berth.daemon.app import build_app
from berth.lifecycle.docker_client import ContainerHandle
from berth.store import api_keys as key_store
from berth.store import db
from berth.store import usage_events as usage_store


@pytest.fixture
def app(tmp_path, monkeypatch):
    from berth.lifecycle.topology import GPUInfo, Topology

    monkeypatch.setattr(
        "berth.lifecycle.manager.wait_healthy",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "weights")),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.estimate_vram_mb",
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
    app = build_app(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
        serve_home=tmp_path,
    )
    # Most legacy endpoint tests exercise raw image/extra-arg plumbing.
    # Security-default behavior is covered explicitly below.
    app.state.manager.resolved_cfg = SimpleNamespace(allow_unsafe_deploy_options=True)
    app.state.resolved_cfg = app.state.manager.resolved_cfg
    return app


@pytest.mark.asyncio
async def test_list_deployments_empty(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/deployments")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_deployment(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "llama-1b",
                "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                "image_tag": "vllm/vllm-openai:v0.7.3",
                "gpu_ids": [0],
                "max_model_len": 8192,
            },
        )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "ready"


@pytest.mark.asyncio
async def test_safe_mode_allows_default_vllm_deploy(app):
    app.state.manager.resolved_cfg = SimpleNamespace(allow_unsafe_deploy_options=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "safe-llama",
                "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                "backend": "vllm",
                "gpu_ids": [0],
                "max_model_len": 8192,
            },
        )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_safe_mode_rejects_risky_deploy_options(app):
    from berth.backends.trtllm import TRTLLMBackend

    app.state.manager.resolved_cfg = SimpleNamespace(allow_unsafe_deploy_options=False)
    app.state.backends["trtllm"] = TRTLLMBackend()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "custom-image",
                "hf_repo": "org/model",
                "backend": "vllm",
                "image_tag": "attacker/image:latest",
                "gpu_ids": [0],
            },
        )
        assert r.status_code == 400
        assert "custom engine images are disabled" in r.text

        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "raw-flags",
                "hf_repo": "org/model",
                "backend": "vllm",
                "gpu_ids": [0],
                "extra_args": {"--trust-remote-code": ""},
            },
        )
        assert r.status_code == 400
        assert "extra_args are disabled" in r.text

        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "trt",
                "hf_repo": "org/model",
                "backend": "trtllm",
                "gpu_ids": [0],
            },
        )
        assert r.status_code == 400
        assert "trtllm deployments are disabled" in r.text


@pytest.mark.asyncio
async def test_service_profile_crud_and_deploy(app):
    docker_client = app.state.manager._docker
    captured: dict[str, list[str]] = {}

    def _spy(**kwargs):
        captured["command"] = list(kwargs["command"])
        return ContainerHandle(id="cid", name="x", address="127.0.0.1", port=49152)

    docker_client.run.side_effect = _spy

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/service-profiles",
            json={
                "name": "qwen-chat-small",
                "model_name": "qwen-0_5b",
                "hf_repo": "Qwen/Qwen2.5-0.5B-Instruct",
                "backend": "vllm",
                "gpu_ids": [0],
                "max_model_len": 4096,
                "target_concurrency": 16,
                "extra_args": {"--reasoning-parser": "qwen3"},
            },
        )
        assert r.status_code == 201, r.text
        profile = r.json()
        assert profile["name"] == "qwen-chat-small"
        assert profile["backend"] == "vllm"
        assert profile["tensor_parallel"] == 1
        assert profile["extra_args"] == {"--reasoning-parser": "qwen3"}

        r = await c.get("/admin/service-profiles")
        assert r.status_code == 200
        assert [p["name"] for p in r.json()] == ["qwen-chat-small"]

        r = await c.get("/admin/service-profiles/qwen-chat-small")
        assert r.status_code == 200
        assert r.json()["model_name"] == "qwen-0_5b"

        r = await c.post("/admin/service-profiles/qwen-chat-small/deploy")
        assert r.status_code == 201, r.text
        dep = r.json()
        assert dep["status"] == "ready"
        assert dep["gpu_ids"] == [0]

        r = await c.delete("/admin/service-profiles/qwen-chat-small")
        assert r.status_code == 204

    argv = captured["command"]
    assert argv[argv.index("--served-model-name") + 1] == "qwen-0_5b"
    assert argv[argv.index("--max-num-seqs") + 1] == "16"
    assert argv[argv.index("--reasoning-parser") + 1] == "qwen3"


@pytest.mark.asyncio
async def test_service_profile_duplicate_409(app):
    body = {
        "name": "dup",
        "model_name": "qwen",
        "hf_repo": "org/qwen",
        "backend": "vllm",
        "gpu_ids": [0],
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r1 = await c.post("/admin/service-profiles", json=body)
        r2 = await c.post("/admin/service-profiles", json=body)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_service_route_crud(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/service-profiles",
            json={
                "name": "qwen-service",
                "model_name": "qwen",
                "hf_repo": "org/qwen",
                "backend": "vllm",
                "gpu_ids": [0],
            },
        )
        assert r.status_code == 201, r.text

        r = await c.post(
            "/admin/routes",
            json={
                "name": "public-chat",
                "match_model": "chat",
                "profile_name": "qwen-service",
                "priority": 10,
            },
        )
        assert r.status_code == 201, r.text
        route = r.json()
        assert route["name"] == "public-chat"
        assert route["match_model"] == "chat"
        assert route["profile_name"] == "qwen-service"
        assert route["target_model_name"] == "qwen"
        assert route["priority"] == 10

        r = await c.get("/admin/routes")
        assert r.status_code == 200
        assert [row["name"] for row in r.json()] == ["public-chat"]

        r = await c.get("/admin/routes/public-chat")
        assert r.status_code == 200
        assert r.json()["target_model_name"] == "qwen"

        r = await c.delete("/admin/routes/public-chat")
        assert r.status_code == 204


@pytest.mark.asyncio
async def test_service_route_unknown_profile_404(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/routes",
            json={
                "name": "broken",
                "match_model": "chat",
                "profile_name": "missing",
            },
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_model_rejects_active_deployment(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "llama-1b",
                "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                "image_tag": "vllm/vllm-openai:v0.7.3",
                "gpu_ids": [0],
                "max_model_len": 8192,
            },
        )
        assert r.status_code == 201, r.text

        r = await c.delete("/admin/models/llama-1b")
    assert r.status_code == 409
    assert "must be stopped first" in r.text


@pytest.mark.asyncio
async def test_delete_model_after_stopped_deployment_with_usage_history(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "llama-1b",
                "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                "image_tag": "vllm/vllm-openai:v0.7.3",
                "gpu_ids": [0],
                "max_model_len": 8192,
            },
        )
        assert r.status_code == 201, r.text
        dep_id = r.json()["id"]
        usage_id = usage_store.record(
            app.state.conn,
            model_name="llama-1b",
            base_name="llama-1b",
            deployment_id=dep_id,
        )

        r = await c.delete(f"/admin/deployments/{dep_id}")
        assert r.status_code == 204, r.text
        r = await c.delete("/admin/models/llama-1b")
        assert r.status_code == 204, r.text

    row = app.state.conn.execute(
        "SELECT deployment_id FROM usage_events WHERE id=?", (usage_id,),
    ).fetchone()
    assert row["deployment_id"] is None


@pytest.mark.asyncio
async def test_create_deployment_passes_extra_args_to_argv(app):
    # The fixture's docker_client is a MagicMock; we capture argv via its
    # .run side-effect so we can assert request-body extra_args reach the engine.
    docker_client = app.state.manager._docker  # injected MagicMock
    captured: dict[str, list[str]] = {}

    def _spy(**kwargs):
        captured["command"] = list(kwargs["command"])
        return ContainerHandle(id="cid", name="x", address="127.0.0.1", port=49152)

    docker_client.run.side_effect = _spy

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "qwen36",
                "hf_repo": "Qwen/Qwen3.6-35B-A3B-FP8",
                "image_tag": "vllm/vllm-openai:v0.20.2",
                "gpu_ids": [0],
                "max_model_len": 65536,
                "extra_args": {
                    "--kv-cache-dtype": "fp8_e4m3",
                    "--reasoning-parser": "qwen3",
                    "--enable-expert-parallel": "",
                },
            },
        )
    assert r.status_code == 201, r.text
    argv = captured["command"]
    assert argv[argv.index("--kv-cache-dtype") + 1] == "fp8_e4m3"
    assert argv[argv.index("--reasoning-parser") + 1] == "qwen3"
    bare_idx = argv.index("--enable-expert-parallel")
    if bare_idx + 1 < len(argv):
        assert argv[bare_idx + 1].startswith("--")
    assert "" not in argv


@pytest.mark.asyncio
async def test_predictor_candidates_endpoint(app):
    """/admin/predictor/candidates returns a list of {model, score, reason}.
    With a fresh empty DB the list is empty - the endpoint should not 500."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/predictor/candidates")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_predictor_stats_endpoint(app):
    """/admin/predictor/stats returns the tick-loop counters even before
    the first tick has run."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/predictor/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["preloads_attempted"] == 0
    assert body["preloads_succeeded"] == 0
    assert "enabled" in body


@pytest.mark.asyncio
async def test_create_deployment_409_when_replacing_pinned(app):
    """If a same-name deployment is already pinned, the daemon must return
    a 4xx with the manager's "is pinned" message - not a 500 - so the CLI
    can show the actionable hint ("run `berth unpin <model>`")."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r1 = await c.post(
            "/admin/deployments",
            json={
                "model_name": "llama-1b",
                "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                "image_tag": "vllm/vllm-openai:v0.7.3",
                "gpu_ids": [0], "max_model_len": 8192, "pinned": True,
            },
        )
        assert r1.status_code == 201, r1.text
        r2 = await c.post(
            "/admin/deployments",
            json={
                "model_name": "llama-1b",
                "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                "image_tag": "vllm/vllm-openai:v0.7.3",
                "gpu_ids": [0], "max_model_len": 8192,
            },
        )
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert "is pinned" in body["detail"]
    assert "berth unpin" in body["detail"]


@pytest.mark.asyncio
async def test_list_models(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/admin/models",
            json={"name": "x", "hf_repo": "org/x"},
        )
        r = await c.get("/admin/models")
    assert r.status_code == 200
    names = [m["name"] for m in r.json()]
    assert "x" in names


@pytest.mark.asyncio
async def test_pin_unpin_deployment(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "x",
                "hf_repo": "org/x",
                "image_tag": "img:v1",
                "gpu_ids": [0],
                "max_model_len": 4096,
            },
        )
        dep_id = r.json()["id"]

        r = await c.post(f"/admin/deployments/{dep_id}/pin")
        assert r.status_code == 204

        r = await c.get("/admin/deployments")
        assert r.json()[0]["pinned"] is True

        r = await c.post(f"/admin/deployments/{dep_id}/unpin")
        assert r.status_code == 204
        r = await c.get("/admin/deployments")
        assert r.json()[0]["pinned"] is False


@pytest.mark.asyncio
async def test_pin_404(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as c:
        r = await c.post("/admin/deployments/999/pin")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_deployment_by_id(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "x",
                "hf_repo": "org/x",
                "image_tag": "img:v1",
                "gpu_ids": [0],
                "max_model_len": 4096,
            },
        )
        dep_id = r.json()["id"]
        r = await c.delete(f"/admin/deployments/{dep_id}")
        assert r.status_code == 204
        r = await c.get("/admin/deployments")
        deps = r.json()
        # Deployment row still exists but in stopped status
        assert deps[0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_create_deployment_default_backend_is_vllm(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        r = await c.post(
            "/admin/deployments",
            json={
                "model_name": "x",
                "hf_repo": "org/x",
                "image_tag": "img:v1",
                "gpu_ids": [0],
                "max_model_len": 4096,
                # no `backend` field - should default via selection
            },
        )
    assert r.status_code == 201
    body = r.json()
    assert body["backend"] == "vllm"


@pytest.mark.asyncio
async def test_list_gpus_returns_list(app, monkeypatch):
    from berth.observability.gpu_stats import GPUSnapshot
    monkeypatch.setattr(
        "berth.daemon.admin._read_gpu_stats",
        lambda: [GPUSnapshot(
            index=0, memory_used_mb=10_000, memory_total_mb=80_000,
            gpu_util_pct=42, power_w=350,
        )],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/gpus")
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["index"] == 0
    assert rows[0]["gpu_util_pct"] == 42


@pytest.mark.asyncio
async def test_create_list_revoke_key(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        # The fixture uses the local control app, so it can create the first key.
        r = await c.post("/admin/keys", json={"name": "alice", "tier": "admin"})
        assert r.status_code == 201
        body = r.json()
        assert body["secret"].startswith("sk-")
        kid = body["id"]
        secret = body["secret"]
        auth = {"Authorization": f"Bearer {secret}"}

        # Now a key exists - all subsequent admin requests must carry the bearer
        r = await c.get("/admin/keys", headers=auth)
        assert r.status_code == 200
        names = [k["name"] for k in r.json()]
        assert "alice" in names

        r = await c.delete(f"/admin/keys/{kid}", headers=auth)
        assert r.status_code == 204

        r = await c.get("/admin/keys", headers=auth)
        revoked = [k for k in r.json() if k["id"] == kid]
        assert revoked[0]["revoked"] is True


@pytest.mark.asyncio
async def test_download_model_endpoint(app, monkeypatch, tmp_path):
    """POST /admin/models/{name}/download invokes the downloader."""
    captured = {}

    def fake_download_model(*, hf_repo, revision, cache_dir):
        captured["hf_repo"] = hf_repo
        captured["revision"] = revision
        path = tmp_path / "fake_weights"
        path.mkdir(exist_ok=True)
        return str(path)

    monkeypatch.setattr(
        "berth.lifecycle.downloader.download_model",
        fake_download_model,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        # Register first
        r = await c.post("/admin/models", json={"name": "x", "hf_repo": "org/x"})
        assert r.status_code == 201
        # Trigger download
        r = await c.post("/admin/models/x/download")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "x"
        assert body["local_path"].endswith("fake_weights")
        assert body["already_present"] is False

        # Second call returns already_present=True
        r = await c.post("/admin/models/x/download")
        assert r.json()["already_present"] is True


@pytest.mark.asyncio
async def test_download_unknown_model_404(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/admin/models/no-such/download")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_admin_route_requires_admin_tier(tmp_path, monkeypatch):
    """When non-admin keys exist, admin routes return 403 unless the bearer is admin."""
    from berth.backends.vllm import VLLMBackend
    from berth.daemon.app import build_apps
    from berth.lifecycle.docker_client import ContainerHandle
    from berth.lifecycle.topology import GPUInfo, Topology
    from berth.store import api_keys as _ak
    from berth.store import db as _db

    monkeypatch.setattr(
        "berth.lifecycle.manager.estimate_vram_mb",
        lambda inp: 20_000,
    )
    (tmp_path / "weights").mkdir(exist_ok=True)
    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="x", address="127.0.0.1", port=49152,
    )
    conn = _db.connect(tmp_path / "t.db")
    _db.init_schema(conn)
    # Create keys before constructing the public app; TCP admin routes
    # must require a bearer and then enforce admin tier.
    std_secret, _ = _ak.create(conn, name="user", tier="standard")
    admin_secret, _ = _ak.create(conn, name="root", tier="admin")
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    app, _cluster_app, _uds_app = build_apps(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path, topology=topology,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # No bearer -> 401
        r = await c.get("/admin/models")
        assert r.status_code == 401

        # Standard-tier bearer -> 403
        r = await c.get(
            "/admin/models",
            headers={"Authorization": f"Bearer {std_secret}"},
        )
        assert r.status_code == 403

        # Admin-tier bearer -> 200
        r = await c.get(
            "/admin/models",
            headers={"Authorization": f"Bearer {admin_secret}"},
        )
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_patch_key_allowed_models_success(app):
    """PATCH /admin/keys/{id} updates the key's allowlist and returns 204.

    Round-trips all three states (None, [...], []) through the HTTP layer to
    verify the JSON body's `allowed_models` shape matches what the store
    persists and what GET /admin/keys surfaces.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        # Local control app can create the first key without a bearer.
        r = await c.post("/admin/keys", json={"name": "alice", "tier": "admin"})
        assert r.status_code == 201
        kid = r.json()["id"]
        secret = r.json()["secret"]
        auth = {"Authorization": f"Bearer {secret}"}

        # Default state: unrestricted.
        r = await c.get("/admin/keys", headers=auth)
        assert r.status_code == 200
        row = next(k for k in r.json() if k["id"] == kid)
        assert row["allowed_models"] is None

        # Set an allowlist.
        r = await c.patch(
            f"/admin/keys/{kid}",
            headers=auth,
            json={"allowed_models": ["llama-1b", "qwen-3"]},
        )
        assert r.status_code == 204

        r = await c.get("/admin/keys", headers=auth)
        row = next(k for k in r.json() if k["id"] == kid)
        assert row["allowed_models"] == ["llama-1b", "qwen-3"]

        # Empty list = deny-all; must round-trip through the wire as [].
        r = await c.patch(
            f"/admin/keys/{kid}",
            headers=auth,
            json={"allowed_models": []},
        )
        assert r.status_code == 204
        r = await c.get("/admin/keys", headers=auth)
        row = next(k for k in r.json() if k["id"] == kid)
        assert row["allowed_models"] == []

        # Back to null = unrestricted.
        r = await c.patch(
            f"/admin/keys/{kid}",
            headers=auth,
            json={"allowed_models": None},
        )
        assert r.status_code == 204
        r = await c.get("/admin/keys", headers=auth)
        row = next(k for k in r.json() if k["id"] == kid)
        assert row["allowed_models"] is None


@pytest.mark.asyncio
async def test_patch_key_404_when_missing(app):
    """PATCH on a nonexistent key id returns 404."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        # Local control app can access admin endpoints without a bearer.
        r = await c.patch(
            "/admin/keys/99999",
            json={"allowed_models": ["nope"]},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_key_with_allowed_models(app):
    """POST /admin/keys persists allowed_models when supplied."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
        # First create an admin key for follow-up bearer-auth coverage.
        r = await c.post("/admin/keys", json={"name": "root", "tier": "admin"})
        assert r.status_code == 201
        admin_auth = {"Authorization": f"Bearer {r.json()['secret']}"}

        r = await c.post(
            "/admin/keys",
            headers=admin_auth,
            json={
                "name": "scoped",
                "tier": "standard",
                "allowed_models": ["llama-1b"],
            },
        )
        assert r.status_code == 201
        kid = r.json()["id"]

        # Listing reflects the stored allowlist.
        r = await c.get("/admin/keys", headers=admin_auth)
        row = next(k for k in r.json() if k["id"] == kid)
        assert row["allowed_models"] == ["llama-1b"]


def test_stream_ticket_authorizes_only_stream_routes(app):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from berth.daemon.admin import require_admin_key

    key_store.create(app.state.conn, name="root", tier="admin")
    token, _ = app.state.stream_tokens.issue(path="/admin/events")
    public_app = SimpleNamespace(
        state=SimpleNamespace(
            conn=app.state.conn,
            stream_tokens=app.state.stream_tokens,
            tier_cfg=app.state.tier_cfg,
            local_control_surface=False,
        )
    )
    request = MagicMock()
    request.method = "GET"
    request.scope = {"client": ("127.0.0.1", 12345)}
    request.url.path = "/admin/events"
    request.query_params = {"stream_token": token}
    request.app = public_app

    assert require_admin_key(request) is None
    with pytest.raises(HTTPException) as exc:
        require_admin_key(request)
    assert exc.value.status_code == 401

    token, _ = app.state.stream_tokens.issue(path="/admin/events")
    request.url.path = "/admin/keys"
    request.query_params = {"stream_token": token}
    with pytest.raises(HTTPException) as exc:
        require_admin_key(request)
    assert exc.value.status_code == 401


def test_stream_ticket_is_bound_to_issued_path(app):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from berth.daemon.admin import require_admin_key

    key_store.create(app.state.conn, name="root", tier="admin")
    token, _ = app.state.stream_tokens.issue(path="/admin/events")
    public_app = SimpleNamespace(
        state=SimpleNamespace(
            conn=app.state.conn,
            stream_tokens=app.state.stream_tokens,
            tier_cfg=app.state.tier_cfg,
            local_control_surface=False,
        )
    )
    request = MagicMock()
    request.method = "GET"
    request.scope = {"client": ("127.0.0.1", 12345)}
    request.url.path = "/admin/requests/stream"
    request.query_params = {"stream_token": token}
    request.app = public_app

    with pytest.raises(HTTPException) as exc:
        require_admin_key(request)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_create_stream_token_rejects_non_stream_path(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post("/admin/keys", json={"name": "root", "tier": "admin"})
        assert r.status_code == 201
        admin_auth = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await c.post(
            "/admin/stream-token",
            headers=admin_auth,
            json={"path": "/admin/keys"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_create_stream_token_rejects_malformed_log_stream_path(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post("/admin/keys", json={"name": "root", "tier": "admin"})
        assert r.status_code == 201
        admin_auth = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await c.post(
            "/admin/stream-token",
            headers=admin_auth,
            json={"path": "/admin/deployments/../../keys/logs/stream"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_create_deployment_rejects_bad_model_name(app):
    """model_name lands in the Docker container name and in --served-model-name
    argv. We constrain to ``[A-Za-z0-9][A-Za-z0-9_.-]{0,62}`` at the API
    boundary so a space / slash / shell metachar can't escape into Docker or
    leave half-created DB rows when Docker rejects the name."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        for bad in ["my model", "../escape", "evil$inject", "", "-leading-dash"]:
            r = await c.post(
                "/admin/deployments",
                json={
                    "model_name": bad,
                    "hf_repo": "meta-llama/Llama-3.2-1B-Instruct",
                    "image_tag": "vllm/vllm-openai:v0.7.3",
                    "gpu_ids": [0],
                    "max_model_len": 8192,
                },
            )
            assert r.status_code == 422, f"expected 422 for model_name={bad!r}, got {r.status_code}"


@pytest.mark.asyncio
async def test_delete_node_with_active_deployments_returns_409(app, tmp_path):
    """``DELETE /admin/nodes/<id>`` must refuse if any non-terminal
    deployments still point at the node. Without this guard the
    foreign-key-less ``deployments.node_id`` would orphan deployment
    rows whose stop() path can no longer reach the agent."""
    from berth.store import deployments as dep_store
    from berth.store import models as model_store
    from berth.store import nodes as nodes_store

    conn = app.state.manager._conn
    node = nodes_store.insert(
        conn, label="evil-test", fingerprint="sha256:aaa",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=1, total_ram_mb=1024,
        gpu_count=1, total_vram_mb=80000,
    )
    m = model_store.add(conn, name="m", hf_repo="org/x")
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    dep_store.set_container(
        conn, d.id, container_id="cid", container_name="x",
        container_port=0, container_address="tunnel", node_id=node,
    )
    dep_store.update_status(conn, d.id, "ready")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=10,
    ) as c:
        r = await c.delete(f"/admin/nodes/{node}")
    assert r.status_code == 409
    assert "active deployment" in r.text.lower()
    # Node still exists; deployment still references it.
    assert nodes_store.get(conn, node) is not None

    # After we stop the deployment, the delete succeeds.
    dep_store.update_status(conn, d.id, "stopped")
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=10,
    ) as c:
        r = await c.delete(f"/admin/nodes/{node}")
    assert r.status_code == 200
    assert nodes_store.get(conn, node) is None


@pytest.mark.asyncio
async def test_create_stream_token_returns_path_bound_ticket(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30,
    ) as c:
        r = await c.post("/admin/keys", json={"name": "root", "tier": "admin"})
        assert r.status_code == 201
        admin_auth = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await c.post(
            "/admin/stream-token",
            headers=admin_auth,
            json={"path": "/admin/events"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/admin/events"
    assert body["token"]
