"""Tests for image-digest capture.

`deployments.image_tag` (e.g. `vllm/vllm-openai:v0.20.2`) is a mutable
pointer. If upstream retags, reproducibility breaks. The lifecycle manager
records the docker image's content-addressable id (`sha256:...`) at
container start so the row carries an immutable reference to what actually
ran. These tests pin that contract end-to-end.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from berth.backends.vllm import VLLMBackend
from berth.lifecycle.docker_client import ContainerHandle
from berth.lifecycle.manager import LifecycleManager
from berth.lifecycle.plan import DeploymentPlan
from berth.lifecycle.topology import GPUInfo, Topology
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store


def _fresh(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


def test_image_digest_defaults_to_none(tmp_path):
    """Brand-new rows have no digest until the engine actually starts."""
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    refreshed = dep_store.get_by_id(conn, d.id)
    assert refreshed.image_digest is None


def test_set_image_digest_round_trip(tmp_path):
    """The setter persists the digest and get_by_id reads it back unchanged."""
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    digest = "sha256:" + "a" * 64
    dep_store.set_image_digest(conn, d.id, digest)
    refreshed = dep_store.get_by_id(conn, d.id)
    assert refreshed.image_digest == digest


def test_set_image_digest_is_overwritable(tmp_path):
    """A subsequent load against the same row replaces the prior digest -
    this matches the reality that set_container overwrites too."""
    conn = _fresh(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    d = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=8192, dtype="auto",
    )
    dep_store.set_image_digest(conn, d.id, "sha256:" + "1" * 64)
    dep_store.set_image_digest(conn, d.id, "sha256:" + "2" * 64)
    assert dep_store.get_by_id(conn, d.id).image_digest == "sha256:" + "2" * 64


# -- manager-level coverage --------------------------------------------------


def _make_plan() -> DeploymentPlan:
    return DeploymentPlan(
        model_name="llama-1b",
        hf_repo="meta-llama/Llama-3.2-1B-Instruct",
        revision="main",
        backend="vllm",
        image_tag="vllm/vllm-openai:v0.7.3",
        gpu_ids=[0],
        max_model_len=8192,
    )


@pytest.fixture
def topo_one_gpu():
    return Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )


def _patch_externals(monkeypatch, tmp_path, vram_mb=20_000):
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
        lambda inp: vram_mb,
    )
    (tmp_path / "weights").mkdir(exist_ok=True)


def test_load_persists_image_digest_from_docker(tmp_path, monkeypatch, topo_one_gpu):
    """After a successful load the row carries the digest the docker client
    reported - not the (mutable) image_tag."""
    conn = _fresh(tmp_path)
    expected_digest = "sha256:" + "f" * 64

    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="vllm-llama-1b", address="127.0.0.1", port=49152,
    )
    docker_client.container_image_id.return_value = expected_digest
    _patch_externals(monkeypatch, tmp_path, vram_mb=20_000)

    mgr = LifecycleManager(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
        topology=topo_one_gpu,
    )
    model_store.add(conn, name="llama-1b", hf_repo="meta-llama/Llama-3.2-1B-Instruct")
    dep = asyncio.run(mgr.load(_make_plan()))

    assert dep.image_digest == expected_digest
    docker_client.container_image_id.assert_called_once_with("cid")


def test_load_tolerates_missing_image_digest(tmp_path, monkeypatch, topo_one_gpu):
    """If the docker client can't resolve the digest, the load still
    succeeds - image_digest is best-effort observability, not a gate."""
    conn = _fresh(tmp_path)

    docker_client = MagicMock()
    docker_client.run.return_value = ContainerHandle(
        id="cid", name="vllm-llama-1b", address="127.0.0.1", port=49152,
    )
    docker_client.container_image_id.return_value = None
    _patch_externals(monkeypatch, tmp_path, vram_mb=20_000)

    mgr = LifecycleManager(
        conn=conn, docker_client=docker_client,
        backends={"vllm": VLLMBackend()}, models_dir=tmp_path,
        topology=topo_one_gpu,
    )
    model_store.add(conn, name="llama-1b", hf_repo="meta-llama/Llama-3.2-1B-Instruct")
    dep = asyncio.run(mgr.load(_make_plan()))

    assert dep.status == "ready"
    assert dep.image_digest is None
