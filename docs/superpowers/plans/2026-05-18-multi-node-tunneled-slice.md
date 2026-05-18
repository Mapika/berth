# Multi-Node Serving — Tunneled Vertical Slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the smallest useful multi-node setup: one leader can manage a remote agent over mTLS WebSocket, place deployments on its GPUs, and route `/v1/*` traffic to those deployments via WS tunneling. Single-node installs keep working unchanged.

**Architecture:** Same `serve` binary in two roles. Leader keeps SQLite + router + auth + lifecycle. Agent wraps the existing Docker driver and dials home over mTLS WS. WS multiplexes control, telemetry, and per-request data-plane streams. Local host runs an in-process agent so placement and routing have one code path.

**Tech Stack:** Python 3.11, FastAPI, `websockets` (client), `cryptography` (CA + certs), existing SQLite/asyncio stack.

**Scope (this plan):** DB foundations, CA + enrollment, WS protocol + leader hub, agent client, agent daemon CLI, AgentLink abstraction with local + remote implementations, placement across (node, gpu), tunneled data plane for `/v1/*`, heartbeat/unreachable handling, `serve nodes` CLI.

**Out of scope (follow-up plans):**
- Direct-LAN ingress + leader probing (separate plan)
- UI additions (Nodes page, node chips) (separate plan)
- Service-profile `node_label` affinity field (separate plan)
- Replica fan-out across nodes (separate plan)
- Reconnect reconciliation polish — orphan-container kill, deployment state diff (separate plan; minimal version included here)

**Spec:** `docs/superpowers/specs/2026-05-18-multi-node-serving-design.md`

---

## File Structure

**Create:**
- `src/serve_engine/store/migrations/014_nodes.sql` — new tables, deployments.node_id
- `src/serve_engine/store/nodes.py` — node CRUD
- `src/serve_engine/store/node_gpus.py` — per-node GPU CRUD
- `src/serve_engine/cluster/__init__.py`
- `src/serve_engine/cluster/ca.py` — CA + agent cert mint + fingerprint
- `src/serve_engine/cluster/enrollment.py` — one-time token store
- `src/serve_engine/cluster/protocol.py` — WS frame schema
- `src/serve_engine/cluster/agent_link.py` — AgentLink protocol (interface)
- `src/serve_engine/cluster/local_agent.py` — in-process AgentLink
- `src/serve_engine/cluster/leader_hub.py` — WS server endpoint, remote AgentLink
- `src/serve_engine/cluster/agent_client.py` — WS client, frame dispatch
- `src/serve_engine/cluster/agent_daemon.py` — agent role entrypoint
- `src/serve_engine/cluster/host_info.py` — collect cpu/ram/gpu inventory
- `src/serve_engine/cli/nodes_cmd.py` — `serve nodes` subcommands
- `src/serve_engine/cli/agent_cmd.py` — `serve agent` subcommands
- `tests/unit/test_ca.py`, `test_protocol.py`, `test_nodes_store.py`, `test_node_gpus_store.py`, `test_local_agent.py`, `test_enrollment.py`, `test_placement_multinode.py`
- `tests/integration/test_remote_agent_roundtrip.py`

**Modify:**
- `pyproject.toml` — add `cryptography>=42`, `websockets>=12`
- `src/serve_engine/store/db.py` — apply 014 migration (no code change if migration runner is automatic; verify)
- `src/serve_engine/store/deployments.py` — add `node_id` column to row dataclass and queries
- `src/serve_engine/daemon/app.py` — mount WS hub, bootstrap local node, instantiate AgentLink registry
- `src/serve_engine/daemon/admin.py` — add `POST /admin/nodes/enroll`, `GET /admin/nodes`, `GET /admin/nodes/{id}`, `DELETE /admin/nodes/{id}`, `POST /admin/nodes/register` (cert exchange)
- `src/serve_engine/daemon/openai_proxy.py` — dispatch via AgentLink instead of direct `httpx` to engine
- `src/serve_engine/lifecycle/manager.py` — call AgentLink for start/stop instead of DockerClient
- `src/serve_engine/lifecycle/placement.py` — extend candidates to (node, gpu)
- `src/serve_engine/cli/__init__.py` — register nodes_cmd, agent_cmd
- `src/serve_engine/cli/daemon_cmd.py` — `--role agent` flag plumbing

**Test commands:**
- Unit: `pytest tests/unit -v`
- Integration: `pytest tests/integration -v`
- Lint: `ruff check src/ tests/`
- Type: `mypy src/serve_engine`

---

## Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `cryptography` and `websockets` to dependencies**

In `pyproject.toml`, under `[project] dependencies`, add:

```toml
    "cryptography>=42",
    "websockets>=12",
```

- [ ] **Step 2: Sync the env**

Run: `uv pip install -e ".[dev]"`
Expected: installs `cryptography` and `websockets`, no errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add cryptography and websockets for cluster transport"
```

---

## Task 2: Schema migration — nodes, node_gpus, deployments.node_id

**Files:**
- Create: `src/serve_engine/store/migrations/014_nodes.sql`
- Test: `tests/unit/test_migration_014.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_migration_014.py
from __future__ import annotations

import sqlite3

import pytest

from serve_engine.store.db import open_db


def test_migration_014_creates_nodes_tables(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    names = {r[0] for r in cur.fetchall()}
    assert "nodes" in names
    assert "node_gpus" in names


def test_migration_014_adds_node_id_to_deployments(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    cur = conn.execute("PRAGMA table_info(deployments)")
    cols = {r[1] for r in cur.fetchall()}
    assert "node_id" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_migration_014.py -v`
Expected: FAIL — `nodes` table not in schema.

- [ ] **Step 3: Write the migration**

Create `src/serve_engine/store/migrations/014_nodes.sql`:

```sql
-- Multi-node support: nodes table, per-node GPU inventory, and a
-- deployments.node_id pointer. Single-node installs become node_id=0
-- automatically (the local node row is inserted by the daemon on first
-- startup; existing deployments default to node_id=0 here).

CREATE TABLE nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL UNIQUE,
    fingerprint     TEXT NOT NULL,
    reachable_as    TEXT,
    status          TEXT NOT NULL DEFAULT 'unreachable',
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    agent_version   TEXT,
    cpu_count       INTEGER NOT NULL DEFAULT 0,
    total_ram_mb    INTEGER NOT NULL DEFAULT 0,
    gpu_count       INTEGER NOT NULL DEFAULT 0,
    total_vram_mb   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE node_gpus (
    node_id         INTEGER NOT NULL,
    gpu_index       INTEGER NOT NULL,
    name            TEXT NOT NULL,
    total_vram_mb   INTEGER NOT NULL,
    driver_version  TEXT,
    PRIMARY KEY (node_id, gpu_index),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

ALTER TABLE deployments ADD COLUMN node_id INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_deployments_node_id ON deployments(node_id);
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_migration_014.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/store/migrations/014_nodes.sql tests/unit/test_migration_014.py
git commit -m "feat(store): migration 014 — nodes, node_gpus, deployments.node_id"
```

---

## Task 3: Node store CRUD

**Files:**
- Create: `src/serve_engine/store/nodes.py`
- Test: `tests/unit/test_nodes_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_nodes_store.py
from __future__ import annotations

import time

from serve_engine.store.db import open_db
from serve_engine.store import nodes as nodes_store


def test_insert_get_list_node(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    now = time.time()
    node_id = nodes_store.insert(
        conn,
        label="agent-a",
        fingerprint="sha256:aaaa",
        reachable_as=None,
        first_seen=now,
        last_seen=now,
        agent_version="0.0.1",
        cpu_count=8,
        total_ram_mb=32000,
        gpu_count=1,
        total_vram_mb=80000,
    )
    n = nodes_store.get(conn, node_id)
    assert n is not None
    assert n.label == "agent-a"
    assert n.fingerprint == "sha256:aaaa"
    assert n.status == "unreachable"
    rows = nodes_store.list_all(conn)
    assert [r.id for r in rows] == [node_id]


def test_set_status_and_last_seen(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    nid = nodes_store.insert(
        conn, label="x", fingerprint="fp",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=12.0)
    n = nodes_store.get(conn, nid)
    assert n.status == "ready"
    assert n.last_seen == 12.0


def test_delete_node(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    nid = nodes_store.insert(
        conn, label="x", fingerprint="fp",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.delete(conn, nid)
    assert nodes_store.get(conn, nid) is None


def test_find_by_fingerprint(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    nid = nodes_store.insert(
        conn, label="x", fingerprint="fp-xyz",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    n = nodes_store.find_by_fingerprint(conn, "fp-xyz")
    assert n is not None and n.id == nid
    assert nodes_store.find_by_fingerprint(conn, "nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_nodes_store.py -v`
Expected: FAIL — module `serve_engine.store.nodes` not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/store/nodes.py`:

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    id: int
    label: str
    fingerprint: str
    reachable_as: str | None
    status: str
    first_seen: float
    last_seen: float
    agent_version: str | None
    cpu_count: int
    total_ram_mb: int
    gpu_count: int
    total_vram_mb: int


def _row_to_node(row: tuple) -> Node:
    return Node(
        id=row[0], label=row[1], fingerprint=row[2], reachable_as=row[3],
        status=row[4], first_seen=row[5], last_seen=row[6],
        agent_version=row[7], cpu_count=row[8], total_ram_mb=row[9],
        gpu_count=row[10], total_vram_mb=row[11],
    )


def insert(
    conn: sqlite3.Connection,
    *,
    label: str,
    fingerprint: str,
    reachable_as: str | None,
    first_seen: float,
    last_seen: float,
    agent_version: str | None,
    cpu_count: int,
    total_ram_mb: int,
    gpu_count: int,
    total_vram_mb: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO nodes (
            label, fingerprint, reachable_as, status,
            first_seen, last_seen, agent_version,
            cpu_count, total_ram_mb, gpu_count, total_vram_mb
        ) VALUES (?, ?, ?, 'unreachable', ?, ?, ?, ?, ?, ?, ?)
        """,
        (label, fingerprint, reachable_as,
         first_seen, last_seen, agent_version,
         cpu_count, total_ram_mb, gpu_count, total_vram_mb),
    )
    conn.commit()
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, node_id: int) -> Node | None:
    cur = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
    row = cur.fetchone()
    return _row_to_node(row) if row else None


def find_by_label(conn: sqlite3.Connection, label: str) -> Node | None:
    cur = conn.execute("SELECT * FROM nodes WHERE label = ?", (label,))
    row = cur.fetchone()
    return _row_to_node(row) if row else None


def find_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Node | None:
    cur = conn.execute("SELECT * FROM nodes WHERE fingerprint = ?", (fingerprint,))
    row = cur.fetchone()
    return _row_to_node(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[Node]:
    cur = conn.execute("SELECT * FROM nodes ORDER BY id")
    return [_row_to_node(r) for r in cur.fetchall()]


def set_status(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    status: str,
    last_seen: float,
) -> None:
    conn.execute(
        "UPDATE nodes SET status = ?, last_seen = ? WHERE id = ?",
        (status, last_seen, node_id),
    )
    conn.commit()


def update_inventory(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    agent_version: str,
    cpu_count: int,
    total_ram_mb: int,
    gpu_count: int,
    total_vram_mb: int,
) -> None:
    conn.execute(
        """UPDATE nodes
           SET agent_version = ?, cpu_count = ?, total_ram_mb = ?,
               gpu_count = ?, total_vram_mb = ?
           WHERE id = ?""",
        (agent_version, cpu_count, total_ram_mb,
         gpu_count, total_vram_mb, node_id),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, node_id: int) -> None:
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_nodes_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/store/nodes.py tests/unit/test_nodes_store.py
git commit -m "feat(store): node CRUD"
```

---

## Task 4: Node GPU store CRUD

**Files:**
- Create: `src/serve_engine/store/node_gpus.py`
- Test: `tests/unit/test_node_gpus_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_node_gpus_store.py
from __future__ import annotations

from serve_engine.store.db import open_db
from serve_engine.store import nodes as nodes_store
from serve_engine.store import node_gpus as node_gpus_store


def _seed_node(conn) -> int:
    return nodes_store.insert(
        conn, label="local", fingerprint="local",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )


def test_upsert_and_list(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    nid = _seed_node(conn)
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=0,
        name="H100", total_vram_mb=81920, driver_version="555.42",
    )
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=1,
        name="H100", total_vram_mb=81920, driver_version="555.42",
    )
    gpus = node_gpus_store.list_for_node(conn, nid)
    assert [g.gpu_index for g in gpus] == [0, 1]


def test_upsert_updates_existing_row(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    nid = _seed_node(conn)
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=0,
        name="old", total_vram_mb=1, driver_version=None,
    )
    node_gpus_store.upsert(
        conn, node_id=nid, gpu_index=0,
        name="new", total_vram_mb=2, driver_version=None,
    )
    gpus = node_gpus_store.list_for_node(conn, nid)
    assert len(gpus) == 1
    assert gpus[0].name == "new" and gpus[0].total_vram_mb == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_node_gpus_store.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/store/node_gpus.py`:

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class NodeGpu:
    node_id: int
    gpu_index: int
    name: str
    total_vram_mb: int
    driver_version: str | None


def upsert(
    conn: sqlite3.Connection,
    *,
    node_id: int,
    gpu_index: int,
    name: str,
    total_vram_mb: int,
    driver_version: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO node_gpus (node_id, gpu_index, name, total_vram_mb, driver_version)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(node_id, gpu_index) DO UPDATE SET
            name = excluded.name,
            total_vram_mb = excluded.total_vram_mb,
            driver_version = excluded.driver_version
        """,
        (node_id, gpu_index, name, total_vram_mb, driver_version),
    )
    conn.commit()


def list_for_node(conn: sqlite3.Connection, node_id: int) -> list[NodeGpu]:
    cur = conn.execute(
        "SELECT node_id, gpu_index, name, total_vram_mb, driver_version "
        "FROM node_gpus WHERE node_id = ? ORDER BY gpu_index",
        (node_id,),
    )
    return [NodeGpu(*r) for r in cur.fetchall()]


def delete_for_node(conn: sqlite3.Connection, node_id: int) -> None:
    conn.execute("DELETE FROM node_gpus WHERE node_id = ?", (node_id,))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_node_gpus_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/store/node_gpus.py tests/unit/test_node_gpus_store.py
git commit -m "feat(store): per-node GPU inventory CRUD"
```

---

## Task 5: Host inventory helper

**Files:**
- Create: `src/serve_engine/cluster/__init__.py` (empty)
- Create: `src/serve_engine/cluster/host_info.py`
- Test: `tests/unit/test_host_info.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_host_info.py
from __future__ import annotations

from serve_engine.cluster.host_info import HostInfo, collect_host_info


def test_collect_host_info_returns_populated_struct(monkeypatch):
    # Patch the GPU collector so the test runs without an NVIDIA host.
    from serve_engine.cluster import host_info as hi

    monkeypatch.setattr(
        hi, "_collect_gpus",
        lambda: [hi.GpuInfo(index=0, name="Mock", total_vram_mb=1024, driver_version="x")],
    )
    info = collect_host_info()
    assert isinstance(info, HostInfo)
    assert info.cpu_count >= 1
    assert info.total_ram_mb > 0
    assert info.gpu_count == 1
    assert info.total_vram_mb == 1024
    assert info.gpus[0].name == "Mock"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_host_info.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/__init__.py` empty.

Create `src/serve_engine/cluster/host_info.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    total_vram_mb: int
    driver_version: str | None


@dataclass(frozen=True)
class HostInfo:
    cpu_count: int
    total_ram_mb: int
    gpu_count: int
    total_vram_mb: int
    gpus: list[GpuInfo]


def _collect_ram_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _collect_gpus() -> list[GpuInfo]:
    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        return []
    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError:
        return []
    try:
        driver = pynvml.nvmlSystemGetDriverVersion()
        if isinstance(driver, bytes):
            driver = driver.decode()
        out: list[GpuInfo] = []
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            out.append(GpuInfo(
                index=i, name=name,
                total_vram_mb=int(mem.total // (1024 * 1024)),
                driver_version=driver,
            ))
        return out
    finally:
        try:
            pynvml.nvmlShutdown()
        except pynvml.NVMLError:
            pass


def collect_host_info() -> HostInfo:
    gpus = _collect_gpus()
    return HostInfo(
        cpu_count=os.cpu_count() or 1,
        total_ram_mb=_collect_ram_mb(),
        gpu_count=len(gpus),
        total_vram_mb=sum(g.total_vram_mb for g in gpus),
        gpus=gpus,
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_host_info.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/__init__.py src/serve_engine/cluster/host_info.py tests/unit/test_host_info.py
git commit -m "feat(cluster): host info collector (cpu, ram, gpu inventory)"
```

---

## Task 6: Local node bootstrap on daemon startup

**Files:**
- Create: `src/serve_engine/cluster/local_bootstrap.py`
- Modify: `src/serve_engine/daemon/app.py`
- Test: `tests/unit/test_local_bootstrap.py`

Goal: when the leader starts, ensure a `nodes` row with `id=0`, `label='local'`, `fingerprint='local'` exists, populate its GPU inventory from `host_info`, and set status `ready`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_local_bootstrap.py
from __future__ import annotations

from serve_engine.cluster import host_info as hi
from serve_engine.cluster.local_bootstrap import ensure_local_node
from serve_engine.store.db import open_db
from serve_engine.store import nodes as nodes_store
from serve_engine.store import node_gpus as node_gpus_store


def test_inserts_local_node_with_id_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hi, "collect_host_info",
        lambda: hi.HostInfo(
            cpu_count=4, total_ram_mb=8000, gpu_count=1, total_vram_mb=1024,
            gpus=[hi.GpuInfo(index=0, name="Mock", total_vram_mb=1024, driver_version="x")],
        ),
    )
    conn = open_db(tmp_path / "db.sqlite")
    ensure_local_node(conn, agent_version="0.0.1-test")
    n = nodes_store.find_by_label(conn, "local")
    assert n is not None
    assert n.id == 0 or n.id == 1  # AUTOINCREMENT — accept either; see Step 3 note
    assert n.status == "ready"
    gpus = node_gpus_store.list_for_node(conn, n.id)
    assert len(gpus) == 1 and gpus[0].name == "Mock"


def test_idempotent_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hi, "collect_host_info",
        lambda: hi.HostInfo(
            cpu_count=4, total_ram_mb=8000, gpu_count=0, total_vram_mb=0, gpus=[],
        ),
    )
    conn = open_db(tmp_path / "db.sqlite")
    ensure_local_node(conn, agent_version="v1")
    ensure_local_node(conn, agent_version="v2")
    rows = nodes_store.list_all(conn)
    assert len(rows) == 1
    assert rows[0].agent_version == "v2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_local_bootstrap.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/local_bootstrap.py`:

```python
from __future__ import annotations

import sqlite3
import time

from serve_engine.cluster.host_info import collect_host_info
from serve_engine.store import node_gpus as node_gpus_store
from serve_engine.store import nodes as nodes_store


def ensure_local_node(conn: sqlite3.Connection, *, agent_version: str) -> int:
    """Ensure a 'local' node row exists and is up to date.

    Note: we use label='local' + fingerprint='local' as the durable identity.
    The exact integer id is whatever AUTOINCREMENT assigns on first insert;
    nothing in the codebase depends on it being literally 0.
    """
    info = collect_host_info()
    now = time.time()
    existing = nodes_store.find_by_label(conn, "local")
    if existing is None:
        node_id = nodes_store.insert(
            conn,
            label="local", fingerprint="local",
            reachable_as=None,
            first_seen=now, last_seen=now,
            agent_version=agent_version,
            cpu_count=info.cpu_count,
            total_ram_mb=info.total_ram_mb,
            gpu_count=info.gpu_count,
            total_vram_mb=info.total_vram_mb,
        )
    else:
        node_id = existing.id
        nodes_store.update_inventory(
            conn, node_id,
            agent_version=agent_version,
            cpu_count=info.cpu_count,
            total_ram_mb=info.total_ram_mb,
            gpu_count=info.gpu_count,
            total_vram_mb=info.total_vram_mb,
        )
    nodes_store.set_status(conn, node_id, status="ready", last_seen=now)
    node_gpus_store.delete_for_node(conn, node_id)
    for g in info.gpus:
        node_gpus_store.upsert(
            conn, node_id=node_id, gpu_index=g.index,
            name=g.name, total_vram_mb=g.total_vram_mb,
            driver_version=g.driver_version,
        )
    return node_id
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_local_bootstrap.py -v`
Expected: PASS.

- [ ] **Step 5: Wire into daemon startup**

In `src/serve_engine/daemon/app.py`, find the leader-mode startup section (where the DB connection is opened) and add:

```python
from serve_engine import __version__ as _serve_version
from serve_engine.cluster.local_bootstrap import ensure_local_node

# ... after `conn = open_db(...)` and migrations have run:
ensure_local_node(conn, agent_version=_serve_version)
```

If `serve_engine.__init__` has no `__version__`, add `__version__ = "0.0.1"` to it as the same change.

- [ ] **Step 6: Run the unit suite to confirm nothing else broke**

Run: `pytest tests/unit -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/serve_engine/cluster/local_bootstrap.py src/serve_engine/daemon/app.py src/serve_engine/__init__.py tests/unit/test_local_bootstrap.py
git commit -m "feat(cluster): bootstrap local node row on daemon startup"
```

---

## Task 7: CA — self-signed CA cert + agent cert mint

**Files:**
- Create: `src/serve_engine/cluster/ca.py`
- Test: `tests/unit/test_ca.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ca.py
from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from serve_engine.cluster.ca import (
    fingerprint_sha256,
    generate_ca,
    issue_agent_cert,
    load_ca,
)


def test_generate_and_load_ca(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="serve-engine-ca")
    ca = load_ca(ca_dir)
    cert = x509.load_pem_x509_certificate(ca.cert_pem)
    assert "serve-engine-ca" in cert.subject.rfc4514_string()


def test_issue_agent_cert_signed_by_ca(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="serve-engine-ca")
    ca = load_ca(ca_dir)
    bundle = issue_agent_cert(ca, label="agent-a")
    leaf = x509.load_pem_x509_certificate(bundle.cert_pem)
    assert "agent-a" in leaf.subject.rfc4514_string()
    # Verify it's signed by the CA's public key
    ca_cert = x509.load_pem_x509_certificate(ca.cert_pem)
    ca_cert.public_key().verify(
        leaf.signature, leaf.tbs_certificate_bytes,
        leaf.signature_algorithm_parameters,
        leaf.signature_hash_algorithm,  # type: ignore[arg-type]
    )


def test_fingerprint_is_stable_sha256(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="x")
    ca = load_ca(ca_dir)
    b = issue_agent_cert(ca, label="agent-a")
    fp1 = fingerprint_sha256(b.cert_pem)
    fp2 = fingerprint_sha256(b.cert_pem)
    assert fp1 == fp2
    assert fp1.startswith("sha256:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ca.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/ca.py`:

```python
from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


_ONE_YEAR = _dt.timedelta(days=365)
_TEN_YEARS = _dt.timedelta(days=365 * 10)


@dataclass(frozen=True)
class CA:
    cert_pem: bytes
    key_pem: bytes


@dataclass(frozen=True)
class CertBundle:
    cert_pem: bytes
    key_pem: bytes


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def generate_ca(ca_dir: Path, *, common_name: str) -> None:
    ca_dir = Path(ca_dir)
    ca_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _TEN_YEARS)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    (ca_dir / "ca.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (ca_dir / "ca.key").write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    (ca_dir / "ca.key").chmod(0o600)


def load_ca(ca_dir: Path) -> CA:
    ca_dir = Path(ca_dir)
    return CA(
        cert_pem=(ca_dir / "ca.crt").read_bytes(),
        key_pem=(ca_dir / "ca.key").read_bytes(),
    )


def issue_agent_cert(ca: CA, *, label: str) -> CertBundle:
    ca_cert = x509.load_pem_x509_certificate(ca.cert_pem)
    ca_key = serialization.load_pem_private_key(ca.key_pem, password=None)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(label))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _ONE_YEAR * 5)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())  # type: ignore[arg-type]
    )
    return CertBundle(
        cert_pem=leaf.public_bytes(serialization.Encoding.PEM),
        key_pem=leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def fingerprint_sha256(cert_pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return "sha256:" + hashlib.sha256(der).hexdigest()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_ca.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/ca.py tests/unit/test_ca.py
git commit -m "feat(cluster): CA + agent cert mint + fingerprint helpers"
```

---

## Task 8: Enrollment token store

**Files:**
- Create: `src/serve_engine/cluster/enrollment.py`
- Test: `tests/unit/test_enrollment.py`

Goal: a small in-memory single-use token store. Tokens TTL out after a configurable window (default 10 min). Persisted on the leader's filesystem so a daemon restart doesn't lose mid-flight enrollments? **No** — keep in-memory only; operator re-mints if they restart mid-enrollment. Simpler.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_enrollment.py
from __future__ import annotations

import pytest

from serve_engine.cluster.enrollment import EnrollmentTokens


def test_mint_and_consume_once():
    store = EnrollmentTokens(ttl_seconds=60, now=lambda: 1000.0)
    tok = store.mint(label="agent-a")
    assert store.consume(tok) == "agent-a"
    assert store.consume(tok) is None  # single-use


def test_token_expires():
    t = {"now": 1000.0}
    store = EnrollmentTokens(ttl_seconds=60, now=lambda: t["now"])
    tok = store.mint(label="agent-a")
    t["now"] = 1061.0
    assert store.consume(tok) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_enrollment.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/enrollment.py`:

```python
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class _Entry:
    label: str
    expires_at: float


class EnrollmentTokens:
    """In-memory single-use enrollment token store.

    Tokens are minted by the leader (e.g. via `serve nodes enroll`) and
    consumed once by an agent during `serve agent register`. Successful
    consumption hands the agent back a long-lived client certificate; the
    token is then discarded.
    """

    def __init__(
        self, *,
        ttl_seconds: int = 600,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._tokens: dict[str, _Entry] = {}

    def mint(self, *, label: str) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = _Entry(
            label=label, expires_at=self._now() + self._ttl,
        )
        return token

    def consume(self, token: str) -> str | None:
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        if entry.expires_at < self._now():
            return None
        return entry.label
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_enrollment.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/enrollment.py tests/unit/test_enrollment.py
git commit -m "feat(cluster): single-use enrollment token store"
```

---

## Task 9: WS frame schema (protocol)

**Files:**
- Create: `src/serve_engine/cluster/protocol.py`
- Test: `tests/unit/test_protocol.py`

Goal: a JSON envelope schema for every frame that crosses the WS. Frames are line-delimited JSON for control/telemetry; data-plane HTTP chunks are length-prefixed binary tunneled as a second WS subprotocol but encoded inside the same JSON envelope as base64 for simplicity in v1 (we can switch to binary frames later if perf demands).

Frame types (kept narrow for v1):
- `hello` — agent → leader on connect: `{type, agent_version, host_info}`
- `welcome` — leader → agent: `{type, node_id, server_time}`
- `heartbeat` — agent → leader: `{type, ts}`
- `gpu_stats` — agent → leader: `{type, gpus: [{index, utilization, used_mb}]}`
- `start_deployment` — leader → agent: `{type, request_id, plan}`
- `stop_deployment` — leader → agent: `{type, request_id, container_id}`
- `op_result` — agent → leader: `{type, request_id, ok, data?, error?}`
- `http_request` — leader → agent (data plane): `{type, stream_id, method, path, headers, body_b64}`
- `http_chunk` — agent → leader: `{type, stream_id, status?, headers?, body_b64, eof}`
- `http_cancel` — leader → agent: `{type, stream_id}`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_protocol.py
from __future__ import annotations

import pytest

from serve_engine.cluster.protocol import (
    Frame,
    decode_frame,
    encode_frame,
    Hello,
    HttpChunk,
    HttpRequest,
    OpResult,
    StartDeployment,
    Welcome,
)


def test_encode_decode_hello():
    f = Hello(agent_version="0.0.1", host_info={"cpu_count": 4, "total_ram_mb": 8000, "gpus": []})
    wire = encode_frame(f)
    back = decode_frame(wire)
    assert isinstance(back, Hello)
    assert back.agent_version == "0.0.1"


def test_decode_unknown_type_raises():
    with pytest.raises(ValueError):
        decode_frame('{"type": "nope"}')


def test_http_request_roundtrip():
    f = HttpRequest(
        stream_id="s1", method="POST", path="/v1/chat/completions",
        headers={"content-type": "application/json"}, body_b64="aGVsbG8=",
    )
    back = decode_frame(encode_frame(f))
    assert isinstance(back, HttpRequest)
    assert back.method == "POST"
    assert back.body_b64 == "aGVsbG8="


def test_http_chunk_eof():
    f = HttpChunk(stream_id="s1", status=200, headers={"x": "y"}, body_b64="", eof=True)
    back = decode_frame(encode_frame(f))
    assert isinstance(back, HttpChunk)
    assert back.eof is True
    assert back.status == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_protocol.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/protocol.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Hello:
    agent_version: str
    host_info: dict[str, Any]
    type: str = "hello"


@dataclass
class Welcome:
    node_id: int
    server_time: float
    type: str = "welcome"


@dataclass
class Heartbeat:
    ts: float
    type: str = "heartbeat"


@dataclass
class GpuStats:
    gpus: list[dict[str, Any]]
    type: str = "gpu_stats"


@dataclass
class StartDeployment:
    request_id: str
    plan: dict[str, Any]
    type: str = "start_deployment"


@dataclass
class StopDeployment:
    request_id: str
    container_id: str
    type: str = "stop_deployment"


@dataclass
class OpResult:
    request_id: str
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    type: str = "op_result"


@dataclass
class HttpRequest:
    stream_id: str
    method: str
    path: str
    headers: dict[str, str]
    body_b64: str
    type: str = "http_request"


@dataclass
class HttpChunk:
    stream_id: str
    body_b64: str
    eof: bool
    status: int | None = None
    headers: dict[str, str] | None = None
    type: str = "http_chunk"


@dataclass
class HttpCancel:
    stream_id: str
    type: str = "http_cancel"


Frame = (
    Hello | Welcome | Heartbeat | GpuStats
    | StartDeployment | StopDeployment | OpResult
    | HttpRequest | HttpChunk | HttpCancel
)


_REGISTRY: dict[str, type] = {
    "hello": Hello, "welcome": Welcome, "heartbeat": Heartbeat,
    "gpu_stats": GpuStats, "start_deployment": StartDeployment,
    "stop_deployment": StopDeployment, "op_result": OpResult,
    "http_request": HttpRequest, "http_chunk": HttpChunk,
    "http_cancel": HttpCancel,
}


def encode_frame(frame: Frame) -> str:
    return json.dumps(frame.__dict__)


def decode_frame(wire: str | bytes) -> Frame:
    if isinstance(wire, bytes):
        wire = wire.decode("utf-8")
    raw = json.loads(wire)
    if not isinstance(raw, dict) or "type" not in raw:
        raise ValueError("frame missing 'type'")
    cls = _REGISTRY.get(raw["type"])
    if cls is None:
        raise ValueError(f"unknown frame type: {raw['type']!r}")
    payload = {k: v for k, v in raw.items() if k != "type"}
    return cls(**payload)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_protocol.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/protocol.py tests/unit/test_protocol.py
git commit -m "feat(cluster): WS frame schema"
```

---

## Task 10: AgentLink protocol (interface)

**Files:**
- Create: `src/serve_engine/cluster/agent_link.py`
- Test: `tests/unit/test_agent_link_interface.py`

The leader has many places it used to call `DockerClient` directly. Replace with an `AgentLink` interface implemented by `LocalAgentLink` (in-process) and `RemoteAgentLink` (WS-backed). LifecycleManager and openai_proxy depend only on this interface.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agent_link_interface.py
from __future__ import annotations

import inspect

from serve_engine.cluster.agent_link import AgentLink


def test_agentlink_required_methods():
    expected = {"start_deployment", "stop_deployment", "proxy_request", "is_ready", "node_id"}
    members = {n for n, _ in inspect.getmembers(AgentLink) if not n.startswith("_")}
    assert expected.issubset(members)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_agent_link_interface.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/agent_link.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class StartedContainer:
    container_id: str
    address: str   # host or ip the leader's router uses; "tunnel" sentinel for tunneled mode
    port: int      # for direct mode; ignored if address == "tunnel"


@dataclass(frozen=True)
class ProxyResponseChunk:
    """One chunk of an /v1/* response streamed back from an agent."""
    status: int | None  # set on first chunk only
    headers: dict[str, str] | None  # set on first chunk only
    body: bytes
    eof: bool


class AgentLink(Protocol):
    """Common interface for in-process and remote agents."""

    @property
    def node_id(self) -> int: ...
    @property
    def is_ready(self) -> bool: ...

    async def start_deployment(self, plan: dict[str, Any]) -> StartedContainer: ...
    async def stop_deployment(self, container_id: str, *, remove: bool = True) -> None: ...
    async def proxy_request(
        self,
        *,
        container_id: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> AsyncIterator[ProxyResponseChunk]: ...
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_agent_link_interface.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/agent_link.py tests/unit/test_agent_link_interface.py
git commit -m "feat(cluster): AgentLink protocol"
```

---

## Task 11: LocalAgentLink — in-process implementation

**Files:**
- Create: `src/serve_engine/cluster/local_agent.py`
- Test: `tests/unit/test_local_agent.py`

`LocalAgentLink` wraps the existing `DockerClient` for start/stop and uses `httpx.AsyncClient` for proxy_request. This keeps the single-node code path unchanged in behavior but unified under AgentLink.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_local_agent.py
from __future__ import annotations

import asyncio
import httpx
import pytest

from serve_engine.cluster.local_agent import LocalAgentLink


class _FakeContainer:
    def __init__(self, cid, port):
        self.id = cid
        self.attrs = {"NetworkSettings": {"Ports": {f"{port}/tcp": [{"HostPort": str(port)}]}}}
        self.name = "x"
    def reload(self): pass
    def stop(self, timeout): pass
    def remove(self): pass


class _FakeContainers:
    def __init__(self): self._c = None
    def run(self, **kw): self._c = _FakeContainer("cid-1", 9000); return self._c
    def get(self, cid): return self._c


class _FakeDocker:
    def __init__(self):
        self.containers = _FakeContainers()
        class _N:
            def get(s, name): pass
            def create(s, *a, **k): pass
        self.networks = _N()


@pytest.mark.asyncio
async def test_start_returns_address_and_port(monkeypatch):
    from serve_engine.lifecycle.docker_client import DockerClient
    dc = DockerClient(client=_FakeDocker(), network_name="serve")
    link = LocalAgentLink(node_id=0, docker_client=dc)
    started = await link.start_deployment({
        "image": "x", "name": "d-1", "command": [], "environment": {},
        "kwargs": {}, "volumes": {}, "internal_port": 9000,
    })
    assert started.container_id == "cid-1"
    assert started.address == "127.0.0.1"
    assert started.port == 9000


@pytest.mark.asyncio
async def test_proxy_request_streams_response(monkeypatch):
    from serve_engine.lifecycle.docker_client import DockerClient
    dc = DockerClient(client=_FakeDocker(), network_name="serve")

    started_addr = {"addr": "127.0.0.1", "port": 9000}

    class _T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async def stream():
                yield b"hello "
                yield b"world"
            return httpx.Response(200, headers={"content-type": "text/plain"}, stream=httpx.AsyncByteStream(stream()))

    link = LocalAgentLink(node_id=0, docker_client=dc,
                          transport_for_test=_T())
    # Pretend a deployment is running at 127.0.0.1:9000
    link._endpoints["cid-1"] = (started_addr["addr"], started_addr["port"])

    chunks = []
    async for c in link.proxy_request(
        container_id="cid-1", method="GET", path="/", headers={}, body=b"",
    ):
        chunks.append(c)
    assert chunks[0].status == 200
    assert b"".join(c.body for c in chunks) == b"hello world"
    assert chunks[-1].eof is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_local_agent.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/local_agent.py`:

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from serve_engine.cluster.agent_link import ProxyResponseChunk, StartedContainer
from serve_engine.lifecycle.docker_client import DockerClient


class LocalAgentLink:
    """In-process AgentLink. Uses the existing DockerClient on the leader host
    and httpx for direct loopback proxying. Preserves today's single-node
    behavior."""

    def __init__(
        self,
        *,
        node_id: int,
        docker_client: DockerClient,
        transport_for_test: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._node_id = node_id
        self._docker = docker_client
        self._endpoints: dict[str, tuple[str, int]] = {}
        self._transport = transport_for_test

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def is_ready(self) -> bool:
        return True

    async def start_deployment(self, plan: dict[str, Any]) -> StartedContainer:
        h = await asyncio.to_thread(
            self._docker.run,
            image=plan["image"], name=plan["name"], command=plan["command"],
            environment=plan["environment"], kwargs=plan["kwargs"],
            volumes=plan["volumes"], internal_port=plan["internal_port"],
        )
        self._endpoints[h.id] = (h.address, h.port)
        return StartedContainer(container_id=h.id, address=h.address, port=h.port)

    async def stop_deployment(self, container_id: str, *, remove: bool = True) -> None:
        await asyncio.to_thread(self._docker.stop, container_id, remove=remove)
        self._endpoints.pop(container_id, None)

    async def proxy_request(
        self, *, container_id: str, method: str, path: str,
        headers: dict[str, str], body: bytes,
    ) -> AsyncIterator[ProxyResponseChunk]:
        endpoint = self._endpoints.get(container_id)
        if endpoint is None:
            raise KeyError(f"no endpoint for container {container_id!r}")
        addr, port = endpoint
        base = f"http://{addr}:{port}"
        client = httpx.AsyncClient(
            base_url=base,
            transport=self._transport,
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0),
        )
        try:
            async with client.stream(
                method, path, headers=headers, content=body,
            ) as resp:
                first = True
                async for chunk in resp.aiter_raw():
                    yield ProxyResponseChunk(
                        status=resp.status_code if first else None,
                        headers=dict(resp.headers) if first else None,
                        body=chunk, eof=False,
                    )
                    first = False
                yield ProxyResponseChunk(
                    status=resp.status_code if first else None,
                    headers=dict(resp.headers) if first else None,
                    body=b"", eof=True,
                )
        finally:
            await client.aclose()

    def register_endpoint(self, container_id: str, address: str, port: int) -> None:
        """For deployments started before this AgentLink instance existed
        (e.g. process restart) — wire their endpoint back in."""
        self._endpoints[container_id] = (address, port)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_local_agent.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/local_agent.py tests/unit/test_local_agent.py
git commit -m "feat(cluster): in-process LocalAgentLink wrapping DockerClient"
```

---

## Task 12: Agent registry on the leader

**Files:**
- Create: `src/serve_engine/cluster/agent_registry.py`
- Test: `tests/unit/test_agent_registry.py`

A small in-memory map: `node_id -> AgentLink`. Created at daemon startup; entries added when agents connect, removed on disconnect.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agent_registry.py
from __future__ import annotations

import pytest

from serve_engine.cluster.agent_registry import AgentRegistry


class _Stub:
    def __init__(self, nid): self._nid = nid
    @property
    def node_id(self): return self._nid
    @property
    def is_ready(self): return True


def test_register_get_unregister():
    r = AgentRegistry()
    r.register(_Stub(7))
    assert r.get(7).node_id == 7
    assert {l.node_id for l in r.all()} == {7}
    r.unregister(7)
    assert r.get(7) is None


def test_get_missing_returns_none():
    r = AgentRegistry()
    assert r.get(99) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_agent_registry.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/agent_registry.py`:

```python
from __future__ import annotations

from serve_engine.cluster.agent_link import AgentLink


class AgentRegistry:
    def __init__(self) -> None:
        self._by_node: dict[int, AgentLink] = {}

    def register(self, link: AgentLink) -> None:
        self._by_node[link.node_id] = link

    def unregister(self, node_id: int) -> None:
        self._by_node.pop(node_id, None)

    def get(self, node_id: int) -> AgentLink | None:
        return self._by_node.get(node_id)

    def all(self) -> list[AgentLink]:
        return list(self._by_node.values())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_agent_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/agent_registry.py tests/unit/test_agent_registry.py
git commit -m "feat(cluster): in-memory AgentLink registry"
```

---

## Task 13: RemoteAgentLink — WS-backed AgentLink (skeleton + start/stop)

**Files:**
- Create: `src/serve_engine/cluster/remote_agent.py`
- Test: `tests/unit/test_remote_agent.py`

`RemoteAgentLink` holds the live WebSocket to one agent, owns request_id → future maps for op_result, and stream_id → queue maps for HTTP chunks. This task wires `start_deployment` / `stop_deployment` and tests them against a fake WS pair (using two `asyncio.Queue`s instead of a real socket).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_remote_agent.py
from __future__ import annotations

import asyncio
import pytest

from serve_engine.cluster.remote_agent import RemoteAgentLink
from serve_engine.cluster.protocol import (
    OpResult, StartDeployment, StopDeployment, decode_frame, encode_frame,
)


class _FakeWS:
    """Two queues simulating a duplex WS."""
    def __init__(self):
        self.outbound: asyncio.Queue[str] = asyncio.Queue()  # what agent receives
        self.inbound: asyncio.Queue[str] = asyncio.Queue()   # what leader receives
        self.closed = False

    async def send(self, msg: str): await self.outbound.put(msg)
    async def recv(self) -> str:
        m = await self.inbound.get()
        if m is None:
            raise ConnectionError("closed")
        return m

    async def push_from_agent(self, frame_str: str):
        await self.inbound.put(frame_str)


@pytest.mark.asyncio
async def test_start_deployment_roundtrip():
    ws = _FakeWS()
    link = RemoteAgentLink(node_id=7, ws=ws)
    task = asyncio.create_task(link.run())  # consume frames

    async def agent_replies():
        sent = await ws.outbound.get()
        f = decode_frame(sent)
        assert isinstance(f, StartDeployment)
        await ws.push_from_agent(encode_frame(OpResult(
            request_id=f.request_id, ok=True,
            data={"container_id": "cid-77", "address": "tunnel", "port": 0},
        )))

    asyncio.create_task(agent_replies())
    started = await link.start_deployment({"image": "x", "name": "d"})
    assert started.container_id == "cid-77"
    assert started.address == "tunnel"

    link.shutdown()
    await ws.push_from_agent(None)  # type: ignore[arg-type]
    await asyncio.gather(task, return_exceptions=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_remote_agent.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/remote_agent.py`:

```python
from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from typing import Any

from serve_engine.cluster.agent_link import ProxyResponseChunk, StartedContainer
from serve_engine.cluster.protocol import (
    Frame, HttpCancel, HttpChunk, HttpRequest, OpResult,
    StartDeployment, StopDeployment, decode_frame, encode_frame,
)


class _WSProto:
    """Subset of the websocket interface we use; declared as a Protocol-like
    duck type so both `websockets` clients and FastAPI's WebSocket fit."""
    async def send(self, msg: str) -> None: ...
    async def recv(self) -> str | None: ...


class RemoteAgentLink:
    def __init__(self, *, node_id: int, ws: _WSProto) -> None:
        self._node_id = node_id
        self._ws = ws
        self._pending_ops: dict[str, asyncio.Future[OpResult]] = {}
        self._streams: dict[str, asyncio.Queue[HttpChunk]] = {}
        self._send_lock = asyncio.Lock()
        self._shutdown = False
        self._ready = True

    @property
    def node_id(self) -> int:
        return self._node_id

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._shutdown

    def shutdown(self) -> None:
        self._shutdown = True
        self._ready = False
        for fut in self._pending_ops.values():
            if not fut.done():
                fut.set_exception(ConnectionError("agent disconnected"))
        for q in self._streams.values():
            q.put_nowait(HttpChunk(stream_id="", body_b64="", eof=True))

    async def _send(self, frame: Frame) -> None:
        async with self._send_lock:
            await self._ws.send(encode_frame(frame))

    async def run(self) -> None:
        """Consume frames from the WS forever (until close / shutdown)."""
        try:
            while not self._shutdown:
                raw = await self._ws.recv()
                if raw is None:
                    break
                f = decode_frame(raw)
                if isinstance(f, OpResult):
                    fut = self._pending_ops.pop(f.request_id, None)
                    if fut and not fut.done():
                        fut.set_result(f)
                elif isinstance(f, HttpChunk):
                    q = self._streams.get(f.stream_id)
                    if q is not None:
                        await q.put(f)
                # Other frame types (heartbeat, gpu_stats) are handled by
                # the leader hub layer, not by RemoteAgentLink. They won't
                # reach here in practice; ignore safely if they do.
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.shutdown()

    async def _request_op(self, frame: Frame, request_id: str) -> OpResult:
        fut: asyncio.Future[OpResult] = asyncio.get_running_loop().create_future()
        self._pending_ops[request_id] = fut
        await self._send(frame)
        return await fut

    async def start_deployment(self, plan: dict[str, Any]) -> StartedContainer:
        rid = secrets.token_hex(8)
        res = await self._request_op(StartDeployment(request_id=rid, plan=plan), rid)
        if not res.ok or res.data is None:
            raise RuntimeError(res.error or "start_deployment failed")
        return StartedContainer(
            container_id=res.data["container_id"],
            address=res.data.get("address", "tunnel"),
            port=int(res.data.get("port", 0)),
        )

    async def stop_deployment(self, container_id: str, *, remove: bool = True) -> None:
        rid = secrets.token_hex(8)
        res = await self._request_op(
            StopDeployment(request_id=rid, container_id=container_id), rid,
        )
        if not res.ok:
            raise RuntimeError(res.error or "stop_deployment failed")

    async def proxy_request(
        self, *, container_id: str, method: str, path: str,
        headers: dict[str, str], body: bytes,
    ) -> AsyncIterator[ProxyResponseChunk]:
        import base64
        stream_id = secrets.token_hex(8)
        q: asyncio.Queue[HttpChunk] = asyncio.Queue()
        self._streams[stream_id] = q
        try:
            await self._send(HttpRequest(
                stream_id=stream_id, method=method, path=path,
                headers={**headers, "x-serve-container-id": container_id},
                body_b64=base64.b64encode(body).decode("ascii"),
            ))
            first = True
            while True:
                chunk = await q.get()
                body_bytes = base64.b64decode(chunk.body_b64) if chunk.body_b64 else b""
                yield ProxyResponseChunk(
                    status=chunk.status if first else None,
                    headers=chunk.headers if first else None,
                    body=body_bytes, eof=chunk.eof,
                )
                first = False
                if chunk.eof:
                    break
        finally:
            self._streams.pop(stream_id, None)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_remote_agent.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cluster/remote_agent.py tests/unit/test_remote_agent.py
git commit -m "feat(cluster): RemoteAgentLink — WS-backed start/stop/proxy"
```

---

## Task 14: Leader hub — WebSocket endpoint + mTLS verify + welcome handshake

**Files:**
- Create: `src/serve_engine/cluster/leader_hub.py`
- Modify: `src/serve_engine/daemon/app.py`
- Test: `tests/unit/test_leader_hub.py`

The hub is the FastAPI side: agents connect to `/cluster/agent` over wss, present their client cert, hub looks up the fingerprint in `nodes`, accepts the connection, and registers a `RemoteAgentLink` in the `AgentRegistry`. When the connection drops, hub unregisters and marks the node `unreachable`.

mTLS verification: agents connect via the leader's TLS listener (terminated by the operator's reverse proxy in production *or* directly when configured). For v1, the leader accepts a verified client cert and passes the fingerprint via header `x-serve-client-fingerprint` (set by the reverse proxy) OR via TLS state when the daemon terminates TLS directly. Document both modes; default to the TLS-direct path in the unit test by stubbing the verify.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_leader_hub.py
from __future__ import annotations

import asyncio
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.leader_hub import LeaderHub
from serve_engine.cluster.protocol import Hello, Welcome, decode_frame, encode_frame
from serve_engine.store.db import open_db
from serve_engine.store import nodes as nodes_store


@pytest.fixture
def app_with_hub(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="sha256:aaa",
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    reg = AgentRegistry()
    hub = LeaderHub(conn=conn, registry=reg,
                    fingerprint_resolver=lambda ws: "sha256:aaa")
    app = FastAPI()
    app.include_router(hub.router)
    return app, reg, nid


def test_handshake_registers_agent(app_with_hub):
    app, reg, nid = app_with_hub
    client = TestClient(app)
    with client.websocket_connect("/cluster/agent") as ws:
        ws.send_text(encode_frame(Hello(
            agent_version="0.0.1",
            host_info={"cpu_count": 1, "total_ram_mb": 1, "gpus": []},
        )))
        welcome = decode_frame(ws.receive_text())
        assert isinstance(welcome, Welcome)
        assert welcome.node_id == nid
    # On context-manager exit the WS closes; hub should unregister.
    assert reg.get(nid) is None


def test_unknown_fingerprint_rejected(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    reg = AgentRegistry()
    hub = LeaderHub(conn=conn, registry=reg,
                    fingerprint_resolver=lambda ws: "sha256:unknown")
    app = FastAPI()
    app.include_router(hub.router)
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/cluster/agent") as ws:
            ws.send_text("{}")
            ws.receive_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_leader_hub.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/leader_hub.py`:

```python
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.protocol import (
    Frame, Hello, Welcome, decode_frame, encode_frame,
)
from serve_engine.cluster.remote_agent import RemoteAgentLink
from serve_engine.store import nodes as nodes_store

log = logging.getLogger(__name__)


FingerprintResolver = Callable[[WebSocket], str | None]


def _default_fingerprint_resolver(ws: WebSocket) -> str | None:
    """In production the operator's reverse proxy verifies the cert and forwards
    its sha256 as `x-serve-client-fingerprint`. When the daemon terminates TLS
    directly, uvicorn populates ws.scope['extensions']['tls']['client_cert_der']
    — that branch is added in a follow-up plan once we ship a TLS listener.
    For v1 we trust the header path."""
    return ws.headers.get("x-serve-client-fingerprint")


class _WSAdapter:
    """Adapts FastAPI WebSocket to the _WSProto duck type RemoteAgentLink wants."""
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
    async def send(self, msg: str) -> None:
        await self._ws.send_text(msg)
    async def recv(self) -> str | None:
        try:
            return await self._ws.receive_text()
        except WebSocketDisconnect:
            return None


class LeaderHub:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        registry: AgentRegistry,
        fingerprint_resolver: FingerprintResolver = _default_fingerprint_resolver,
    ) -> None:
        self._conn = conn
        self._registry = registry
        self._resolve_fp = fingerprint_resolver
        self.router = APIRouter()
        self.router.add_api_websocket_route("/cluster/agent", self._handle_agent)

    async def _handle_agent(self, ws: WebSocket) -> None:
        fp = self._resolve_fp(ws)
        if fp is None:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        node = nodes_store.find_by_fingerprint(self._conn, fp)
        if node is None:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await ws.accept()
        # Read Hello, send Welcome.
        try:
            hello_text = await ws.receive_text()
            hello = decode_frame(hello_text)
            if not isinstance(hello, Hello):
                await ws.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return
            now = time.time()
            nodes_store.update_inventory(
                self._conn, node.id,
                agent_version=hello.agent_version,
                cpu_count=hello.host_info.get("cpu_count", 0),
                total_ram_mb=hello.host_info.get("total_ram_mb", 0),
                gpu_count=hello.host_info.get("gpu_count", 0),
                total_vram_mb=hello.host_info.get("total_vram_mb", 0),
            )
            nodes_store.set_status(self._conn, node.id, status="ready", last_seen=now)
            await ws.send_text(encode_frame(Welcome(node_id=node.id, server_time=now)))
        except WebSocketDisconnect:
            return

        link = RemoteAgentLink(node_id=node.id, ws=_WSAdapter(ws))
        self._registry.register(link)
        try:
            await link.run()
        finally:
            self._registry.unregister(node.id)
            nodes_store.set_status(self._conn, node.id, status="unreachable", last_seen=time.time())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_leader_hub.py -v`
Expected: PASS.

- [ ] **Step 5: Wire hub + registry into daemon app**

In `src/serve_engine/daemon/app.py`:

```python
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.leader_hub import LeaderHub
from serve_engine.cluster.local_agent import LocalAgentLink

# After conn open + ensure_local_node + docker_client init:
agent_registry = AgentRegistry()
local_node = nodes_store.find_by_label(conn, "local")
local_link = LocalAgentLink(node_id=local_node.id, docker_client=docker_client)
agent_registry.register(local_link)
app.state.agent_registry = agent_registry

hub = LeaderHub(conn=conn, registry=agent_registry)
app.include_router(hub.router)
```

(Adapt to the actual structure of `app.py`; the surrounding code already opens the conn and constructs docker_client.)

- [ ] **Step 6: Run the unit suite**

Run: `pytest tests/unit -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/serve_engine/cluster/leader_hub.py src/serve_engine/daemon/app.py tests/unit/test_leader_hub.py
git commit -m "feat(cluster): WS leader hub — handshake, fingerprint pin, AgentLink registration"
```

---

## Task 15: Admin endpoints — nodes enroll / list / show / remove

**Files:**
- Modify: `src/serve_engine/daemon/admin.py`
- Modify: `src/serve_engine/daemon/app.py` (instantiate EnrollmentTokens, mount CA dir)
- Test: `tests/unit/test_admin_nodes.py`

Endpoints:
- `POST /admin/nodes/enroll`  body `{label: str}` → `{token: str, leader_url: str, ca_cert: str}`
- `GET  /admin/nodes`         → list
- `GET  /admin/nodes/{id}`    → detail
- `DELETE /admin/nodes/{id}`  → revoke + delete

We add `POST /admin/nodes/register` in Task 16 (separate task since it's the cert-exchange path).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_admin_nodes.py
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.unit.testing_helpers import make_app_with_admin_key  # built in this task


def test_enroll_mints_one_time_token():
    app, admin_key = make_app_with_admin_key()
    client = TestClient(app)
    r = client.post(
        "/admin/nodes/enroll",
        json={"label": "agent-a"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "token" in data and len(data["token"]) >= 32
    assert "ca_cert" in data
    # Re-issuing for the same label is fine — agent can re-enroll.
    r2 = client.post(
        "/admin/nodes/enroll",
        json={"label": "agent-a"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert r2.json()["token"] != data["token"]


def test_list_includes_local_node():
    app, admin_key = make_app_with_admin_key()
    client = TestClient(app)
    r = client.get("/admin/nodes",
                   headers={"Authorization": f"Bearer {admin_key}"})
    assert r.status_code == 200
    labels = {n["label"] for n in r.json()["nodes"]}
    assert "local" in labels
```

- [ ] **Step 2: Build `tests/unit/testing_helpers.py`**

Create `tests/unit/testing_helpers.py`:

```python
from __future__ import annotations

import tempfile
from pathlib import Path

# Minimal app factory used by admin endpoint tests. Adapt to the real
# `build_app` entrypoint in serve_engine/daemon/app.py; if it already
# takes overrides for paths/auth/docker, prefer those over reimplementing.
def make_app_with_admin_key():
    from serve_engine.daemon.app import build_app
    tmp = Path(tempfile.mkdtemp())
    app = build_app(
        serve_home=tmp,
        docker_client_factory=lambda: None,  # admin endpoints don't need docker
    )
    # Mint an admin key using the existing key-creation API:
    from serve_engine.store import api_keys as ak
    secret = ak.create(app.state.conn, name="test-admin", tier="admin")
    return app, secret
```

If `build_app` does not exist as a factory yet, this task adds one as a refactor — move the existing top-level app construction into a `build_app(serve_home: Path, ...) -> FastAPI` function in `daemon/app.py`. Keep the existing module-level `app = build_app(...)` for production callers.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/unit/test_admin_nodes.py -v`
Expected: FAIL — endpoints not implemented.

- [ ] **Step 4: Implement endpoints**

In `src/serve_engine/daemon/admin.py`, add:

```python
from pydantic import BaseModel

from serve_engine.cluster.enrollment import EnrollmentTokens
from serve_engine.store import nodes as nodes_store
from serve_engine.store import node_gpus as node_gpus_store


class EnrollBody(BaseModel):
    label: str


@router.post("/admin/nodes/enroll")
def admin_nodes_enroll(body: EnrollBody, request: Request,
                       _: ApiKey = Depends(require_admin)):
    tokens: EnrollmentTokens = request.app.state.enrollment_tokens
    token = tokens.mint(label=body.label)
    leader_url: str = request.app.state.leader_url
    ca_cert: str = request.app.state.ca_cert_pem
    return {"token": token, "leader_url": leader_url, "ca_cert": ca_cert}


@router.get("/admin/nodes")
def admin_nodes_list(request: Request,
                     _: ApiKey = Depends(require_admin)):
    conn = request.app.state.conn
    return {"nodes": [n.__dict__ for n in nodes_store.list_all(conn)]}


@router.get("/admin/nodes/{node_id}")
def admin_nodes_show(node_id: int, request: Request,
                     _: ApiKey = Depends(require_admin)):
    conn = request.app.state.conn
    n = nodes_store.get(conn, node_id)
    if n is None:
        raise HTTPException(404, "node not found")
    return {
        "node": n.__dict__,
        "gpus": [g.__dict__ for g in node_gpus_store.list_for_node(conn, node_id)],
    }


@router.delete("/admin/nodes/{node_id}")
def admin_nodes_remove(node_id: int, request: Request,
                       _: ApiKey = Depends(require_admin)):
    conn = request.app.state.conn
    n = nodes_store.get(conn, node_id)
    if n is None:
        raise HTTPException(404, "node not found")
    if n.label == "local":
        raise HTTPException(400, "cannot remove the local node")
    # Disconnect any live link.
    reg = request.app.state.agent_registry
    link = reg.get(node_id)
    if link is not None:
        reg.unregister(node_id)
    nodes_store.delete(conn, node_id)
    return {"ok": True}
```

In `daemon/app.py`, instantiate the enrollment store and load (or create) the CA on startup:

```python
from serve_engine.cluster.ca import generate_ca, load_ca
from serve_engine.cluster.enrollment import EnrollmentTokens

ca_dir = serve_home / "ca"
if not (ca_dir / "ca.crt").exists():
    generate_ca(ca_dir, common_name="serve-engine-ca")
ca = load_ca(ca_dir)

app.state.ca = ca
app.state.ca_cert_pem = ca.cert_pem.decode("ascii")
app.state.leader_url = os.environ.get("SERVE_LEADER_URL", "https://127.0.0.1:11500")
app.state.enrollment_tokens = EnrollmentTokens()
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/unit/test_admin_nodes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/serve_engine/daemon/admin.py src/serve_engine/daemon/app.py tests/unit/testing_helpers.py tests/unit/test_admin_nodes.py
git commit -m "feat(daemon): /admin/nodes — enroll, list, show, remove"
```

---

## Task 16: Cert exchange — `POST /admin/nodes/register`

**Files:**
- Modify: `src/serve_engine/daemon/admin.py`
- Test: `tests/unit/test_admin_register.py`

Flow: agent sends `{token, host_info}`. Leader consumes the token, mints an agent cert via `issue_agent_cert`, computes its fingerprint, inserts a `nodes` row (with the label the token was bound to) and the agent's GPU inventory, returns `{node_id, agent_cert, agent_key, ca_cert}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_admin_register.py
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.unit.testing_helpers import make_app_with_admin_key


def test_register_with_valid_token_issues_cert():
    app, admin_key = make_app_with_admin_key()
    client = TestClient(app)
    enroll = client.post(
        "/admin/nodes/enroll", json={"label": "agent-a"},
        headers={"Authorization": f"Bearer {admin_key}"},
    ).json()
    r = client.post(
        "/admin/nodes/register",
        json={
            "token": enroll["token"],
            "host_info": {
                "cpu_count": 8, "total_ram_mb": 32000,
                "gpu_count": 1, "total_vram_mb": 81920,
                "gpus": [{"index": 0, "name": "H100",
                          "total_vram_mb": 81920, "driver_version": "555.42"}],
            },
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert "node_id" in data
    assert data["agent_cert"].startswith("-----BEGIN CERTIFICATE-----")
    assert data["agent_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert data["ca_cert"].startswith("-----BEGIN CERTIFICATE-----")


def test_register_with_bad_token_rejected():
    app, _ = make_app_with_admin_key()
    client = TestClient(app)
    r = client.post(
        "/admin/nodes/register",
        json={"token": "garbage", "host_info": {"cpu_count": 1, "total_ram_mb": 1,
                                                "gpu_count": 0, "total_vram_mb": 0, "gpus": []}},
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_admin_register.py -v`
Expected: FAIL — endpoint not implemented.

- [ ] **Step 3: Implement**

In `src/serve_engine/daemon/admin.py`:

```python
from serve_engine.cluster.ca import fingerprint_sha256, issue_agent_cert


class RegisterBody(BaseModel):
    token: str
    host_info: dict


@router.post("/admin/nodes/register")
def admin_nodes_register(body: RegisterBody, request: Request):
    """Bootstrapped via single-use token; no admin auth required at this step
    because the token IS the auth. Successful registration returns the
    durable agent certificate."""
    tokens: EnrollmentTokens = request.app.state.enrollment_tokens
    label = tokens.consume(body.token)
    if label is None:
        raise HTTPException(403, "invalid or expired enrollment token")

    ca = request.app.state.ca
    bundle = issue_agent_cert(ca, label=label)
    fp = fingerprint_sha256(bundle.cert_pem)
    now = time.time()

    conn = request.app.state.conn
    info = body.host_info
    # If a row with this label already exists (re-enrollment), update it.
    existing = nodes_store.find_by_label(conn, label)
    if existing is not None:
        node_id = existing.id
        nodes_store.update_inventory(
            conn, node_id,
            agent_version=info.get("agent_version", "unknown"),
            cpu_count=info.get("cpu_count", 0),
            total_ram_mb=info.get("total_ram_mb", 0),
            gpu_count=info.get("gpu_count", 0),
            total_vram_mb=info.get("total_vram_mb", 0),
        )
        conn.execute("UPDATE nodes SET fingerprint = ? WHERE id = ?", (fp, node_id))
        conn.commit()
    else:
        node_id = nodes_store.insert(
            conn, label=label, fingerprint=fp,
            reachable_as=None, first_seen=now, last_seen=now,
            agent_version=info.get("agent_version", "unknown"),
            cpu_count=info.get("cpu_count", 0),
            total_ram_mb=info.get("total_ram_mb", 0),
            gpu_count=info.get("gpu_count", 0),
            total_vram_mb=info.get("total_vram_mb", 0),
        )
    node_gpus_store.delete_for_node(conn, node_id)
    for g in info.get("gpus", []):
        node_gpus_store.upsert(
            conn, node_id=node_id, gpu_index=g["index"],
            name=g["name"], total_vram_mb=g["total_vram_mb"],
            driver_version=g.get("driver_version"),
        )
    return {
        "node_id": node_id,
        "agent_cert": bundle.cert_pem.decode("ascii"),
        "agent_key": bundle.key_pem.decode("ascii"),
        "ca_cert": request.app.state.ca_cert_pem,
    }
```

Top of `admin.py`, ensure `import time` is present.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_admin_register.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/admin.py tests/unit/test_admin_register.py
git commit -m "feat(daemon): POST /admin/nodes/register — issue agent cert on token exchange"
```

---

## Task 17: WS agent client (transport + reconnect)

**Files:**
- Create: `src/serve_engine/cluster/agent_client.py`
- Test: `tests/unit/test_agent_client.py`

Goal: an `AgentDaemon`-friendly WS client that:
1. Reads the agent identity (cert, key, ca, leader_url, node_id) from `~/.serve/agent.yaml`.
2. Connects via mTLS WSS to `${leader_url}/cluster/agent` with the client cert presented.
3. Sends `Hello`, awaits `Welcome`.
4. Spawns a heartbeat task and a frame-dispatch loop.
5. Handles `StartDeployment`, `StopDeployment`, `HttpRequest`, `HttpCancel` by delegating to a local `DockerClient` (start/stop) and `httpx.AsyncClient` (http_request → engine loopback).
6. On disconnect: exponential backoff and reconnect.

In v1, the agent's frame handlers run inline in the same task that reads from the WS; long-running ops (`start_deployment`) are awaited inside the handler. We can move to a worker pool later if needed.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agent_client.py
from __future__ import annotations

import asyncio
import base64
import json
import pytest

from serve_engine.cluster.agent_client import AgentFrameDispatcher
from serve_engine.cluster.protocol import (
    HttpChunk, HttpRequest, OpResult, StartDeployment, StopDeployment,
    decode_frame, encode_frame,
)


class _DockerStub:
    def __init__(self): self.started = []
    async def start(self, plan): self.started.append(plan); return ("cid-1", "127.0.0.1", 9000)
    async def stop(self, cid, remove): pass


class _HTTPStub:
    async def stream(self, method, url, headers, body):
        async def gen():
            yield (200, {"content-type": "text/plain"}, b"hi", False)
            yield (None, None, b"", True)
        return gen()


@pytest.mark.asyncio
async def test_start_then_op_result():
    sent: list[str] = []
    async def sender(s): sent.append(s)
    disp = AgentFrameDispatcher(docker=_DockerStub(), http=_HTTPStub(), send=sender)
    await disp.handle(StartDeployment(request_id="r1", plan={"image": "x"}))
    res = decode_frame(sent[-1])
    assert isinstance(res, OpResult)
    assert res.ok and res.data["container_id"] == "cid-1"


@pytest.mark.asyncio
async def test_http_request_streams_chunks_back():
    sent: list[str] = []
    async def sender(s): sent.append(s)
    disp = AgentFrameDispatcher(docker=_DockerStub(), http=_HTTPStub(), send=sender)
    # Pre-register an endpoint as if the deployment was already started.
    disp.register_endpoint(container_id="cid-1", address="127.0.0.1", port=9000)
    await disp.handle(HttpRequest(
        stream_id="s1", method="GET", path="/", headers={"x-serve-container-id": "cid-1"},
        body_b64="",
    ))
    chunks = [decode_frame(s) for s in sent]
    assert any(isinstance(c, HttpChunk) and c.eof for c in chunks)
    body = b"".join(base64.b64decode(c.body_b64) for c in chunks if isinstance(c, HttpChunk))
    assert b"hi" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_agent_client.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement frame dispatcher (decoupled from the WS itself for testability)**

Create `src/serve_engine/cluster/agent_client.py`:

```python
from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import websockets
import yaml

from serve_engine.cluster.protocol import (
    Frame, GpuStats, Heartbeat, Hello, HttpCancel, HttpChunk, HttpRequest,
    OpResult, StartDeployment, StopDeployment, Welcome, decode_frame, encode_frame,
)

log = logging.getLogger(__name__)


class AgentFrameDispatcher:
    """Handles inbound frames on the agent side. Side-effect-free except for
    the docker / http stubs it's constructed with — easy to unit-test."""

    def __init__(
        self,
        *,
        docker: Any,   # has async start(plan) -> (cid, addr, port); async stop(cid, remove)
        http: Any,     # has async stream(method, url, headers, body) -> async iter of (status, headers, body, eof)
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        self._docker = docker
        self._http = http
        self._send = send
        self._endpoints: dict[str, tuple[str, int]] = {}
        self._inflight_streams: dict[str, asyncio.Task[None]] = {}

    def register_endpoint(self, *, container_id: str, address: str, port: int) -> None:
        self._endpoints[container_id] = (address, port)

    async def handle(self, frame: Frame) -> None:
        if isinstance(frame, StartDeployment):
            try:
                cid, addr, port = await self._docker.start(frame.plan)
                self._endpoints[cid] = (addr, port)
                await self._send(encode_frame(OpResult(
                    request_id=frame.request_id, ok=True,
                    data={"container_id": cid, "address": "tunnel", "port": 0},
                )))
            except Exception as e:  # noqa: BLE001 — surface to leader
                await self._send(encode_frame(OpResult(
                    request_id=frame.request_id, ok=False, error=str(e),
                )))
        elif isinstance(frame, StopDeployment):
            try:
                await self._docker.stop(frame.container_id, remove=True)
                self._endpoints.pop(frame.container_id, None)
                await self._send(encode_frame(OpResult(
                    request_id=frame.request_id, ok=True,
                )))
            except Exception as e:  # noqa: BLE001
                await self._send(encode_frame(OpResult(
                    request_id=frame.request_id, ok=False, error=str(e),
                )))
        elif isinstance(frame, HttpRequest):
            self._inflight_streams[frame.stream_id] = asyncio.create_task(
                self._run_http_stream(frame)
            )
        elif isinstance(frame, HttpCancel):
            t = self._inflight_streams.pop(frame.stream_id, None)
            if t is not None:
                t.cancel()
        # Heartbeats, Hello/Welcome handled by AgentClient itself, not here.

    async def _run_http_stream(self, frame: HttpRequest) -> None:
        cid = frame.headers.get("x-serve-container-id")
        endpoint = self._endpoints.get(cid or "")
        if endpoint is None:
            await self._send(encode_frame(HttpChunk(
                stream_id=frame.stream_id, status=502,
                headers={"x-serve-error": "no-endpoint"},
                body_b64="", eof=True,
            )))
            return
        addr, port = endpoint
        body = base64.b64decode(frame.body_b64) if frame.body_b64 else b""
        url = f"http://{addr}:{port}{frame.path}"
        headers = {k: v for k, v in frame.headers.items()
                   if k != "x-serve-container-id"}
        try:
            agen = await self._http.stream(frame.method, url, headers, body)
            async for status, hdrs, chunk, eof in agen:
                await self._send(encode_frame(HttpChunk(
                    stream_id=frame.stream_id, status=status, headers=hdrs,
                    body_b64=base64.b64encode(chunk).decode("ascii") if chunk else "",
                    eof=eof,
                )))
        except Exception as e:  # noqa: BLE001
            await self._send(encode_frame(HttpChunk(
                stream_id=frame.stream_id, status=502,
                headers={"x-serve-error": "proxy-failed", "x-serve-detail": str(e)[:200]},
                body_b64="", eof=True,
            )))
        finally:
            self._inflight_streams.pop(frame.stream_id, None)
```

- [ ] **Step 4: Run dispatcher tests to verify pass**

Run: `pytest tests/unit/test_agent_client.py -v`
Expected: PASS.

- [ ] **Step 5: Add the AgentClient runner (WS connect + reconnect loop)**

Append to `src/serve_engine/cluster/agent_client.py`:

```python
def _load_agent_config(serve_home: Path) -> dict:
    p = serve_home / "agent.yaml"
    if not p.exists():
        raise FileNotFoundError(
            f"agent config not found at {p}; run `serve agent register` first"
        )
    with p.open() as f:
        return yaml.safe_load(f)


def _build_ssl_context(cfg: dict) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(cfg["ca_cert_path"]))
    ctx.load_cert_chain(
        certfile=str(cfg["agent_cert_path"]),
        keyfile=str(cfg["agent_key_path"]),
    )
    return ctx


class _DockerAdapter:
    def __init__(self, dc): self._dc = dc
    async def start(self, plan):
        h = await asyncio.to_thread(
            self._dc.run,
            image=plan["image"], name=plan["name"], command=plan["command"],
            environment=plan["environment"], kwargs=plan["kwargs"],
            volumes=plan["volumes"], internal_port=plan["internal_port"],
        )
        return (h.id, h.address, h.port)
    async def stop(self, cid, *, remove):
        await asyncio.to_thread(self._dc.stop, cid, remove=remove)


class _HttpxAdapter:
    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=30.0),
        )
    async def stream(self, method, url, headers, body):
        async def gen():
            async with self._client.stream(method, url, headers=headers, content=body) as resp:
                first = True
                async for chunk in resp.aiter_raw():
                    yield (resp.status_code if first else None,
                           dict(resp.headers) if first else None,
                           chunk, False)
                    first = False
                yield (resp.status_code if first else None,
                       dict(resp.headers) if first else None,
                       b"", True)
        return gen()


async def run_agent(serve_home: Path) -> None:
    from serve_engine import __version__ as _v
    from serve_engine.cluster.host_info import collect_host_info
    from serve_engine.lifecycle.docker_client import DockerClient

    cfg = _load_agent_config(serve_home)
    ssl_ctx = _build_ssl_context(cfg)
    docker = _DockerAdapter(DockerClient(network_name="serve"))
    http = _HttpxAdapter()

    ws_url = cfg["leader_url"].replace("https://", "wss://").rstrip("/") + "/cluster/agent"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(ws_url, ssl=ssl_ctx) as ws:
                backoff = 1.0  # reset on success
                async def send(s): await ws.send(s)
                disp = AgentFrameDispatcher(docker=docker, http=http, send=send)
                info = collect_host_info()
                await ws.send(encode_frame(Hello(
                    agent_version=_v,
                    host_info={
                        "cpu_count": info.cpu_count,
                        "total_ram_mb": info.total_ram_mb,
                        "gpu_count": info.gpu_count,
                        "total_vram_mb": info.total_vram_mb,
                        "gpus": [g.__dict__ for g in info.gpus],
                    },
                )))
                welcome = decode_frame(await ws.recv())
                if not isinstance(welcome, Welcome):
                    raise RuntimeError(f"unexpected handshake reply: {welcome}")
                log.info("agent connected to leader as node_id=%s", welcome.node_id)

                # Heartbeat task
                async def heartbeat():
                    while True:
                        import time as _time
                        await ws.send(encode_frame(Heartbeat(ts=_time.time())))
                        await asyncio.sleep(5.0)
                hb = asyncio.create_task(heartbeat())

                try:
                    async for raw in ws:
                        try:
                            frame = decode_frame(raw)
                        except ValueError as e:
                            log.warning("dropping unknown frame: %s", e)
                            continue
                        if isinstance(frame, Heartbeat):
                            continue
                        await disp.handle(frame)
                finally:
                    hb.cancel()
        except (OSError, websockets.WebSocketException) as e:
            log.warning("agent connection lost: %s; reconnecting in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
```

- [ ] **Step 6: Commit**

```bash
git add src/serve_engine/cluster/agent_client.py tests/unit/test_agent_client.py
git commit -m "feat(cluster): agent WS client — frame dispatcher + reconnect loop"
```

---

## Task 18: `serve agent register` CLI

**Files:**
- Create: `src/serve_engine/cli/agent_cmd.py`
- Modify: `src/serve_engine/cli/__init__.py`
- Test: `tests/unit/test_cli_agent_register.py`

Calls `POST /admin/nodes/register`, writes `~/.serve/agent.yaml` and the cert/key/ca to disk.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cli_agent_register.py
from __future__ import annotations

import yaml
from typer.testing import CliRunner
import httpx

from serve_engine.cli import app


def test_register_writes_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVE_HOME", str(tmp_path))

    class _MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "node_id": 7,
                "agent_cert": "-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n",
                "agent_key":  "-----BEGIN PRIVATE KEY-----\nB\n-----END PRIVATE KEY-----\n",
                "ca_cert":    "-----BEGIN CERTIFICATE-----\nC\n-----END CERTIFICATE-----\n",
            }
    def _post(url, json, timeout=None):
        assert url.endswith("/admin/nodes/register")
        assert json["token"] == "tok-123"
        return _MockResp()
    monkeypatch.setattr(httpx, "post", _post)

    r = CliRunner().invoke(app, [
        "agent", "register",
        "--leader", "https://leader.example:11500",
        "--token", "tok-123",
    ])
    assert r.exit_code == 0, r.output
    cfg = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    assert cfg["leader_url"] == "https://leader.example:11500"
    assert cfg["node_id"] == 7
    assert (tmp_path / "agent.crt").exists()
    assert (tmp_path / "agent.key").exists()
    assert (tmp_path / "ca.crt").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_cli_agent_register.py -v`
Expected: FAIL — `agent` subcommand missing.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cli/agent_cmd.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import httpx
import typer
import yaml

from serve_engine.cluster.host_info import collect_host_info

app = typer.Typer(help="Manage the local agent on this host.")


def _serve_home() -> Path:
    return Path(os.environ.get("SERVE_HOME", str(Path.home() / ".serve")))


@app.command()
def register(
    leader: str = typer.Option(..., help="https://<leader-host>:<port>"),
    token: str = typer.Option(..., help="one-time enrollment token from `serve nodes enroll`"),
    reachable_as: str | None = typer.Option(
        None, help="(future) LAN address for direct routing; unused in tunneled mode"
    ),
):
    """Exchange a one-time token for a durable agent cert. Writes config to
    $SERVE_HOME (default ~/.serve)."""
    home = _serve_home()
    home.mkdir(parents=True, exist_ok=True)

    info = collect_host_info()
    payload = {
        "token": token,
        "host_info": {
            "cpu_count": info.cpu_count,
            "total_ram_mb": info.total_ram_mb,
            "gpu_count": info.gpu_count,
            "total_vram_mb": info.total_vram_mb,
            "gpus": [g.__dict__ for g in info.gpus],
        },
    }
    r = httpx.post(f"{leader.rstrip('/')}/admin/nodes/register",
                   json=payload, timeout=30.0)
    r.raise_for_status()
    data = r.json()

    (home / "agent.crt").write_text(data["agent_cert"])
    (home / "agent.key").write_text(data["agent_key"])
    os.chmod(home / "agent.key", 0o600)
    (home / "ca.crt").write_text(data["ca_cert"])
    cfg = {
        "leader_url": leader,
        "node_id": data["node_id"],
        "agent_cert_path": str(home / "agent.crt"),
        "agent_key_path":  str(home / "agent.key"),
        "ca_cert_path":    str(home / "ca.crt"),
        "reachable_as": reachable_as,
    }
    (home / "agent.yaml").write_text(yaml.safe_dump(cfg))
    typer.echo(f"registered as node_id={data['node_id']}")


@app.command()
def start():
    """Run the agent daemon in the foreground (connects to leader over mTLS WS)."""
    import asyncio
    from serve_engine.cluster.agent_client import run_agent
    asyncio.run(run_agent(_serve_home()))


@app.command()
def status():
    home = _serve_home()
    cfg_path = home / "agent.yaml"
    if not cfg_path.exists():
        typer.echo("not registered")
        raise typer.Exit(1)
    cfg = yaml.safe_load(cfg_path.read_text())
    typer.echo(f"node_id  : {cfg['node_id']}")
    typer.echo(f"leader   : {cfg['leader_url']}")
    typer.echo(f"cert     : {cfg['agent_cert_path']}")
```

Register the subcommand. In `src/serve_engine/cli/__init__.py`:

```python
from serve_engine.cli.agent_cmd import app as _agent_app
app.add_typer(_agent_app, name="agent")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_cli_agent_register.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cli/agent_cmd.py src/serve_engine/cli/__init__.py tests/unit/test_cli_agent_register.py
git commit -m "feat(cli): serve agent register/start/status"
```

---

## Task 19: `serve nodes` CLI

**Files:**
- Create: `src/serve_engine/cli/nodes_cmd.py`
- Modify: `src/serve_engine/cli/__init__.py`
- Test: `tests/unit/test_cli_nodes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cli_nodes.py
from __future__ import annotations

import json
import httpx
from typer.testing import CliRunner

from serve_engine.cli import app


def test_nodes_ls(monkeypatch):
    class _MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"nodes": [
                {"id": 0, "label": "local", "status": "ready", "gpu_count": 1, "total_vram_mb": 80000, "agent_version": "0.0.1"},
                {"id": 1, "label": "agent-a", "status": "ready", "gpu_count": 2, "total_vram_mb": 160000, "agent_version": "0.0.1"},
            ]}
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: _MockResp())
    monkeypatch.setenv("SERVE_URL", "http://x")
    monkeypatch.setenv("SERVE_TOKEN", "sk-abc")
    r = CliRunner().invoke(app, ["nodes", "ls"])
    assert r.exit_code == 0
    assert "local" in r.output and "agent-a" in r.output


def test_nodes_enroll(monkeypatch):
    class _MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"token": "tok-xyz", "leader_url": "https://leader:11500",
                    "ca_cert": "ca-pem"}
    monkeypatch.setattr(httpx, "post",
                        lambda url, json=None, headers=None, timeout=None: _MockResp())
    monkeypatch.setenv("SERVE_URL", "http://x")
    monkeypatch.setenv("SERVE_TOKEN", "sk-abc")
    r = CliRunner().invoke(app, ["nodes", "enroll", "agent-a"])
    assert r.exit_code == 0
    assert "tok-xyz" in r.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_cli_nodes.py -v`
Expected: FAIL — `nodes` subcommand missing.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cli/nodes_cmd.py`:

```python
from __future__ import annotations

import os

import httpx
import typer

app = typer.Typer(help="Manage cluster nodes from the leader.")


def _auth_headers() -> dict[str, str]:
    tok = os.environ.get("SERVE_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _base() -> str:
    return os.environ.get("SERVE_URL", "http://127.0.0.1:11500").rstrip("/")


@app.command("ls")
def list_nodes():
    r = httpx.get(f"{_base()}/admin/nodes", headers=_auth_headers(), timeout=10.0)
    r.raise_for_status()
    rows = r.json()["nodes"]
    typer.echo(f"{'ID':<4} {'LABEL':<20} {'STATUS':<14} {'GPUs':<5} {'VRAM MB':>10} {'VERSION'}")
    for n in rows:
        typer.echo(
            f"{n['id']:<4} {n['label']:<20} {n['status']:<14} "
            f"{n['gpu_count']:<5} {n['total_vram_mb']:>10} {n.get('agent_version') or '-'}"
        )


@app.command()
def show(node_id: int):
    r = httpx.get(f"{_base()}/admin/nodes/{node_id}",
                  headers=_auth_headers(), timeout=10.0)
    r.raise_for_status()
    data = r.json()
    n = data["node"]
    typer.echo(f"label    : {n['label']}")
    typer.echo(f"status   : {n['status']}")
    typer.echo(f"version  : {n.get('agent_version') or '-'}")
    typer.echo(f"cpus     : {n['cpu_count']}, ram_mb: {n['total_ram_mb']}")
    typer.echo("gpus:")
    for g in data["gpus"]:
        typer.echo(f"  [{g['gpu_index']}] {g['name']} {g['total_vram_mb']} MB")


@app.command()
def enroll(label: str):
    """Mint a one-time enrollment token for a new agent."""
    r = httpx.post(f"{_base()}/admin/nodes/enroll",
                   json={"label": label},
                   headers=_auth_headers(), timeout=10.0)
    r.raise_for_status()
    data = r.json()
    typer.echo("Enrollment token (single-use, expires in 10 min):")
    typer.echo(f"  token      : {data['token']}")
    typer.echo(f"  leader_url : {data['leader_url']}")
    typer.echo("")
    typer.echo("On the agent host run:")
    typer.echo(
        f"  serve agent register --leader {data['leader_url']} --token {data['token']}"
    )


@app.command()
def remove(node_id: int):
    r = httpx.delete(f"{_base()}/admin/nodes/{node_id}",
                     headers=_auth_headers(), timeout=10.0)
    r.raise_for_status()
    typer.echo(f"removed node {node_id}")
```

Register in `src/serve_engine/cli/__init__.py`:

```python
from serve_engine.cli.nodes_cmd import app as _nodes_app
app.add_typer(_nodes_app, name="nodes")
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_cli_nodes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/cli/nodes_cmd.py src/serve_engine/cli/__init__.py tests/unit/test_cli_nodes.py
git commit -m "feat(cli): serve nodes ls/show/enroll/remove"
```

---

## Task 20: Placement — extend to (node, gpu) candidates

**Files:**
- Modify: `src/serve_engine/lifecycle/placement.py`
- Test: `tests/unit/test_placement_multinode.py`

Today's `plan_placement(topology, allocated, req)` operates on one Topology of GPUs. We add `plan_placement_multi(nodes_topology, req)` where `nodes_topology` is a mapping `node_id -> (Topology, list[AllocatedDeployment])`. The new function tries each ready node in turn and returns `(node_id, Decision)`. The existing function is preserved; the manager calls the new one.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_placement_multinode.py
from __future__ import annotations

import pytest

from serve_engine.lifecycle.placement import (
    AllocatedDeployment, Fit, NoRoom, PlacementRequest,
    plan_placement_multi,
)
from serve_engine.lifecycle.topology import Topology, GpuInfo


def _topo(gpus_mb: list[int]) -> Topology:
    return Topology(
        gpus=[GpuInfo(index=i, name="g", total_mb=mb) for i, mb in enumerate(gpus_mb)],
        nvlink_islands=[frozenset([i]) for i in range(len(gpus_mb))],
    )


def test_picks_first_node_that_fits():
    nodes = {
        1: (_topo([10_000]), []),
        2: (_topo([100_000]), []),
    }
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=50_000, model_name="m")
    node_id, decision = plan_placement_multi(nodes, req)
    assert node_id == 2
    assert isinstance(decision, Fit)
    assert decision.gpu_ids == [0]


def test_no_room_when_none_fit():
    nodes = {
        1: (_topo([1_000]), []),
        2: (_topo([1_000]), []),
    }
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=50_000, model_name="m")
    node_id, decision = plan_placement_multi(nodes, req)
    assert node_id is None
    assert isinstance(decision, NoRoom)


def test_prefers_node_with_more_headroom_on_tie():
    nodes = {
        1: (_topo([60_000]), []),
        2: (_topo([100_000]), []),
    }
    req = PlacementRequest(tensor_parallel=1, vram_reserved_mb=50_000, model_name="m")
    node_id, _ = plan_placement_multi(nodes, req)
    assert node_id == 2  # more headroom wins
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_placement_multinode.py -v`
Expected: FAIL — function not defined.

- [ ] **Step 3: Implement**

In `src/serve_engine/lifecycle/placement.py`, add at bottom:

```python
def _max_free_mb(topo: Topology, allocated: list[AllocatedDeployment]) -> int:
    avail = _available_mb(topo, allocated)
    return max(avail.values(), default=0)


def plan_placement_multi(
    nodes: dict[int, tuple[Topology, list[AllocatedDeployment]]],
    req: PlacementRequest,
) -> tuple[int | None, Decision]:
    """Pick a (node_id, Decision) pair. Tries each candidate node in
    decreasing free-VRAM order and returns the first Fit it finds.
    If no node Fits but at least one EvictThenFit is possible, returns the
    least-disruptive one. Otherwise NoRoom."""
    ordered = sorted(
        nodes.items(),
        key=lambda kv: -_max_free_mb(kv[1][0], kv[1][1]),
    )
    evict_fallback: tuple[int, EvictThenFit] | None = None
    for node_id, (topo, allocated) in ordered:
        decision = plan_placement(topo, allocated, req)
        if isinstance(decision, Fit):
            return node_id, decision
        if isinstance(decision, EvictThenFit) and evict_fallback is None:
            evict_fallback = (node_id, decision)
    if evict_fallback is not None:
        return evict_fallback[0], evict_fallback[1]
    return None, NoRoom(reason="no node has room")
```

If `plan_placement` doesn't already exist as a function name in this file, check the current entrypoint (e.g. `EvictThenFit`-aware single-node planner) and route through it. Adapt the call site accordingly.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/unit/test_placement_multinode.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/lifecycle/placement.py tests/unit/test_placement_multinode.py
git commit -m "feat(placement): multi-node candidate selection"
```

---

## Task 21: Lifecycle manager — dispatch start/stop through AgentLink

**Files:**
- Modify: `src/serve_engine/lifecycle/manager.py`
- Modify: `src/serve_engine/store/deployments.py` (read/write `node_id`)
- Test: `tests/integration/test_manager_multinode.py`

Today `LifecycleManager.start_deployment(...)` calls `self._docker.run(...)` directly. We change it to:
1. Build per-node topology+allocated state from `nodes` + `deployments`.
2. Call `plan_placement_multi` to pick a node.
3. Look up `AgentLink` for that node via `AgentRegistry`.
4. Call `link.start_deployment(plan)`.
5. Persist `node_id` on the deployment row.

`stop_deployment` looks up the deployment's `node_id`, gets the link, calls `link.stop_deployment`.

- [ ] **Step 1: Write the failing unit test**

This is a unit test against the manager's `_dispatch_start` seam — not a full app boot. It verifies the routing logic without needing FastAPI, auth, or real Docker.

```python
# tests/integration/test_manager_multinode.py  (unit-level despite the dir)
from __future__ import annotations

import pytest

from serve_engine.cluster.agent_link import StartedContainer
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.lifecycle.manager import _dispatch_start, _dispatch_stop


class _FakeLink:
    def __init__(self, nid):
        self._nid = nid
        self.started: list[dict] = []
        self.stopped: list[str] = []
    @property
    def node_id(self): return self._nid
    @property
    def is_ready(self): return True
    async def start_deployment(self, plan):
        self.started.append(plan)
        return StartedContainer(
            container_id=f"cid-{self._nid}", address="tunnel", port=0,
        )
    async def stop_deployment(self, cid, *, remove=True):
        self.stopped.append(cid)


@pytest.mark.asyncio
async def test_dispatch_start_calls_link_for_chosen_node():
    reg = AgentRegistry()
    link_a = _FakeLink(1)
    link_b = _FakeLink(2)
    reg.register(link_a)
    reg.register(link_b)
    plan = {"image": "x", "name": "d-1", "command": [], "environment": {},
            "kwargs": {}, "volumes": {}, "internal_port": 9000}
    started = await _dispatch_start(reg, node_id=2, plan=plan)
    assert started.container_id == "cid-2"
    assert link_b.started == [plan]
    assert link_a.started == []


@pytest.mark.asyncio
async def test_dispatch_start_raises_when_node_not_connected():
    reg = AgentRegistry()
    reg.register(_FakeLink(1))
    plan = {"image": "x", "name": "d", "command": [], "environment": {},
            "kwargs": {}, "volumes": {}, "internal_port": 0}
    with pytest.raises(RuntimeError, match="not connected"):
        await _dispatch_start(reg, node_id=99, plan=plan)


@pytest.mark.asyncio
async def test_dispatch_stop_calls_link_for_dep_node():
    reg = AgentRegistry()
    link = _FakeLink(7)
    reg.register(link)
    await _dispatch_stop(reg, node_id=7, container_id="cid-7")
    assert link.stopped == ["cid-7"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_manager_multinode.py -v`
Expected: FAIL — manager still calls DockerClient directly.

- [ ] **Step 3: Add dispatch seams to manager**

In `src/serve_engine/lifecycle/manager.py`, add two module-level helpers near the top (after imports), so they are unit-testable independently of `LifecycleManager`:

```python
from serve_engine.cluster.agent_link import StartedContainer
from serve_engine.cluster.agent_registry import AgentRegistry


async def _dispatch_start(
    registry: AgentRegistry, *, node_id: int, plan: dict,
) -> StartedContainer:
    link = registry.get(node_id)
    if link is None or not link.is_ready:
        raise RuntimeError(f"node {node_id} not connected")
    return await link.start_deployment(plan)


async def _dispatch_stop(
    registry: AgentRegistry, *, node_id: int, container_id: str,
) -> None:
    link = registry.get(node_id)
    if link is None:
        raise RuntimeError(f"node {node_id} not connected")
    await link.stop_deployment(container_id)
```

Then in `LifecycleManager`:

1. Accept `agent_registry: AgentRegistry` in `__init__` and store as `self._registry`.
2. In the existing start path, after `plan_placement` (now `plan_placement_multi`) chooses a `(node_id, decision)`, replace the direct `self._docker.run(...)` block with:
   ```python
   started = await _dispatch_start(self._registry, node_id=chosen_node_id, plan=plan_dict)
   dep_store.set_runtime(
       self._conn, dep_id,
       node_id=chosen_node_id,
       container_id=started.container_id,
       address=started.address, port=started.port,
   )
   ```
3. Replace the direct `self._docker.stop(...)` block in the stop path with:
   ```python
   row = dep_store.get(self._conn, dep_id)
   await _dispatch_stop(self._registry, node_id=row.node_id, container_id=row.container_id)
   ```

In `src/serve_engine/store/deployments.py`:

- Extend the deployment row dataclass to include `node_id: int` (column exists from migration 014). The existing `address`/`port` fields stay where they are; this task simply ensures they are populated together with `node_id` on writes.
- Add a `set_runtime` function:
  ```python
  def set_runtime(
      conn, dep_id: int, *,
      node_id: int, container_id: str, address: str, port: int,
  ) -> None:
      conn.execute(
          "UPDATE deployments SET node_id=?, container_id=?, address=?, port=? WHERE id=?",
          (node_id, container_id, address, port, dep_id),
      )
      conn.commit()
  ```
- Ensure `get()` and `list()` SELECTs include `node_id` and the dataclass receives it.

Engineer note: the existing deployments table already has a `container_id`-equivalent column (verify the exact name — earlier migrations call it `container_id`); if the column name differs, adjust the SQL and dataclass accordingly. Do not rename the existing column.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/integration -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/lifecycle/manager.py src/serve_engine/store/deployments.py tests/integration/test_manager_multinode.py
git commit -m "feat(lifecycle): dispatch deployment start/stop through AgentLink"
```

---

## Task 22: Data plane — openai_proxy uses AgentLink

**Files:**
- Modify: `src/serve_engine/daemon/openai_proxy.py`
- Test: `tests/integration/test_openai_proxy_via_link.py`

Today `_proxy` builds an `httpx.AsyncClient` against `127.0.0.1:engine_port` directly. We change it to resolve the deployment's `node_id`, look up the AgentLink, and call `link.proxy_request(...)`, streaming `ProxyResponseChunk`s back as a FastAPI `StreamingResponse`.

For `node_id == local_node`, this is a transparent no-op (LocalAgentLink calls the same httpx). For remote nodes, it goes through the WS tunnel.

- [ ] **Step 1: Write the failing test**

This is a focused test on the new dispatch helper (`_proxy_via_link`) that this task introduces — avoids depending on full app construction.

```python
# tests/integration/test_openai_proxy_via_link.py  (unit-level despite the dir)
from __future__ import annotations

import pytest

from serve_engine.cluster.agent_link import ProxyResponseChunk
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.daemon.openai_proxy import _proxy_via_link


class _FakeLink:
    def __init__(self, nid):
        self._nid = nid
        self.calls: list[dict] = []
    @property
    def node_id(self): return self._nid
    @property
    def is_ready(self): return True
    async def start_deployment(self, plan): raise NotImplementedError
    async def stop_deployment(self, cid, *, remove=True): raise NotImplementedError
    async def proxy_request(self, *, container_id, method, path, headers, body):
        self.calls.append({"container_id": container_id, "method": method,
                           "path": path, "headers": headers, "body": body})
        yield ProxyResponseChunk(
            status=200, headers={"content-type": "text/plain"},
            body=b"hello ", eof=False,
        )
        yield ProxyResponseChunk(status=None, headers=None, body=b"world", eof=False)
        yield ProxyResponseChunk(status=None, headers=None, body=b"", eof=True)


@pytest.mark.asyncio
async def test_proxy_via_link_streams_chunks_with_first_status():
    reg = AgentRegistry()
    link = _FakeLink(7)
    reg.register(link)
    status_code, headers, body_chunks = await _proxy_via_link(
        registry=reg, node_id=7, container_id="cid-x",
        method="POST", path="/v1/chat/completions",
        headers={"authorization": "Bearer x"}, body=b'{"model":"y"}',
    )
    assert status_code == 200
    assert headers["content-type"] == "text/plain"
    assert b"".join(body_chunks) == b"hello world"
    assert link.calls[0]["container_id"] == "cid-x"
    assert link.calls[0]["path"] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_proxy_via_link_raises_when_node_unreachable():
    reg = AgentRegistry()
    with pytest.raises(RuntimeError, match="not connected"):
        await _proxy_via_link(
            registry=reg, node_id=99, container_id="cid",
            method="GET", path="/", headers={}, body=b"",
        )
```

Note: this test returns chunks as a materialized list (via the helper) rather than streaming through a `StreamingResponse`. The wrapping `_proxy` route handler stays a thin layer over `_proxy_via_link` — covered by the existing openai-proxy integration tests already in `tests/integration/`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_openai_proxy_via_link.py -v`
Expected: FAIL — proxy still uses direct httpx.

- [ ] **Step 3: Add the dispatch helper**

In `src/serve_engine/daemon/openai_proxy.py`, add at module level:

```python
from serve_engine.cluster.agent_registry import AgentRegistry


async def _proxy_via_link(
    *, registry: AgentRegistry, node_id: int, container_id: str,
    method: str, path: str, headers: dict[str, str], body: bytes,
) -> tuple[int, dict[str, str], list[bytes]]:
    """Materialize the proxy response. Used directly by tests; the route
    handler streams instead — see `_proxy_stream_via_link` below."""
    link = registry.get(node_id)
    if link is None or not link.is_ready:
        raise RuntimeError(f"node {node_id} not connected")
    status_code: int | None = None
    out_headers: dict[str, str] = {}
    body_chunks: list[bytes] = []
    async for chunk in link.proxy_request(
        container_id=container_id, method=method, path=path,
        headers=headers, body=body,
    ):
        if chunk.status is not None and status_code is None:
            status_code = chunk.status
        if chunk.headers is not None and not out_headers:
            out_headers = dict(chunk.headers)
        if chunk.body:
            body_chunks.append(chunk.body)
        if chunk.eof:
            break
    assert status_code is not None, "agent returned no status chunk"
    out_headers.pop("content-length", None)
    return status_code, out_headers, body_chunks
```

- [ ] **Step 4: Refactor `_proxy` to use the helper for streaming**

Replace the existing engine-client construction in `_proxy` with:

```python
# After resolving dep_row (with node_id, container_id):
registry: AgentRegistry = request.app.state.agent_registry
link = registry.get(dep_row.node_id)
if link is None or not link.is_ready:
    raise HTTPException(
        status_code=503,
        detail=f"deployment {dep_row.id} is on node {dep_row.node_id} which is not connected",
    )

body = await request.body()
hdrs = {k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}}

agen = link.proxy_request(
    container_id=dep_row.container_id,
    method=request.method,
    path=request.url.path,
    headers=hdrs,
    body=body,
)
first = await agen.__anext__()
status_code = first.status or 200
out_headers = dict(first.headers or {})
out_headers.pop("content-length", None)

async def body_stream():
    if first.body:
        yield first.body
    if first.eof:
        return
    async for c in agen:
        if c.body:
            yield c.body
        if c.eof:
            return

return StreamingResponse(
    body_stream(), status_code=status_code, headers=out_headers,
    media_type=out_headers.get("content-type"),
)
```

Drop the previous direct-httpx engine-client path. Keep `make_engine_client` as a deprecated shim only if existing tests still import it; otherwise remove.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/integration -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/openai_proxy.py tests/integration/test_openai_proxy_via_link.py
git commit -m "feat(daemon): /v1/* dispatches through AgentLink (tunnel-ready)"
```

---

## Task 23: Heartbeat tracking & unreachable transitions

**Files:**
- Create: `src/serve_engine/cluster/health_watcher.py`
- Modify: `src/serve_engine/daemon/app.py` (start watcher task)
- Test: `tests/unit/test_health_watcher.py`

The watcher runs on the leader. Every 5 seconds it scans `nodes` and:
- If a node is `ready` and `last_seen < now - 15s`, transitions it to `unreachable` and unregisters its AgentLink.

`last_seen` is updated by the hub each time it processes a heartbeat. (Add that to `LeaderHub._handle_agent`: in the main `link.run()` loop, intercept `Heartbeat` frames before passing to `run()` — easiest path is to wrap the WS in a small adapter that updates `last_seen` on each `Heartbeat` it sees. For v1, simpler: extend `RemoteAgentLink.run()` to call back into a `on_heartbeat` callback.)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_health_watcher.py
from __future__ import annotations

import time

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.health_watcher import sweep
from serve_engine.store.db import open_db
from serve_engine.store import nodes as nodes_store


def test_sweep_marks_stale_node_unreachable(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    now = 1000.0
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="fp",
        reachable_as=None, first_seen=now, last_seen=now,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=now)
    reg = AgentRegistry()
    sweep(conn, reg, now=now + 30, stale_after_s=15)
    n = nodes_store.get(conn, nid)
    assert n.status == "unreachable"


def test_sweep_does_not_touch_fresh_node(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    now = 1000.0
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint="fp",
        reachable_as=None, first_seen=now, last_seen=now,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    nodes_store.set_status(conn, nid, status="ready", last_seen=now + 5)
    reg = AgentRegistry()
    sweep(conn, reg, now=now + 10, stale_after_s=15)
    n = nodes_store.get(conn, nid)
    assert n.status == "ready"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_health_watcher.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

Create `src/serve_engine/cluster/health_watcher.py`:

```python
from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.store import nodes as nodes_store


def sweep(
    conn: sqlite3.Connection,
    registry: AgentRegistry,
    *,
    now: float | None = None,
    stale_after_s: float = 15.0,
) -> None:
    t = now if now is not None else time.time()
    for n in nodes_store.list_all(conn):
        if n.label == "local":
            continue  # local is always reachable while the daemon runs
        if n.status == "ready" and (t - n.last_seen) > stale_after_s:
            nodes_store.set_status(conn, n.id, status="unreachable", last_seen=n.last_seen)
            registry.unregister(n.id)


async def run_health_watcher(
    conn: sqlite3.Connection,
    registry: AgentRegistry,
    *,
    interval_s: float = 5.0,
    stale_after_s: float = 15.0,
) -> None:
    while True:
        try:
            sweep(conn, registry, stale_after_s=stale_after_s)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(interval_s)
```

In `daemon/app.py`, start the watcher in the app lifespan:

```python
import asyncio
from serve_engine.cluster.health_watcher import run_health_watcher

# Inside startup event:
app.state.health_watcher_task = asyncio.create_task(
    run_health_watcher(conn, agent_registry)
)
# Inside shutdown event:
app.state.health_watcher_task.cancel()
```

Also: wire `LeaderHub` so that whenever it processes a `Heartbeat` frame, it calls `nodes_store.set_status(conn, node_id, status='ready', last_seen=now)`. Easiest: split the frame loop in `LeaderHub` so heartbeats are handled there instead of forwarded into `RemoteAgentLink.run()`. Code sketch:

```python
# In LeaderHub._handle_agent, replace `await link.run()` with:
try:
    async for raw in ws.iter_text():
        f = decode_frame(raw)
        if isinstance(f, Heartbeat):
            nodes_store.set_status(self._conn, node.id, status="ready", last_seen=time.time())
            continue
        # Forward to link's inbound machinery:
        await link._inbound(f)  # see Step 4 below
except WebSocketDisconnect:
    pass
```

- [ ] **Step 4: Adapt RemoteAgentLink to expose a frame-inbound entrypoint**

In `src/serve_engine/cluster/remote_agent.py`, split `run()`'s body so each handled frame goes through `_inbound(frame)`:

```python
async def _inbound(self, f: Frame) -> None:
    if isinstance(f, OpResult):
        fut = self._pending_ops.pop(f.request_id, None)
        if fut and not fut.done():
            fut.set_result(f)
    elif isinstance(f, HttpChunk):
        q = self._streams.get(f.stream_id)
        if q is not None:
            await q.put(f)
```

`run()` becomes thin (only used by the unit test):

```python
async def run(self) -> None:
    try:
        while not self._shutdown:
            raw = await self._ws.recv()
            if raw is None:
                break
            await self._inbound(decode_frame(raw))
    finally:
        self.shutdown()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/serve_engine/cluster/health_watcher.py src/serve_engine/cluster/leader_hub.py src/serve_engine/cluster/remote_agent.py src/serve_engine/daemon/app.py tests/unit/test_health_watcher.py
git commit -m "feat(cluster): heartbeat-based health watcher; unreachable transitions"
```

---

## Task 24: Router gating — refuse to send to unreachable nodes

**Files:**
- Modify: `src/serve_engine/daemon/openai_proxy.py`
- Modify: `src/serve_engine/lifecycle/adapter_router.py` (resolution layer — same gating)
- Test: `tests/integration/test_router_gating.py`

If the deployment's `node_id` resolves to no AgentLink in the registry, return 503 with a clear message. (This is already partially in Task 22's snippet; this task formalizes and tests it.)

- [ ] **Step 1: Write the failing test**

Unit-level test against the helper, plus a focused gating test against the route handler using FastAPI's TestClient.

```python
# tests/integration/test_router_gating.py
from __future__ import annotations

import pytest

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.daemon.openai_proxy import _proxy_via_link


@pytest.mark.asyncio
async def test_proxy_via_link_503_when_node_missing():
    reg = AgentRegistry()
    with pytest.raises(RuntimeError, match="not connected"):
        await _proxy_via_link(
            registry=reg, node_id=99, container_id="cid",
            method="GET", path="/v1/models", headers={}, body=b"",
        )


def test_adapter_router_skips_deployments_on_unreachable_nodes():
    """find_deployment_for should not return a row whose node_id has no
    live AgentLink — even if the row is `ready` in the DB."""
    from serve_engine.cluster.agent_registry import AgentRegistry
    from serve_engine.lifecycle.adapter_router import (
        _filter_by_reachable_nodes,  # added in Step 3 below
    )

    # Two candidate deployments — only node 1 is connected.
    candidates = [
        type("Dep", (), {"id": 10, "node_id": 1, "state": "ready"})(),
        type("Dep", (), {"id": 11, "node_id": 2, "state": "ready"})(),
    ]

    class _Link:
        @property
        def node_id(self): return 1
        @property
        def is_ready(self): return True
        async def start_deployment(self, p): ...
        async def stop_deployment(self, c, *, remove=True): ...
        async def proxy_request(self, **kw): ...
    reg = AgentRegistry()
    reg.register(_Link())

    filtered = _filter_by_reachable_nodes(candidates, reg)
    assert [d.id for d in filtered] == [10]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_router_gating.py -v`
Expected: FAIL — no gating.

- [ ] **Step 3: Implement**

The HTTP 503 gating in `_proxy` was added in Task 22. This task adds the same gating one layer earlier — at deployment selection — so the router prefers a different reachable deployment of the same model rather than failing.

In `src/serve_engine/lifecycle/adapter_router.py`, add a small helper:

```python
from serve_engine.cluster.agent_registry import AgentRegistry


def _filter_by_reachable_nodes(candidates, registry: AgentRegistry):
    """Drop candidate deployment rows whose node_id has no live AgentLink."""
    out = []
    for dep in candidates:
        link = registry.get(dep.node_id)
        if link is not None and link.is_ready:
            out.append(dep)
    return out
```

Then in `find_deployment_for` (the existing ranking entrypoint), thread an `AgentRegistry` argument through and call `_filter_by_reachable_nodes(candidates, registry)` before the existing ranking step. Update callers in `openai_proxy.py` to pass `request.app.state.agent_registry`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve_engine/daemon/openai_proxy.py src/serve_engine/lifecycle/adapter_router.py tests/integration/test_router_gating.py
git commit -m "feat(daemon): refuse routing to deployments on unreachable nodes"
```

---

## Task 25: End-to-end roundtrip integration test

**Files:**
- Create: `tests/integration/test_remote_agent_roundtrip.py`

This is the load-bearing test for the slice: spin up the leader's FastAPI app in-process, simulate a remote agent by opening a WS to `/cluster/agent` (with a stubbed fingerprint), have the agent answer a fake `StartDeployment` and a fake `HttpRequest`, and verify the leader can both place a deployment on the simulated agent and route a `/v1/chat/completions` request through to it.

- [ ] **Step 1: Write the test**

The substantive end-to-end behavior (handshake, start_deployment over WS, http_request tunneling) is already exercised by the unit tests in Tasks 13, 14, and 17 against the same `RemoteAgentLink` and `AgentFrameDispatcher` codepaths the production code uses. This integration test is a thin glue test: it connects a fake agent to the leader's hub via FastAPI's `TestClient.websocket_connect`, completes the handshake, then drives one `start_deployment` operation through the registry that the hub registered.

```python
# tests/integration/test_remote_agent_roundtrip.py
from __future__ import annotations

import asyncio
import pytest
from fastapi.testclient import TestClient

from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.cluster.leader_hub import LeaderHub
from serve_engine.cluster.protocol import (
    Hello, OpResult, StartDeployment, Welcome, decode_frame, encode_frame,
)
from serve_engine.store import nodes as nodes_store
from serve_engine.store.db import open_db
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_leader_to_remote_agent_roundtrip(tmp_path):
    conn = open_db(tmp_path / "db.sqlite")
    fp = "sha256:fake"
    nid = nodes_store.insert(
        conn, label="agent-a", fingerprint=fp,
        reachable_as=None, first_seen=0.0, last_seen=0.0,
        agent_version=None, cpu_count=0, total_ram_mb=0,
        gpu_count=0, total_vram_mb=0,
    )
    registry = AgentRegistry()
    hub = LeaderHub(conn=conn, registry=registry,
                    fingerprint_resolver=lambda ws: fp)
    app = FastAPI()
    app.include_router(hub.router)

    client = TestClient(app)
    with client.websocket_connect("/cluster/agent") as ws:
        ws.send_text(encode_frame(Hello(
            agent_version="x",
            host_info={"cpu_count": 0, "total_ram_mb": 0,
                       "gpu_count": 0, "total_vram_mb": 0, "gpus": []},
        )))
        welcome = decode_frame(ws.receive_text())
        assert isinstance(welcome, Welcome)
        assert welcome.node_id == nid

        # The hub should have registered a RemoteAgentLink for this node.
        link = registry.get(nid)
        assert link is not None and link.is_ready

        # Drive a start_deployment from the leader side and answer it.
        async def caller():
            return await link.start_deployment({"image": "x", "name": "d"})

        # In a background thread, run the caller; we'll answer it from the
        # foreground via the TestClient WS.
        caller_task = asyncio.create_task(caller())
        # Read what the leader sent to "us" (the fake agent):
        raw = ws.receive_text()
        start = decode_frame(raw)
        assert isinstance(start, StartDeployment)
        # Answer:
        ws.send_text(encode_frame(OpResult(
            request_id=start.request_id, ok=True,
            data={"container_id": "cid-rt", "address": "tunnel", "port": 0},
        )))
        started = await caller_task
        assert started.container_id == "cid-rt"
```

- [ ] **Step 2: Run it and iterate to green**

Run: `pytest tests/integration/test_remote_agent_roundtrip.py -v`
Expected: PASS once the scenario is fully filled in. This test exists primarily as a verification gate — if it goes red, the slice does not work.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_remote_agent_roundtrip.py
git commit -m "test(cluster): leader↔remote-agent E2E roundtrip"
```

---

## Task 26: README — minimal operator setup section

**Files:**
- Modify: `README.md`

Add a small section after "Operations Notes" titled "Multi-Node (Tunneled, Preview)" that describes:
- The two roles (`serve daemon start` = leader, `serve agent start` = agent on each capacity host)
- The enrollment flow (`serve nodes enroll <label>` on the leader → copy the printed command → run `serve agent register ...` on the new host → `serve agent start`)
- That data plane is tunneled today; direct LAN routing is coming later
- That the leader's CA lives in `~/.serve/ca/`

- [ ] **Step 1: Add the section**

Append to `README.md`, before "License":

````markdown
## Multi-Node (Tunneled, Preview)

A leader serves the OpenAI API and admin API. Additional GPU hosts run a
thin agent that dials home over mTLS WebSocket. Engines run on the agent's
host; `/v1/*` traffic is tunneled over the WS for now (direct-LAN routing
is the next iteration).

On the leader, mint a one-time enrollment token:

```bash
serve nodes enroll gpu-rig-2
```

It prints a `serve agent register ...` command. Copy it.

On the new GPU host:

```bash
serve agent register --leader https://leader.example:11500 --token <token>
serve agent start
```

Check it landed:

```bash
serve nodes ls
```

The leader's CA cert and key live under `~/.serve/ca/`. Revoke an agent with:

```bash
serve nodes remove <id>
```
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(readme): multi-node tunneled preview section"
```

---

## Follow-up Plans (NOT in this plan)

Track these as separate plans, each producing working slices on their own:

1. **Direct-LAN ingress + leader probing** — agent opens a guarded mTLS ingress port on its `reachable_as` address; leader probes and switches to direct routing when reachable. Engines stay on loopback; ingress proxies by deployment id.
2. **UI additions** — `Nodes` page, node chip on deployment cards, GPU view groups by node, route/profile editor `node_label` field.
3. **Service-profile `node_label` affinity** — schema column, surfaced through admin API + CLI + placement filter.
4. **Replica fan-out** — `replicas > 1` per profile across nodes, router round-robins.
5. **Reconnect reconciliation** — agent state-snapshot on reconnect, orphan-container kill, drift correction.
6. **uvicorn-direct mTLS termination** — make the leader speak TLS directly (today's design assumes the operator terminates TLS at a reverse proxy and forwards the cert fingerprint).
