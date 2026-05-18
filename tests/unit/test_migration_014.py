from __future__ import annotations

from serve_engine.store import db


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def test_migration_014_creates_nodes_tables(tmp_path):
    conn = _fresh(tmp_path)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {r[0] for r in cur.fetchall()}
    assert "nodes" in names
    assert "node_gpus" in names


def test_migration_014_adds_node_id_to_deployments(tmp_path):
    conn = _fresh(tmp_path)
    cur = conn.execute("PRAGMA table_info(deployments)")
    cols = {r[1] for r in cur.fetchall()}
    assert "node_id" in cols


def test_migration_014_node_gpus_cascade(tmp_path):
    conn = _fresh(tmp_path)
    cur = conn.execute("PRAGMA foreign_key_list(node_gpus)")
    fks = cur.fetchall()
    assert any(fk[2] == "nodes" and fk[6] == "CASCADE" for fk in fks)
