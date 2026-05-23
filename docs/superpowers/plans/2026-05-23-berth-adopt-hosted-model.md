# Adopt Externally-Hosted Model — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator register an already-running OpenAI-compatible server on the agent host as a berth deployment (`berth agent adopt`), so the leader routes `/v1/*` to it and reserves its GPUs, without berth launching or stopping anything.

**Architecture:** Agent-authoritative. The agent persists adopted endpoints in `~/.berth/adopted.yaml`, registers them in its dispatcher, and reports the full set to the leader on every (re)connect via a new `ReportAdopted` frame. The leader reconciles `source='adopted'` deployment rows for that node to match the report. Routing reuses the existing `container_id → (address, port)` dispatcher hook; the reaper and `manager.stop` are taught to leave adopted rows' processes alone.

**Tech Stack:** Python 3.11+, sqlite (migrations), httpx, typer CLI, websockets, pytest. Spec: `docs/superpowers/specs/2026-05-23-berth-adopt-hosted-model-design.md`.

---

## File Structure

- Create `src/berth/store/migrations/016_adopted.sql` — adds `source` column.
- Create `src/berth/cluster/adopted.py` — adopted-endpoint dataclass, `adopted.yaml` load/save, endpoint probe, docker introspection, report-dict mapping. (Pure-ish, unit-testable.)
- Modify `src/berth/store/deployments.py` — `source` field + `upsert_adopted` + `list_adopted_for_node`.
- Modify `src/berth/cluster/protocol.py` — `ReportAdopted` frame.
- Modify `src/berth/lifecycle/reaper.py` — skip adopted.
- Modify `src/berth/lifecycle/manager.py` — `_stop_locked` adopted branch.
- Modify `src/berth/cli/agent_cmd.py` — `adopt`, `unadopt`, `adopted ls`.
- Modify `src/berth/cluster/leader_hub.py` — `ReportAdopted` reconciliation.
- Modify `src/berth/cluster/agent_client.py` — load/register/report + watch.
- Tests under `tests/unit/` and `tests/integration/`.

---

## Task 1: DB migration + `source` column + store helpers

**Files:**
- Create: `src/berth/store/migrations/016_adopted.sql`
- Modify: `src/berth/store/deployments.py`
- Test: `tests/unit/test_adopted_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_adopted_store.py
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

    # Re-report same container_id on same node → update, not duplicate row.
    dep2 = dep_store.upsert_adopted(
        conn, model_id=m.id, node_id=3, container_id="cid-1",
        address="127.0.0.1", port=30011, gpu_ids=[7],
        vram_reserved_mb=268000, image_tag="lmsysorg/sglang:latest",
    )
    assert dep2.id == dep.id
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_store.py -v`
Expected: FAIL — `Deployment` has no attribute `source` / `dep_store` has no `upsert_adopted`.

- [ ] **Step 3a: Create the migration**

```sql
-- src/berth/store/migrations/016_adopted.sql
-- Adopted deployments wrap an externally-hosted OpenAI-compatible endpoint
-- that berth routes to but never launches/stops. 'managed' = berth owns the
-- container's lifecycle (the default for every existing row).
ALTER TABLE deployments ADD COLUMN source TEXT NOT NULL DEFAULT 'managed';
```

- [ ] **Step 3b: Add `source` to the dataclass + row mapper**

In `src/berth/store/deployments.py`, add to the `Deployment` dataclass (after `node_id`):

```python
    source: str = "managed"  # 'managed' | 'adopted' (migration 016)
```

In `_row_to_dep`, before the `return Deployment(`:

```python
    source_value = row_get(row, "source", "managed")
```

and add to the `Deployment(...)` constructor call:

```python
        source=source_value or "managed",
```

- [ ] **Step 3c: Add the store helpers**

Append to `src/berth/store/deployments.py`:

```python
def upsert_adopted(
    conn: sqlite3.Connection,
    *,
    model_id: int,
    node_id: int,
    container_id: str,
    address: str,
    port: int,
    gpu_ids: list[int],
    vram_reserved_mb: int,
    image_tag: str,
    status: Status = "ready",
) -> Deployment:
    """Create or update the adopted deployment for (node_id, container_id).

    Keyed on (node_id, container_id) so a repeated full-state report updates
    the existing row instead of duplicating it."""
    gpu_csv = ",".join(str(g) for g in gpu_ids)
    existing = conn.execute(
        "SELECT id FROM deployments "
        "WHERE source='adopted' AND node_id=? AND container_id=?",
        (node_id, container_id),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE deployments
            SET model_id=?, backend='adopted', image_tag=?, gpu_ids=?,
                tensor_parallel=?, max_model_len=NULL, dtype='auto',
                container_name=?, container_port=?, container_address=?,
                vram_reserved_mb=?, status=?, last_error=NULL
            WHERE id=?
            """,
            (model_id, image_tag, gpu_csv, max(1, len(gpu_ids)),
             container_id, port, address, vram_reserved_mb, status,
             existing["id"]),
        )
        dep_id = existing["id"]
    else:
        cur = conn.execute(
            """
            INSERT INTO deployments
                (model_id, backend, image_tag, gpu_ids, tensor_parallel,
                 max_model_len, dtype, pinned, idle_timeout_s,
                 vram_reserved_mb, node_id, source,
                 container_id, container_name, container_port,
                 container_address, status)
            VALUES (?, 'adopted', ?, ?, ?, NULL, 'auto', 0, NULL, ?, ?,
                    'adopted', ?, ?, ?, ?, ?)
            """,
            (model_id, image_tag, gpu_csv, max(1, len(gpu_ids)),
             vram_reserved_mb, node_id, container_id, container_id,
             port, address, status),
        )
        if cur.lastrowid is None:
            raise RuntimeError("adopted deployment insert returned no id")
        dep_id = cur.lastrowid
    result = get_by_id(conn, dep_id)
    if result is None:
        raise RuntimeError(f"adopted upsert lost row id={dep_id}")
    return result


def list_adopted_for_node(
    conn: sqlite3.Connection, node_id: int
) -> list[Deployment]:
    rows = conn.execute(
        "SELECT * FROM deployments WHERE source='adopted' AND node_id=? "
        "ORDER BY id",
        (node_id,),
    ).fetchall()
    return [_row_to_dep(r) for r in rows]


def delete_adopted(conn: sqlite3.Connection, dep_id: int) -> None:
    conn.execute(
        "DELETE FROM deployments WHERE id=? AND source='adopted'", (dep_id,)
    )
```

> Note: plain `conn.execute` matches the existing `create()`/`set_container()` style (reconciliation is serialized on the leader's frame loop, so no extra locking is needed). `create()` keeps its existing signature; `source` defaults to `'managed'` at the DB layer, so managed inserts are unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_store.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/store/migrations/016_adopted.sql src/berth/store/deployments.py tests/unit/test_adopted_store.py
git commit -m "feat(store): adopted deployment source column + upsert/list helpers"
```

---

## Task 2: `ReportAdopted` protocol frame

**Files:**
- Modify: `src/berth/cluster/protocol.py`
- Test: `tests/unit/test_protocol_report_adopted.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_protocol_report_adopted.py
from berth.cluster.protocol import ReportAdopted, decode_frame, encode_frame


def test_report_adopted_round_trips():
    frame = ReportAdopted(endpoints=[{
        "model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "served_model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "address": "127.0.0.1", "port": 30011,
        "container_id": "cid-1", "gpu_ids": [7],
        "vram_reserved_mb": 268000, "alive": True,
    }])
    decoded = decode_frame(encode_frame(frame))
    assert isinstance(decoded, ReportAdopted)
    assert decoded.endpoints[0]["port"] == 30011
    assert decoded.type == "report_adopted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_protocol_report_adopted.py -v`
Expected: FAIL — `ImportError: cannot import name 'ReportAdopted'`.

- [ ] **Step 3: Add the frame**

In `src/berth/cluster/protocol.py`, add the dataclass after `LogCancel`:

```python
@dataclass
class ReportAdopted:
    """Agent → leader: the FULL current set of adopted endpoints on this node.

    Full-state (not incremental): the leader makes its source='adopted' rows
    for the node equal this list — entries absent here are removed and their
    GPUs freed. Sent after Hello on connect and whenever the local set or any
    `alive` flag changes."""
    endpoints: list[dict[str, Any]]
    type: str = "report_adopted"
```

Add `ReportAdopted` to the `Frame` union and add `"report_adopted": ReportAdopted,` to `_REGISTRY`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_protocol_report_adopted.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/cluster/protocol.py tests/unit/test_protocol_report_adopted.py
git commit -m "feat(protocol): ReportAdopted agent->leader frame"
```

---

## Task 3: Reaper skips adopted deployments

**Files:**
- Modify: `src/berth/lifecycle/reaper.py`
- Test: `tests/unit/test_reaper_skips_adopted.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reaper_skips_adopted.py
import asyncio
from dataclasses import dataclass

from berth.lifecycle.reaper import Reaper


@dataclass
class _Dep:
    id: int
    pinned: bool = False
    status: str = "ready"
    last_request_at: float = 0.0   # epoch 0 => very idle
    idle_timeout_s: int | None = 1
    source: str = "managed"


class _Manager:
    def __init__(self):
        self.stopped = []
    async def stop(self, dep_id):
        self.stopped.append(dep_id)


def test_reaper_evicts_managed_but_not_adopted():
    mgr = _Manager()
    deps = [_Dep(id=1, source="managed"), _Dep(id=2, source="adopted")]
    reaper = Reaper(manager=mgr, list_ready=lambda: deps,
                    now_fn=lambda: 10_000.0)
    asyncio.run(reaper.tick_once())
    assert mgr.stopped == [1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_reaper_skips_adopted.py -v`
Expected: FAIL — both `1` and `2` evicted (`assert [1, 2] == [1]`).

- [ ] **Step 3: Add the skip**

In `src/berth/lifecycle/reaper.py`, inside `tick_once`, immediately after the `for d in self._list_ready():` line and before the `if d.pinned:` check:

```python
            if getattr(d, "source", "managed") == "adopted":
                continue  # berth doesn't own adopted endpoints' lifecycle
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_reaper_skips_adopted.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/lifecycle/reaper.py tests/unit/test_reaper_skips_adopted.py
git commit -m "feat(reaper): never idle-evict adopted deployments"
```

---

## Task 4: `manager.stop` leaves adopted processes alone

**Files:**
- Modify: `src/berth/lifecycle/manager.py` (`_stop_locked`)
- Test: `tests/unit/test_manager_stop_adopted.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_manager_stop_adopted.py
import asyncio

from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store
from berth.lifecycle.manager import LifecycleManager


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
    mgr = LifecycleManager(conn=conn, docker=docker, configs_dir=tmp_path)
    asyncio.run(mgr.stop(dep.id))
    assert docker.stopped == []                      # process untouched
    assert dep_store.get_by_id(conn, dep.id).status == "stopped"
```

> If `LifecycleManager.__init__` requires more args in this codebase, construct it the way the existing `tests/unit/test_*manager*` or `tests/integration` tests do (match their fixture); keep the two assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_manager_stop_adopted.py -v`
Expected: FAIL — `docker.stop("ext-cid")` was called (`assert ['ext-cid'] == []`).

- [ ] **Step 3: Add the adopted branch**

In `src/berth/lifecycle/manager.py`, at the top of `_stop_locked`, right after the `dep_store.update_status(self._conn, dep.id, "stopping")` line, insert:

```python
        if dep.source == "adopted":
            # berth never started this process; just drop the route + row.
            da_store.detach_all(self._conn, dep.id)
            dep_store.update_status(self._conn, dep.id, "stopped")
            await self._emit("deployment.stopped", dep_id=dep_id)
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_manager_stop_adopted.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/lifecycle/manager.py tests/unit/test_manager_stop_adopted.py
git commit -m "feat(manager): stop() deregisters adopted route without docker stop"
```

---

## Task 5: adopted-endpoint module (yaml store + probe + introspection)

**Files:**
- Create: `src/berth/cluster/adopted.py`
- Test: `tests/unit/test_adopted_module.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_adopted_module.py
import httpx
import pytest

from berth.cluster import adopted


def test_save_load_round_trip(tmp_path):
    e = adopted.AdoptedEndpoint(
        name="minimax", model_name="nvidia/MiniMax-M2.7-NVFP4",
        served_model_name="nvidia/MiniMax-M2.7-NVFP4",
        address="127.0.0.1", port=30011, container_id="cid-1",
        gpu_ids=[7], vram_reserved_mb=268000, image_tag="external",
    )
    adopted.save(tmp_path, [e])
    loaded = adopted.load(tmp_path)
    assert loaded == [e]


def test_add_entry_rejects_name_collision(tmp_path):
    e = adopted.AdoptedEndpoint(
        name="minimax", model_name="m", served_model_name="m",
        address="127.0.0.1", port=30011, container_id="c",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    adopted.save(tmp_path, [e])
    with pytest.raises(adopted.AdoptError, match="name"):
        adopted.add_entry(tmp_path, e)


def test_add_entry_rejects_gpu_overlap(tmp_path):
    a = adopted.AdoptedEndpoint(
        name="a", model_name="a", served_model_name="a",
        address="127.0.0.1", port=1, container_id="ca",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    b = adopted.AdoptedEndpoint(
        name="b", model_name="b", served_model_name="b",
        address="127.0.0.1", port=2, container_id="cb",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    adopted.save(tmp_path, [a])
    with pytest.raises(adopted.AdoptError, match="GPU"):
        adopted.add_entry(tmp_path, b)


def test_probe_returns_served_model(monkeypatch):
    def fake_get(url, timeout):
        assert url.endswith("/v1/models")
        return httpx.Response(200, json={"data": [{"id": "served-x"}]})
    monkeypatch.setattr(adopted.httpx, "get", fake_get)
    assert adopted.probe_served_model("127.0.0.1", 30011) == "served-x"


def test_probe_raises_when_unreachable(monkeypatch):
    def boom(url, timeout):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(adopted.httpx, "get", boom)
    with pytest.raises(adopted.AdoptError, match="not reachable"):
        adopted.probe_served_model("127.0.0.1", 30011)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_module.py -v`
Expected: FAIL — `ModuleNotFoundError: berth.cluster.adopted`.

- [ ] **Step 3: Write the module**

```python
# src/berth/cluster/adopted.py
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml


class AdoptError(Exception):
    """Raised for invalid adopt requests (collision, unreachable endpoint)."""


@dataclass
class AdoptedEndpoint:
    name: str
    model_name: str
    served_model_name: str
    address: str
    port: int
    container_id: str
    gpu_ids: list[int] = field(default_factory=list)
    vram_reserved_mb: int = 0
    image_tag: str = "external"

    def to_report_dict(self, *, alive: bool) -> dict[str, Any]:
        d = asdict(self)
        d.pop("name")  # 'name' is a local display label, not reported
        d["alive"] = alive
        return d


def _path(home: Path) -> Path:
    return home / "adopted.yaml"


def load(home: Path) -> list[AdoptedEndpoint]:
    p = _path(home)
    if not p.exists():
        return []
    raw = yaml.safe_load(p.read_text()) or []
    return [AdoptedEndpoint(**e) for e in raw]


def save(home: Path, entries: list[AdoptedEndpoint]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    _path(home).write_text(
        yaml.safe_dump([asdict(e) for e in entries], sort_keys=False)
    )


def add_entry(home: Path, entry: AdoptedEndpoint) -> list[AdoptedEndpoint]:
    entries = load(home)
    if any(e.name == entry.name for e in entries):
        raise AdoptError(f"adopted name {entry.name!r} already exists")
    used = {g for e in entries for g in e.gpu_ids}
    clash = sorted(used.intersection(entry.gpu_ids))
    if clash:
        raise AdoptError(f"GPU(s) {clash} already used by another adopted endpoint")
    entries.append(entry)
    save(home, entries)
    return entries


def remove_entry(home: Path, name: str) -> list[AdoptedEndpoint]:
    entries = [e for e in load(home) if e.name != name]
    save(home, entries)
    return entries


def probe_served_model(address: str, port: int, *, timeout: float = 5.0) -> str:
    """GET /v1/models; return the first model id. Raises AdoptError if the
    endpoint is unreachable or returns no model."""
    url = f"http://{address}:{port}/v1/models"
    try:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            raise AdoptError(f"{url} returned no models")
        return str(data[0]["id"])
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise AdoptError(f"endpoint {address}:{port} not reachable: {e}") from e


def introspect_container(dc, name: str) -> tuple[str, str, int, list[int], str]:
    """Resolve a running docker container by name to
    (container_id, host_address, host_port, gpu_ids, image_tag).

    `dc` is a berth DockerClient exposing `._client` (docker SDK)."""
    c = dc._client.containers.get(name)  # raises docker.errors.NotFound
    attrs = c.attrs
    ports = (attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
    host_addr, host_port = "127.0.0.1", None
    for _internal, bindings in ports.items():
        if bindings:
            host_port = int(bindings[0]["HostPort"])
            addr = bindings[0].get("HostIp") or "127.0.0.1"
            host_addr = "127.0.0.1" if addr in ("0.0.0.0", "") else addr
            break
    if host_port is None:
        raise AdoptError(f"container {name!r} has no published host port")
    host_cfg = attrs.get("HostConfig", {}) or {}
    gpu_ids: list[int] = []
    for req in host_cfg.get("DeviceRequests") or []:
        for dev in req.get("DeviceIDs") or []:
            if str(dev).isdigit():
                gpu_ids.append(int(dev))
    image_tag = (attrs.get("Config", {}) or {}).get("Image", "external")
    return c.id, host_addr, host_port, gpu_ids, image_tag
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_module.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/cluster/adopted.py tests/unit/test_adopted_module.py
git commit -m "feat(adopted): yaml store, /v1/models probe, container introspection"
```

---

## Task 6: CLI commands `adopt` / `unadopt` / `adopted ls`

**Files:**
- Modify: `src/berth/cli/agent_cmd.py`
- Test: `tests/unit/test_cli_adopt.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cli_adopt.py
from typer.testing import CliRunner

from berth.cli import app
from berth.cluster import adopted

runner = CliRunner()


def test_adopt_by_port_writes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    monkeypatch.setattr(adopted, "probe_served_model",
                        lambda a, p, **k: "nvidia/MiniMax-M2.7-NVFP4")
    result = runner.invoke(app, [
        "agent", "adopt",
        "--port", "30011", "--model", "nvidia/MiniMax-M2.7-NVFP4",
        "--name", "minimax", "--gpus", "7", "--vram-mb", "268000",
    ])
    assert result.exit_code == 0, result.output
    entries = adopted.load(tmp_path)
    assert len(entries) == 1
    assert entries[0].port == 30011
    assert entries[0].gpu_ids == [7]
    assert entries[0].container_id == "adopted-nvidia/MiniMax-M2.7-NVFP4-30011"


def test_adopt_aborts_when_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    def boom(a, p, **k):
        raise adopted.AdoptError("not reachable")
    monkeypatch.setattr(adopted, "probe_served_model", boom)
    result = runner.invoke(app, [
        "agent", "adopt", "--port", "30011", "--model", "m",
    ])
    assert result.exit_code != 0
    assert adopted.load(tmp_path) == []


def test_unadopt_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    e = adopted.AdoptedEndpoint(
        name="minimax", model_name="m", served_model_name="m",
        address="127.0.0.1", port=30011, container_id="c",
        gpu_ids=[7], vram_reserved_mb=1, image_tag="external")
    adopted.save(tmp_path, [e])
    result = runner.invoke(app, ["agent", "unadopt", "minimax"])
    assert result.exit_code == 0, result.output
    assert adopted.load(tmp_path) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_cli_adopt.py -v`
Expected: FAIL — `No such command 'adopt'`.

- [ ] **Step 3: Add the commands**

In `src/berth/cli/agent_cmd.py`, add this import near the top:

```python
from berth.cluster import adopted as adopted_mod
```

Add these commands to the `agent_app` Typer group (alongside `register`/`start`):

```python
@agent_app.command("adopt")
def adopt(
    container: str = typer.Option(None, "--container",
        help="Adopt a running docker container by name (introspect port+GPUs)."),
    port: int = typer.Option(None, "--port",
        help="Adopt a raw OpenAI-compatible server on this host:port."),
    model: str = typer.Option(None, "--model",
        help="Model name (required with --port; used as the registry name)."),
    name: str = typer.Option(None, "--name", help="Local label (default: model)."),
    host: str = typer.Option("127.0.0.1", "--host"),
    gpus: str = typer.Option("", "--gpus", help="Comma-separated GPU ids, e.g. '7'."),
    served_model_name: str = typer.Option(None, "--served-model-name"),
    vram_mb: int = typer.Option(0, "--vram-mb",
        help="VRAM to reserve for these GPUs (0 = leave scheduler to treat as full)."),
):
    """Register an already-running OpenAI-compatible server as a deployment."""
    home = _berth_home()
    gpu_ids = [int(g) for g in gpus.split(",") if g.strip()]
    if container:
        from berth.lifecycle.docker_client import DockerClient
        try:
            cid, addr, prt, c_gpus, image_tag = adopted_mod.introspect_container(
                DockerClient(), container)
        except Exception as e:
            typer.echo(f"adopt failed: {e}", err=True)
            raise typer.Exit(1) from e
        gpu_ids = gpu_ids or c_gpus
        addr_eff, port_eff = addr, prt
    elif port:
        if not model:
            typer.echo("--model is required with --port", err=True)
            raise typer.Exit(1)
        cid = f"adopted-{model}-{port}"
        addr_eff, port_eff, image_tag = host, port, "external"
    else:
        typer.echo("provide --container OR --port/--model", err=True)
        raise typer.Exit(1)

    try:
        served = served_model_name or adopted_mod.probe_served_model(addr_eff, port_eff)
    except adopted_mod.AdoptError as e:
        typer.echo(f"adopt failed: {e}", err=True)
        raise typer.Exit(1) from e

    model_name = model or served
    entry = adopted_mod.AdoptedEndpoint(
        name=name or model_name, model_name=model_name,
        served_model_name=served, address=addr_eff, port=port_eff,
        container_id=cid, gpu_ids=gpu_ids, vram_reserved_mb=vram_mb,
        image_tag=image_tag,
    )
    try:
        adopted_mod.add_entry(home, entry)
    except adopted_mod.AdoptError as e:
        typer.echo(f"adopt failed: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"adopted {entry.name} -> {addr_eff}:{port_eff} "
        f"(model {served}, gpu {gpu_ids}). Takes effect on the running agent."
    )


@agent_app.command("unadopt")
def unadopt(name: str = typer.Argument(...)):
    """Remove an adopted endpoint; the route drops on the next agent report."""
    home = _berth_home()
    before = {e.name for e in adopted_mod.load(home)}
    if name not in before:
        typer.echo(f"no adopted endpoint named {name!r}", err=True)
        raise typer.Exit(1)
    adopted_mod.remove_entry(home, name)
    typer.echo(f"unadopted {name}")


@agent_app.command("adopted")
def adopted_ls():
    """List adopted endpoints recorded on this host."""
    for e in adopted_mod.load(_berth_home()):
        typer.echo(f"{e.name}\t{e.address}:{e.port}\t{e.served_model_name}\tgpu={e.gpu_ids}")
```

> The `adopted` command name renders as `berth agent adopted`. The function is named `adopted_ls` to avoid shadowing the imported module; Typer uses the decorator string for the CLI name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_cli_adopt.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/cli/agent_cmd.py tests/unit/test_cli_adopt.py
git commit -m "feat(cli): berth agent adopt/unadopt/adopted commands"
```

---

## Task 7: Leader-side reconciliation of `ReportAdopted`

**Files:**
- Modify: `src/berth/cluster/leader_hub.py`
- Test: `tests/unit/test_leader_reconcile_adopted.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_leader_reconcile_adopted.py
from berth.cluster.leader_hub import reconcile_adopted
from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _ep(**over):
    base = dict(model_name="nvidia/MiniMax-M2.7-NVFP4",
               served_model_name="nvidia/MiniMax-M2.7-NVFP4",
               address="127.0.0.1", port=30011, container_id="cid-1",
               gpu_ids=[7], vram_reserved_mb=268000, alive=True)
    base.update(over)
    return base


def test_reconcile_creates_then_prunes(tmp_path):
    conn = _conn(tmp_path)
    reconcile_adopted(conn, node_id=3, endpoints=[_ep()])
    rows = dep_store.list_adopted_for_node(conn, 3)
    assert len(rows) == 1 and rows[0].status == "ready"
    assert model_store.get_by_name(conn, "nvidia/MiniMax-M2.7-NVFP4") is not None

    # Empty report → row pruned (unadopted).
    reconcile_adopted(conn, node_id=3, endpoints=[])
    assert dep_store.list_adopted_for_node(conn, 3) == []


def test_reconcile_marks_down_when_not_alive(tmp_path):
    conn = _conn(tmp_path)
    reconcile_adopted(conn, node_id=3, endpoints=[_ep(alive=False)])
    rows = dep_store.list_adopted_for_node(conn, 3)
    assert rows[0].status == "failed"


def test_reconcile_skips_gpu_conflict_with_managed(tmp_path):
    conn = _conn(tmp_path)
    m = model_store.add(conn, name="managed-x", hf_repo="org/x")
    managed = dep_store.create(
        conn, model_id=m.id, backend="vllm", image_tag="img:v1",
        gpu_ids=[7], tensor_parallel=1, max_model_len=4096, dtype="auto")
    dep_store.update_status(conn, managed.id, "ready")
    reconcile_adopted(conn, node_id=3, endpoints=[_ep()])  # also wants gpu 7
    # Conflict: no adopted row created for the conflicting endpoint.
    assert dep_store.list_adopted_for_node(conn, 3) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_leader_reconcile_adopted.py -v`
Expected: FAIL — `ImportError: cannot import name 'reconcile_adopted'`.

- [ ] **Step 3a: Add the reconciliation function**

Add to `src/berth/cluster/leader_hub.py` (module-level, so it's unit-testable without a WebSocket):

```python
def reconcile_adopted(conn, *, node_id: int, endpoints: list[dict]) -> None:
    """Make this node's source='adopted' rows equal `endpoints` (full state).

    Upserts present entries (creating the model row if needed), marks them
    ready/failed by `alive`, and deletes rows whose container_id is absent
    from the report. An endpoint whose GPUs collide with a *managed* ready
    deployment is skipped (logged); its row, if any, is removed."""
    from berth.store import deployments as dep_store
    from berth.store import models as model_store

    managed_gpus: set[int] = set()
    for d in dep_store.list_all(conn):
        if d.source == "managed" and d.status in ("pending", "loading", "ready"):
            managed_gpus.update(d.gpu_ids)

    keep_cids: set[str] = set()
    for ep in endpoints:
        if managed_gpus.intersection(ep.get("gpu_ids") or []):
            log.warning(
                "adopted endpoint %s on node %s conflicts with managed GPUs %s; skipping",
                ep.get("container_id"), node_id,
                sorted(managed_gpus.intersection(ep["gpu_ids"])),
            )
            continue
        model = model_store.get_by_name(conn, ep["model_name"])
        if model is None:
            model = model_store.add(
                conn, name=ep["model_name"], hf_repo=ep["model_name"])
        dep_store.upsert_adopted(
            conn, model_id=model.id, node_id=node_id,
            container_id=ep["container_id"], address=ep["address"],
            port=int(ep["port"]), gpu_ids=list(ep.get("gpu_ids") or []),
            vram_reserved_mb=int(ep.get("vram_reserved_mb") or 0),
            image_tag=str(ep.get("image_tag") or "external"),
            status="ready" if ep.get("alive") else "failed",
        )
        keep_cids.add(ep["container_id"])

    for d in dep_store.list_adopted_for_node(conn, node_id):
        if d.container_id not in keep_cids:
            dep_store.delete_adopted(conn, d.id)
```

> `upsert_adopted` accepts `image_tag` and a `status`; both are already in its Task 1 signature. If `model_store.add` raises `AlreadyExists` under a race, wrap the create in try/except and re-`get_by_name`.

- [ ] **Step 3b: Dispatch the frame in the agent loop**

In `leader_hub.py`, add `ReportAdopted` to the `from berth.cluster.protocol import (...)` block. In `_handle_agent`'s receive loop, after the `if isinstance(frame, Heartbeat):` block and before `await link.inbound(frame)`:

```python
                if isinstance(frame, ReportAdopted):
                    try:
                        reconcile_adopted(
                            self._conn, node_id=node.id, endpoints=frame.endpoints)
                    except Exception:
                        log.exception("adopted reconcile failed for node %s", node.id)
                    continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_leader_reconcile_adopted.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/cluster/leader_hub.py tests/unit/test_leader_reconcile_adopted.py
git commit -m "feat(leader): reconcile adopted endpoints from ReportAdopted"
```

---

## Task 8: Agent runtime — load, register, report, and watch

**Files:**
- Modify: `src/berth/cluster/agent_client.py`
- Test: `tests/unit/test_agent_reports_adopted.py`

- [ ] **Step 1: Write the failing test** (test the pure helper, not the live socket)

```python
# tests/unit/test_agent_reports_adopted.py
from berth.cluster import adopted
from berth.cluster.agent_client import build_adopted_report, register_adopted_endpoints


class _Disp:
    def __init__(self):
        self.registered = {}
    def register_endpoint(self, *, container_id, address, port):
        self.registered[container_id] = (address, port)


def _entry():
    return adopted.AdoptedEndpoint(
        name="minimax", model_name="m", served_model_name="served-m",
        address="127.0.0.1", port=30011, container_id="cid-1",
        gpu_ids=[7], vram_reserved_mb=268000, image_tag="external")


def test_build_adopted_report_shapes_frame():
    frame = build_adopted_report([_entry()], alive_by_cid={"cid-1": True})
    assert frame.type == "report_adopted"
    ep = frame.endpoints[0]
    assert ep["served_model_name"] == "served-m"
    assert ep["alive"] is True
    assert "name" not in ep


def test_register_adopted_endpoints_registers_each():
    disp = _Disp()
    register_adopted_endpoints(disp, [_entry()])
    assert disp.registered == {"cid-1": ("127.0.0.1", 30011)}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_agent_reports_adopted.py -v`
Expected: FAIL — `cannot import name 'build_adopted_report'`.

- [ ] **Step 3a: Add the helpers**

Add to `src/berth/cluster/agent_client.py` (module level), and `from berth.cluster.protocol import ReportAdopted` plus `from berth.cluster import adopted as adopted_mod`:

```python
def build_adopted_report(
    entries: list, alive_by_cid: dict[str, bool]
) -> ReportAdopted:
    return ReportAdopted(endpoints=[
        e.to_report_dict(alive=alive_by_cid.get(e.container_id, True))
        for e in entries
    ])


def register_adopted_endpoints(disp, entries: list) -> None:
    for e in entries:
        disp.register_endpoint(
            container_id=e.container_id, address=e.address, port=e.port)
```

- [ ] **Step 3b: Wire it into the connect sequence**

In `run_agent`, immediately after the `_emit_status(status_cb, "agent.connected", ...)` call (right after the `Welcome` handshake, ~line 643) and before the heartbeat task is created:

```python
                _adopted = adopted_mod.load(home)
                if _adopted:
                    register_adopted_endpoints(disp, _adopted)
                    await sender.send(encode_frame(
                        build_adopted_report(_adopted, alive_by_cid={})))
```

> `home` is the agent home `Path` already in scope in `run_agent` (it is the function's first argument). `disp` and `sender` are the `AgentFrameDispatcher` and `SerializedSender` created just above in the same block.

- [ ] **Step 3c: Watch `adopted.yaml` for live changes** (re-report on edit)

Add a watch coroutine using `watchfiles` (already a dependency) started alongside the heartbeat task, and cancelled in the same `finally`:

```python
                async def watch_adopted(sender=sender, disp=disp):
                    from watchfiles import awatch
                    async for _ in awatch(str(home), stop_event=None):
                        entries = adopted_mod.load(home)
                        register_adopted_endpoints(disp, entries)
                        await sender.send(encode_frame(
                            build_adopted_report(entries, alive_by_cid={})))
                wa = asyncio.create_task(watch_adopted())
```

In the existing `finally:` that cancels `hb`, also cancel `wa`:

```python
                    wa.cancel()
                    with suppress(asyncio.CancelledError):
                        await wa
```

> Health-probe loop (periodic `/v1/models` per adopted endpoint, feeding `alive_by_cid`) is a follow-up refinement; the initial report and watch-driven re-report deliver the core feature. Liveness then also corrects via the leader's existing HealthMonitor on the deployment row.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_agent_reports_adopted.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/cluster/agent_client.py tests/unit/test_agent_reports_adopted.py
git commit -m "feat(agent): load/register/report adopted endpoints + watch adopted.yaml"
```

---

## Task 9: Integration smoke — adopt → report → proxy

**Files:**
- Create: `tests/integration/test_adopt_end_to_end.py`

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_adopt_end_to_end.py
"""End-to-end: a stub OpenAI server is adopted on a node; reconcile creates a
ready adopted row; the leader can resolve the model to that node+endpoint."""
from __future__ import annotations

from berth.cluster.leader_hub import reconcile_adopted
from berth.store import db
from berth.store import deployments as dep_store


def test_reconcile_makes_model_routable(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    reconcile_adopted(conn, node_id=3, endpoints=[{
        "model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "served_model_name": "nvidia/MiniMax-M2.7-NVFP4",
        "address": "127.0.0.1", "port": 30011, "container_id": "cid-1",
        "gpu_ids": [7], "vram_reserved_mb": 268000, "alive": True,
    }])
    dep = dep_store.find_ready_by_model_name(conn, "nvidia/MiniMax-M2.7-NVFP4")
    assert dep is not None
    assert dep.node_id == 3
    assert dep.container_id == "cid-1"
    assert (dep.container_address, dep.container_port) == ("127.0.0.1", 30011)
```

> This asserts the routing precondition the leader's `_proxy_via_link(node_id, container_id)` relies on. A fuller HTTP round-trip (stub uvicorn server + `link.proxy_request`) can be added following `tests/integration` patterns, but is heavier than this plan requires.

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/integration/test_adopt_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 3: Run the full unit suite for regressions**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit -q`
Expected: PASS (no regressions in reaper/manager/protocol/deployments tests).

- [ ] **Step 4: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add tests/integration/test_adopt_end_to_end.py
git commit -m "test(adopt): integration smoke for adopt->reconcile->routable"
```

---

## Task 10: Docs

**Files:**
- Modify: `docs/multi-node.md` (or nearest operator doc)

- [ ] **Step 1: Document the workflow**

Add an "Adopting an externally-hosted model" section: run your OpenAI-compatible server on the agent host, then `berth agent adopt --container <name>` (or `--port P --model M`); the model appears at the leader gateway under its served model name; `berth agent unadopt <name>` removes the route (your process keeps running). Note that berth never starts/stops adopted servers and reserves their declared GPUs.

- [ ] **Step 2: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add docs/multi-node.md
git commit -m "docs: adopting an externally-hosted model"
```

---

## Self-Review notes

- **Spec coverage:** adopt-by-container & by-port (T6) · GPU reservation (T1 row carries `gpu_ids`+`vram_reserved_mb`; placement reads these) · no eviction (T3) · no stop of process (T4) · self-heal on reconnect (T8 reports full set after Welcome) · full-state reconcile + prune (T7) · routing name = served_model_name (T6/T7 use `model_name`/`served`; integration T9 asserts routability) · health-down (leader HealthMonitor on the row; agent health-probe loop noted as follow-up) · unadopt (T6 + T7 prune).
- **Naming consistency:** `source` ('managed'|'adopted'), `upsert_adopted`, `list_adopted_for_node`, `delete_adopted`, `ReportAdopted`/`report_adopted`, `reconcile_adopted`, `build_adopted_report`, `register_adopted_endpoints`, `AdoptedEndpoint`/`AdoptError` are used identically across tasks.
- **Deferred (explicitly out of this plan, not silently dropped):** per-endpoint periodic health-probe loop on the agent (T8 note); full HTTP proxy round-trip integration test (T9 note). Both are refinements, not core-feature gaps.
