"""Pure-resolution tests for adapter_router. The async ensure_adapter_loaded
helper is exercised in tests/unit/test_admin_adapter_endpoints.py and
tests/unit/test_proxy_adapter_dispatch.py."""
import pytest

from berth.lifecycle.adapter_router import (
    UnknownModel,
    find_deployment_for,
    rank_deployments_for,
    resolve_target,
)
from berth.routing.scorer import NodeSignals, RoutingRequest
from berth.store import adapters as ad_store
from berth.store import db
from berth.store import deployment_adapters as da_store
from berth.store import deployments as dep_store
from berth.store import models as model_store


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


# ---- resolve_target ----

def test_resolve_target_bare_base_unchanged(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    t = resolve_target(conn, "qwen3-7b")
    assert t.base_model_name == "qwen3-7b"
    assert t.adapter_name is None


def test_resolve_target_bare_adapter_returns_base_and_adapter(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    ad_store.add(
        conn, name="tone-formal", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    t = resolve_target(conn, "tone-formal")
    assert t.base_model_name == "qwen3-7b"
    assert t.adapter_name == "tone-formal"


def test_resolve_target_composite_form(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    ad_store.add(
        conn, name="tone-formal", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    t = resolve_target(conn, "qwen3-7b:tone-formal")
    assert t.base_model_name == "qwen3-7b"
    assert t.adapter_name == "tone-formal"


def test_resolve_target_composite_with_wrong_base_rejected(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    model_store.add(conn, name="llama-8b", hf_repo="meta/L8B")
    ad_store.add(
        conn, name="tone-formal", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    with pytest.raises(UnknownModel, match="belongs to base"):
        resolve_target(conn, "llama-8b:tone-formal")


def test_resolve_target_composite_unknown_adapter_rejected(tmp_path):
    conn = _fresh(tmp_path)
    model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    with pytest.raises(UnknownModel):
        resolve_target(conn, "qwen3-7b:nope")


def test_resolve_target_unknown_bare_falls_through_as_base(tmp_path):
    """Bare names that don't match an adapter return as a base candidate;
    find_deployment_for handles 'no such base' via returning None."""
    conn = _fresh(tmp_path)
    t = resolve_target(conn, "totally-unknown")
    assert t.base_model_name == "totally-unknown"
    assert t.adapter_name is None


# ---- find_deployment_for ----

def _seed_dep(conn, *, model_id: int, max_loras: int = 0, status: str = "ready"):
    d = dep_store.create(
        conn, model_id=model_id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        max_loras=max_loras,
    )
    dep_store.set_container(
        conn, d.id, container_id=f"c{d.id}", container_name=f"x{d.id}",
        container_port=49152 + d.id, container_address="127.0.0.1",
    )
    dep_store.update_status(conn, d.id, status)
    return d


def test_find_deployment_for_bare_base(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    d = _seed_dep(conn, model_id=base.id)
    found = find_deployment_for(conn, "qwen3-7b", None)
    assert found is not None
    assert found.id == d.id


def test_find_deployment_for_adapter_prefers_already_loaded(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    a = ad_store.add(
        conn, name="x", base_model_name="qwen3-7b", hf_repo="o/lora",
    )
    _seed_dep(conn, model_id=base.id, max_loras=4)  # candidate without adapter
    d_with_adapter = _seed_dep(conn, model_id=base.id, max_loras=4)
    da_store.attach(conn, d_with_adapter.id, a.id)
    found = find_deployment_for(conn, "qwen3-7b", "x")
    assert found is not None
    assert found.id == d_with_adapter.id


def test_find_deployment_for_adapter_with_free_slot_picks_freer_over_full(tmp_path):
    """Between deployment with free slots and deployment that would need
    eviction, prefer the one with free slots."""
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    other_a = ad_store.add(
        conn, name="other", base_model_name="qwen3-7b", hf_repo="o/o",
    )
    target_a = ad_store.add(
        conn, name="target", base_model_name="qwen3-7b", hf_repo="o/t",
    )
    d_full = _seed_dep(conn, model_id=base.id, max_loras=1)
    da_store.attach(conn, d_full.id, other_a.id)  # slot now full
    d_free = _seed_dep(conn, model_id=base.id, max_loras=4)
    found = find_deployment_for(conn, "qwen3-7b", target_a.name)
    assert found is not None
    assert found.id == d_free.id


def test_find_deployment_for_adapter_skips_lora_disabled_deployment(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    ad_store.add(conn, name="x", base_model_name="qwen3-7b", hf_repo="o/lora")
    _seed_dep(conn, model_id=base.id, max_loras=0)  # no LoRA
    found = find_deployment_for(conn, "qwen3-7b", "x")
    assert found is None


def test_find_deployment_for_unknown_adapter_returns_none(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    _seed_dep(conn, model_id=base.id, max_loras=4)
    found = find_deployment_for(conn, "qwen3-7b", "no-such-adapter")
    assert found is None


def test_find_deployment_for_no_ready_returns_none(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="qwen3-7b", hf_repo="o/qwen")
    _seed_dep(conn, model_id=base.id, max_loras=4, status="stopped")
    ad_store.add(conn, name="x", base_model_name="qwen3-7b", hf_repo="o/lora")
    found = find_deployment_for(conn, "qwen3-7b", "x")
    assert found is None


# ---- rank_deployments_for ----


def _seed_dep_on_node(conn, *, model_id: int, node_id: int, vram_mb: int = 8000):
    d = dep_store.create(
        conn, model_id=model_id, backend="vllm", image_tag="vllm:test",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
        vram_reserved_mb=vram_mb,
    )
    dep_store.set_container(
        conn, d.id,
        container_id=f"c{d.id}", container_name=f"x{d.id}",
        container_port=49152 + d.id, container_address="127.0.0.1",
        node_id=node_id,
    )
    dep_store.update_status(conn, d.id, "ready")
    return d


def test_rank_deployments_for_orders_by_scorer(tmp_path):
    """Three deployments of the same base on three nodes. Scorer prefers
    lower in_flight; the ranked order matches."""
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="test-base", hf_repo="o/x")
    d10 = _seed_dep_on_node(conn, model_id=base.id, node_id=10)
    d11 = _seed_dep_on_node(conn, model_id=base.id, node_id=11)
    d12 = _seed_dep_on_node(conn, model_id=base.id, node_id=12)
    signals = {
        10: NodeSignals(node_id=10, mem_free_mb=20000, in_flight=5, latency_p95_ms=200),
        11: NodeSignals(node_id=11, mem_free_mb=20000, in_flight=1, latency_p95_ms=100),
        12: NodeSignals(node_id=12, mem_free_mb=20000, in_flight=3, latency_p95_ms=150),
    }
    ranked = rank_deployments_for(
        conn, base_model_name="test-base", adapter_name=None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert [d.id for d in ranked] == [d11.id, d12.id, d10.id]


def test_rank_deployments_for_keeps_ready_deployments_with_low_free_memory(tmp_path):
    """Ready deployments already paid their VRAM cost at placement time.

    Request routing must not double-count vram_reserved_mb against current
    free memory; engines like vLLM can legitimately reserve most of the GPU
    once they are healthy.
    """
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="test-base", hf_repo="o/x")
    d10 = _seed_dep_on_node(conn, model_id=base.id, node_id=10, vram_mb=8000)
    d11 = _seed_dep_on_node(conn, model_id=base.id, node_id=11, vram_mb=8000)
    d12 = _seed_dep_on_node(conn, model_id=base.id, node_id=12, vram_mb=8000)
    signals = {
        10: NodeSignals(node_id=10, mem_free_mb=20000, in_flight=0, latency_p95_ms=100),
        11: NodeSignals(node_id=11, mem_free_mb=500, in_flight=0, latency_p95_ms=100),
        12: NodeSignals(node_id=12, mem_free_mb=20000, in_flight=0, latency_p95_ms=100),
    }
    ranked = rank_deployments_for(
        conn, base_model_name="test-base", adapter_name=None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert {d.id for d in ranked} == {d10.id, d11.id, d12.id}


def test_find_deployment_for_is_head_of_rank_deployments_for(tmp_path):
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="test-base", hf_repo="o/x")
    _seed_dep_on_node(conn, model_id=base.id, node_id=10)
    _seed_dep_on_node(conn, model_id=base.id, node_id=11)
    signals = {
        10: NodeSignals(node_id=10, mem_free_mb=20000, in_flight=5, latency_p95_ms=200),
        11: NodeSignals(node_id=11, mem_free_mb=20000, in_flight=1, latency_p95_ms=100),
    }
    ranked = rank_deployments_for(
        conn, base_model_name="test-base", adapter_name=None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    head = find_deployment_for(
        conn, "test-base", None,
        signals_by_node=signals,
        request=RoutingRequest(affinity_key=None),
    )
    assert head == ranked[0]


def test_find_deployment_for_legacy_path_when_signals_omitted(tmp_path):
    """Existing callers that don't pass signals must still get a deployment."""
    conn = _fresh(tmp_path)
    base = model_store.add(conn, name="test-base", hf_repo="o/x")
    d10 = _seed_dep_on_node(conn, model_id=base.id, node_id=10)
    found = find_deployment_for(conn, "test-base", None)
    assert found is not None
    assert found.id == d10.id
