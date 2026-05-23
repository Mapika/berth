import asyncio

from berth.lifecycle.manager import LifecycleManager
from berth.store import db
from berth.store import deployment_adapters as da_store
from berth.store import deployments as dep_store
from berth.store import models as model_store


class _Docker:
    def __init__(self):
        self.stopped = []

    def stop(self, container_id, timeout=30):
        self.stopped.append(container_id)


def test_stop_adopted_does_not_touch_docker(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    m = model_store.add(conn, name="mm", hf_repo="org/mm")
    dep = dep_store.upsert_adopted(
        conn, model_id=m.id, node_id=0, container_id="ext-cid",
        address="127.0.0.1", port=30011, gpu_ids=[7],
        vram_reserved_mb=1, image_tag="external",
    )
    docker = _Docker()
    mgr = LifecycleManager(
        conn=conn,
        docker_client=docker,
        backends={},
        models_dir=tmp_path,
    )
    asyncio.run(mgr.stop(dep.id))
    assert docker.stopped == []                      # process untouched
    assert dep_store.get_by_id(conn, dep.id).status == "stopped"
    assert da_store.count_for_deployment(conn, dep.id) == 0
