"""Regression: ``dep_store.update_status`` is asymmetric — transitions into
terminal/transitional states always win, transitions back into an active
state are refused once the row is terminal."""
from __future__ import annotations

from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store


def _setup(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    m = model_store.add(conn, name="m", hf_repo="org/m")
    dep = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto",
    )
    return conn, dep.id


def test_update_status_refuses_to_revive_stopped_row(tmp_path):
    """A load() that races a concurrent stop() must not be able to overwrite
    the stopped row with ``ready`` after the container has been torn down."""
    conn, dep_id = _setup(tmp_path)
    assert dep_store.update_status(conn, dep_id, "stopped") is True
    # The "ready" transition lost the race; the row stays "stopped".
    assert dep_store.update_status(conn, dep_id, "ready") is False
    assert dep_store.get_by_id(conn, dep_id).status == "stopped"


def test_update_status_refuses_to_revive_failed_row(tmp_path):
    conn, dep_id = _setup(tmp_path)
    assert dep_store.update_status(conn, dep_id, "failed",
                                   last_error="image pull failed") is True
    assert dep_store.update_status(conn, dep_id, "ready") is False
    assert dep_store.get_by_id(conn, dep_id).status == "failed"


def test_update_status_allows_failed_to_stopped(tmp_path):
    """stop_all() must be able to clean up a failed row into stopped — that's
    the existing operator semantics (see
    test_stop_all_stops_every_non_stopped_deployment)."""
    conn, dep_id = _setup(tmp_path)
    dep_store.update_status(conn, dep_id, "failed", last_error="x")
    assert dep_store.update_status(conn, dep_id, "stopped") is True
    assert dep_store.get_by_id(conn, dep_id).status == "stopped"


def test_update_status_normal_forward_progression(tmp_path):
    """The normal pending → loading → ready arc still works end-to-end."""
    conn, dep_id = _setup(tmp_path)
    assert dep_store.update_status(conn, dep_id, "loading") is True
    assert dep_store.update_status(conn, dep_id, "ready") is True
    assert dep_store.update_status(conn, dep_id, "stopping") is True
    assert dep_store.update_status(conn, dep_id, "stopped") is True
    assert dep_store.get_by_id(conn, dep_id).status == "stopped"
