from __future__ import annotations

from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_managed_deployment_source_defaults_to_managed(tmp_path):
    conn = _conn(tmp_path)
    m = model_store.add(conn, name="m", hf_repo="org/m")
    dep = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    assert dep.source == "managed"


def test_upsert_adopted_creates_then_updates(tmp_path):
    conn = _conn(tmp_path)
    m = model_store.add(conn, name="nvidia/MiniMax-M2.7-NVFP4",
                        hf_repo="nvidia/MiniMax-M2.7-NVFP4")
    dep = dep_store.upsert_adopted(
        conn, model_id=m.id, node_id=3, container_id="cid-1",
        address="127.0.0.1", port=30011, gpu_ids=[7],
        vram_reserved_mb=268000, image_tag="lmsysorg/sglang:latest",
    )
    assert dep.source == "adopted"
    assert dep.gpu_ids == [7]
    assert dep.container_port == 30011
    assert dep.status == "ready"

    dep2 = dep_store.upsert_adopted(
        conn, model_id=m.id, node_id=3, container_id="cid-1",
        address="127.0.0.1", port=30012, gpu_ids=[7],
        vram_reserved_mb=268000, image_tag="lmsysorg/sglang:latest",
    )
    assert dep2.id == dep.id
    assert dep2.container_port == 30012
    assert len(dep_store.list_adopted_for_node(conn, 3)) == 1


def test_list_adopted_for_node_excludes_managed(tmp_path):
    conn = _conn(tmp_path)
    m = model_store.add(conn, name="m", hf_repo="org/m")
    dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    dep_store.upsert_adopted(
        conn, model_id=m.id, node_id=3, container_id="cid-1",
        address="127.0.0.1", port=30011, gpu_ids=[7],
        vram_reserved_mb=1, image_tag="external",
    )
    adopted = dep_store.list_adopted_for_node(conn, 3)
    assert [d.container_id for d in adopted] == ["cid-1"]
