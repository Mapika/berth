from __future__ import annotations
import asyncio
import pytest
from fastapi import HTTPException
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store
from berth.daemon import admin_workloads, admin_adapters

def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn); return conn

def _adopted(conn):
    m = model_store.add(conn, name="mm", hf_repo="org/mm")
    return dep_store.upsert_adopted(conn, model_id=m.id, node_id=2, container_id="cid-1",
        address="127.0.0.1", port=30011, gpu_ids=[7], vram_reserved_mb=1, image_tag="external")

class _Mgr:
    def __init__(self): self.stopped = []
    async def stop(self, dep_id): self.stopped.append(dep_id)

def test_stop_adopted_is_rejected(tmp_path):
    conn=_conn(tmp_path); dep=_adopted(conn); mgr=_Mgr()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(admin_workloads.stop_deployment(dep.id, manager=mgr, conn=conn))
    assert ei.value.status_code == 409
    assert "unadopt" in ei.value.detail
    assert mgr.stopped == []

def test_stop_managed_still_calls_manager(tmp_path):
    conn=_conn(tmp_path)
    m=model_store.add(conn, name="x", hf_repo="org/x")
    dep=dep_store.create(conn, model_id=m.id, backend="vllm", image_tag="i:1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto")
    mgr=_Mgr()
    asyncio.run(admin_workloads.stop_deployment(dep.id, manager=mgr, conn=conn))
    assert mgr.stopped == [dep.id]

def test_hot_unload_on_adopted_is_rejected(tmp_path):
    from berth.backends.adopted import AdoptedBackend
    conn=_conn(tmp_path); dep=_adopted(conn); backends={"adopted": AdoptedBackend()}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(admin_adapters.hot_unload_adapter(
            dep_id=dep.id, adapter_name="a", conn=conn, backends=backends))
    assert ei.value.status_code == 409
    assert "adapter" in ei.value.detail.lower()
