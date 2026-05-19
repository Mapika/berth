"""Coverage for the manager's remote-deploy dispatch branch.

These tests run entirely in-process: the AgentRegistry is populated
with a fake AgentLink that records the plan dict and returns a fake
container handle. No docker, no WS, no HF download.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from berth.backends.vllm import VLLMBackend
from berth.cluster.agent_link import StartedContainer
from berth.cluster.agent_registry import AgentRegistry
from berth.cluster.local_bootstrap import ensure_local_node
from berth.lifecycle.manager import (
    LifecycleManager,
    _json_safe_docker_kwargs,
)
from berth.lifecycle.plan import DeploymentPlan
from berth.lifecycle.topology import GPUInfo, Topology
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import nodes as nodes_store


class _FakeLink:
    """Minimal AgentLink stub: records the dispatched plan dict, returns
    a stable StartedContainer."""

    def __init__(self, node_id: int = 2):
        self._node_id = node_id
        self.captured: dict | None = None
        self.stopped: list[str] = []

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def is_ready(self) -> bool:
        return True

    async def start_deployment(self, plan: dict) -> StartedContainer:
        self.captured = plan
        return StartedContainer(
            container_id="cid-remote", address="tunnel", port=0,
        )

    async def stop_deployment(self, container_id: str) -> None:
        self.stopped.append(container_id)

    async def probe_container(self, *, container_id: str, path: str) -> int:
        # Pretend the engine is healthy on first probe.
        return 200


def _bootstrap_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    # Local + remote node rows.
    ensure_local_node(conn, agent_version="test")
    remote_id = nodes_store.insert(
        conn,
        label="gpu-rig-2",
        fingerprint="sha256:test",
        reachable_as=None,
        first_seen=0.0, last_seen=0.0,
        agent_version="test",
        cpu_count=8, total_ram_mb=32000,
        gpu_count=1, total_vram_mb=8192,
    )
    nodes_store.set_status(conn, remote_id, status="ready", last_seen=0.0)
    return conn


@pytest.mark.asyncio
async def test_remote_dispatch_routes_via_agentlink(tmp_path, monkeypatch):
    """A plan with node_label='gpu-rig-2' must dispatch through the link
    and NOT call docker.run on the leader."""
    conn = _bootstrap_conn(tmp_path)
    remote = nodes_store.find_by_label(conn, "gpu-rig-2")
    assert remote is not None
    link = _FakeLink(node_id=remote.id)
    registry = AgentRegistry()
    registry.register(link)
    docker_client = MagicMock()
    docker_client.run = MagicMock(
        side_effect=AssertionError("leader docker must not be called for remote deploy"),
    )

    # Patch model download to a no-op so the test doesn't reach out to HF.
    fake_path = tmp_path / "model"
    fake_path.mkdir()
    # Minimal config.json for KV estimation. Use real Qwen-style shape.
    (fake_path / "config.json").write_text(json.dumps({
        "hidden_size": 1024, "num_attention_heads": 16,
        "num_key_value_heads": 16, "num_hidden_layers": 24,
        "max_position_embeddings": 4096, "vocab_size": 151936,
        "torch_dtype": "bfloat16",
    }))

    async def _fake_download(**_kw):
        return str(fake_path)

    monkeypatch.setattr(
        "berth.lifecycle.manager.download_model_async", _fake_download,
    )

    topology = Topology(
        gpus=[GPUInfo(index=0, name="X", total_mb=24000)],
        _islands={0: frozenset({0})},
    )
    mgr = LifecycleManager(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
        agent_registry=registry,
    )

    plan = DeploymentPlan(
        model_name="qwen-test",
        hf_repo="Qwen/Qwen3-0.6B",
        revision="main",
        backend="vllm",
        image_tag="vllm/vllm-openai:test",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=512,
        node_label="gpu-rig-2",
    )

    dep = await mgr.load(plan)
    assert dep is not None
    # The fake link was invoked.
    assert link.captured is not None
    captured = link.captured
    # Remote-style plan markers.
    assert captured["model_hf_repo"] == "Qwen/Qwen3-0.6B"
    assert captured["model_sentinel"] == "__SERVE_MODEL_PATH__"
    # Argv contains the sentinel (not a leader-side path).
    assert any("__SERVE_MODEL_PATH__" in a for a in captured["command"])
    # Volumes are NOT set (agent constructs them) — leader-side paths must not leak.
    assert "volumes" not in captured
    # docker.run on the leader was not called.
    docker_client.run.assert_not_called()
    # Deployment row is marked ready with the remote node_id.
    final = dep_store.get_by_id(conn, dep.id)
    assert final is not None
    assert final.status == "ready"
    assert final.node_id == remote.id
    assert final.container_id == "cid-remote"


@pytest.mark.asyncio
async def test_remote_dispatch_rejects_unready_node(tmp_path, monkeypatch):
    """A node that's not 'ready' rejects deploys with a clear message."""
    conn = _bootstrap_conn(tmp_path)
    remote = nodes_store.find_by_label(conn, "gpu-rig-2")
    assert remote is not None
    nodes_store.set_status(conn, remote.id, status="unreachable", last_seen=0.0)
    registry = AgentRegistry()  # no link registered
    docker_client = MagicMock()
    mgr = LifecycleManager(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=Topology(
            gpus=[GPUInfo(index=0, name="X", total_mb=24000)],
            _islands={0: frozenset({0})},
        ),
        agent_registry=registry,
    )

    plan = DeploymentPlan(
        model_name="x",
        hf_repo="Qwen/Qwen3-0.6B",
        revision="main",
        backend="vllm",
        image_tag="vllm/vllm-openai:test",
        gpu_ids=[0],
        tensor_parallel=1,
        max_model_len=512,
        node_label="gpu-rig-2",
    )
    with pytest.raises(RuntimeError, match="'unreachable'"):
        await mgr.load(plan)


def test_json_safe_docker_kwargs_round_trip():
    """The Ulimit-bearing kwargs must encode + decode cleanly."""
    from docker.types import Ulimit

    from berth.cluster.agent_client import _rehydrate_docker_kwargs

    original = {
        "device_requests": [
            {"Driver": "nvidia", "device_ids": ["0"], "Capabilities": [["gpu"]]},
        ],
        "ipc_mode": "host",
        "shm_size": "2g",
        "ulimits": [Ulimit(name="memlock", soft=-1, hard=-1)],
    }
    safe = _json_safe_docker_kwargs(original)
    # Survives a real json round-trip.
    wire = json.dumps(safe)
    parsed = json.loads(wire)
    hydrated = _rehydrate_docker_kwargs(parsed)
    u = hydrated["ulimits"][0]
    assert isinstance(u, Ulimit)
    assert u.name == "memlock"
    assert u.soft == -1
    assert u.hard == -1


def test_remote_resolve_unknown_label_raises(tmp_path):
    conn = _bootstrap_conn(tmp_path)
    mgr = LifecycleManager(
        conn=conn,
        docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=None,
    )
    plan = DeploymentPlan(
        model_name="x", hf_repo="r", revision="main",
        backend="vllm", image_tag="t", gpu_ids=[0],
        tensor_parallel=1, max_model_len=512,
        node_label="does-not-exist",
    )
    with pytest.raises(RuntimeError, match="not found"):
        mgr._resolve_target_node_id(plan)


class _DyingLink(_FakeLink):
    """Like _FakeLink but the remote engine never answers the probe."""

    async def probe_container(self, *, container_id: str, path: str) -> int:
        return 0  # connection refused / no response


@pytest.mark.asyncio
async def test_remote_dispatch_marks_failed_when_probe_never_succeeds(
    tmp_path, monkeypatch,
):
    """If the engine never returns 200 to the health probe inside the
    timeout, the deployment row is marked 'failed' and load() raises."""
    monkeypatch.setattr(
        "berth.lifecycle.manager.download_model_async",
        AsyncMock(return_value=str(tmp_path / "w")),
    )
    monkeypatch.setattr(
        "berth.lifecycle.manager.estimate_vram_mb", lambda _: 8000,
    )
    (tmp_path / "w").mkdir(exist_ok=True)
    conn = _bootstrap_conn(tmp_path)
    remote = nodes_store.find_by_label(conn, "gpu-rig-2")
    assert remote is not None
    link = _DyingLink(node_id=remote.id)
    registry = AgentRegistry()
    registry.register(link)
    mgr = LifecycleManager(
        conn=conn,
        docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=None,
        agent_registry=registry,
    )
    plan = DeploymentPlan(
        model_name="llama-1b", hf_repo="org/x", revision="main",
        backend="vllm", image_tag="img:v1", gpu_ids=[0],
        tensor_parallel=1, max_model_len=4096,
        node_label="gpu-rig-2",
    )
    # Trim the probe timeout so the test stays fast — patch the bound
    # method to use 0.5 s instead of the 30 s default.
    real_probe = mgr._remote_probe_until_healthy
    async def quick_probe(link_, cid, path):
        return await real_probe(
            link_, cid, path, timeout_s=0.5, interval_s=0.1,
        )
    monkeypatch.setattr(mgr, "_remote_probe_until_healthy", quick_probe)

    with pytest.raises(RuntimeError, match="never answered"):
        await mgr.load(plan)

    # Row exists and is marked failed.
    from berth.store import deployments as dep_store
    failed = [d for d in dep_store.list_all(conn) if d.status == "failed"]
    assert failed, "expected a failed deployment row after probe timeout"


def test_local_resolve_returns_local_node_id(tmp_path):
    conn = _bootstrap_conn(tmp_path)
    mgr = LifecycleManager(
        conn=conn,
        docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=None,
    )
    plan = DeploymentPlan(
        model_name="x", hf_repo="r", revision="main",
        backend="vllm", image_tag="t", gpu_ids=[0],
        tensor_parallel=1, max_model_len=512,
        node_label=None,
    )
    local_node = nodes_store.find_by_label(conn, "local")
    assert local_node is not None
    assert mgr._resolve_target_node_id(plan) == local_node.id
    plan_local = DeploymentPlan(
        model_name="x", hf_repo="r", revision="main",
        backend="vllm", image_tag="t", gpu_ids=[0],
        tensor_parallel=1, max_model_len=512,
        node_label="local",
    )
    assert mgr._resolve_target_node_id(plan_local) == local_node.id
