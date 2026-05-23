# berth Hardening + Responses API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /v1/responses` routing, an adopted-endpoint health-check, agent systemd persistence, and clean stop/adapter semantics for adopted deployments.

**Architecture:** Four independent changes. Responses is one proxy route. Health-check is an agent-side probe task feeding the existing `alive_by_cid` report seam. Persistence is a CLI that renders+installs a systemd unit. Semantics are two guard clauses on existing admin handlers.

**Tech Stack:** Python 3.11+, FastAPI, httpx, typer, SQLite, pytest. Spec: `docs/superpowers/specs/2026-05-23-berth-hardening-responses-design.md`.

---

## File Structure
- `src/berth/daemon/openai_proxy.py` — add `POST /v1/responses` route. (Task 1)
- `src/berth/daemon/admin_workloads.py` — adopted guard in `stop_deployment`. (Task 2)
- `src/berth/daemon/admin_adapters.py` — `supports_adapters` guard in `hot_unload_adapter`. (Task 2)
- `src/berth/cluster/agent_client.py` — `_probe_endpoint`, `_recompute_alive` helpers + `health_probe` task wiring. (Task 3)
- `src/berth/cli/agent_cmd.py` — `_render_agent_unit` helper + `install-service` command. (Task 4)
- Tests under `tests/unit/`.

---

## Task 1: `POST /v1/responses` route

**Files:**
- Modify: `src/berth/daemon/openai_proxy.py` (after the `/v1/embeddings` route, ~line 642)
- Test: `tests/unit/test_proxy_responses_route.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_proxy_responses_route.py
"""POST /v1/responses must route through the proxy to the engine's /v1/responses
(so OpenAI Responses clients like Codex work). It must NOT 404."""
from __future__ import annotations

from berth.daemon import openai_proxy


def test_responses_route_registered():
    paths = {r.path for r in openai_proxy.router.routes}
    assert "/v1/responses" in paths


def test_responses_route_is_post():
    methods = set()
    for r in openai_proxy.router.routes:
        if getattr(r, "path", None) == "/v1/responses":
            methods |= set(r.methods or [])
    assert "POST" in methods
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_proxy_responses_route.py -v`
Expected: FAIL — `/v1/responses` not in registered paths.

- [ ] **Step 3: Add the route**

In `src/berth/daemon/openai_proxy.py`, immediately after the `/v1/embeddings` route function (the one that does `return await _proxy(request, "/embeddings", key=key)`), add:

```python
@router.post("/v1/responses")
async def responses(
    request: Request,
    key: _api_keys_store.ApiKey | None = Depends(require_auth_dep),
):
    return await _proxy(request, "/responses", key=key)
```

(`Request`, `Depends`, `require_auth_dep`, `_api_keys_store`, and `router` are all already imported/defined in this module — mirror the `chat_completions` route exactly.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_proxy_responses_route.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/daemon/openai_proxy.py tests/unit/test_proxy_responses_route.py
git commit -m "feat(proxy): route POST /v1/responses to the engine (OpenAI Responses API)"
```

---

## Task 2: Stop / adapter guards for adopted deployments

**Files:**
- Modify: `src/berth/daemon/admin_workloads.py` (`stop_deployment`, ~line 300)
- Modify: `src/berth/daemon/admin_adapters.py` (`hot_unload_adapter`, ~line 350)
- Test: `tests/unit/test_adopted_admin_guards.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_adopted_admin_guards.py
"""Adopted deployments: leader-side stop is refused (agent owns them), and
adapter ops are refused cleanly (the adopted sentinel has no adapter path)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from berth.store import db
from berth.store import deployments as dep_store
from berth.store import models as model_store
from berth.daemon import admin_workloads, admin_adapters


def _conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def _adopted(conn):
    m = model_store.add(conn, name="mm", hf_repo="org/mm")
    return dep_store.upsert_adopted(
        conn, model_id=m.id, node_id=2, container_id="cid-1",
        address="127.0.0.1", port=30011, gpu_ids=[7],
        vram_reserved_mb=1, image_tag="external")


class _Mgr:
    def __init__(self): self.stopped = []
    async def stop(self, dep_id): self.stopped.append(dep_id)


def test_stop_adopted_is_rejected(tmp_path):
    import asyncio
    conn = _conn(tmp_path); dep = _adopted(conn); mgr = _Mgr()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(admin_workloads.stop_deployment(dep.id, manager=mgr, conn=conn))
    assert ei.value.status_code == 409
    assert "unadopt" in ei.value.detail
    assert mgr.stopped == []                       # never reached manager.stop


def test_stop_managed_still_calls_manager(tmp_path):
    import asyncio
    conn = _conn(tmp_path)
    m = model_store.add(conn, name="x", hf_repo="org/x")
    dep = dep_store.create(conn, model_id=m.id, backend="vllm", image_tag="i:1",
                           gpu_ids=[0], tensor_parallel=1, max_model_len=4096, dtype="auto")
    mgr = _Mgr()
    asyncio.run(admin_workloads.stop_deployment(dep.id, manager=mgr, conn=conn))
    assert mgr.stopped == [dep.id]


def test_hot_unload_on_adopted_is_rejected(tmp_path):
    import asyncio
    from berth.backends.adopted import AdoptedBackend
    conn = _conn(tmp_path); dep = _adopted(conn)
    backends = {"adopted": AdoptedBackend()}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(admin_adapters.hot_unload_adapter(
            dep_id=dep.id, adapter_name="a", conn=conn, backends=backends))
    assert ei.value.status_code == 409
    assert "adapter" in ei.value.detail.lower()
```

> NOTE: the real `stop_deployment` / `hot_unload_adapter` signatures use FastAPI `Depends(...)` defaults. Calling them directly (passing `manager=`, `conn=`, `backends=` by keyword) bypasses DI and works in a unit test. If a handler takes an adapter via the path/body differently, match the real signature — inspect with `grep -n "def hot_unload_adapter" -A6 src/berth/daemon/admin_adapters.py` and pass the same params; keep the two assertions (409 + never reaching the engine).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_admin_guards.py -v`
Expected: FAIL — adopted stop calls `manager.stop` (no 409); hot_unload reaches the engine path (no 409 / wrong error).

- [ ] **Step 3a: Guard `stop_deployment`**

In `src/berth/daemon/admin_workloads.py`, replace the body of `stop_deployment`:

```python
    dep = dep_store.get_by_id(conn, dep_id)
    if dep is None:
        raise HTTPException(404, f"no deployment with id {dep_id}")
    if dep.source == "adopted":
        raise HTTPException(
            409,
            f"deployment {dep_id} is an adopted endpoint owned by its agent; "
            f"run `berth agent unadopt <name>` on node {dep.node_id} to remove it",
        )
    await manager.stop(dep_id)
```

- [ ] **Step 3b: Guard `hot_unload_adapter`**

In `src/berth/daemon/admin_adapters.py`, replace the backend-None check in `hot_unload_adapter`:

```python
    backend = backends.get(dep.backend)
    if backend is None:
        raise HTTPException(409, f"backend {dep.backend!r} not registered")
    if not backend.supports_adapters:
        raise HTTPException(409, f"backend {dep.backend!r} does not support adapters")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_admin_guards.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/daemon/admin_workloads.py src/berth/daemon/admin_adapters.py tests/unit/test_adopted_admin_guards.py
git commit -m "feat(admin): reject leader stop + adapter ops on adopted deployments"
```

---

## Task 3: Adopted-endpoint health-check

**Files:**
- Modify: `src/berth/cluster/agent_client.py` (add helpers + `health_probe` task in `run_agent`)
- Test: `tests/unit/test_adopted_health_probe.py`

- [ ] **Step 1: Write the failing test** (pure helper — debounce + recovery + change-detection)

```python
# tests/unit/test_adopted_health_probe.py
from berth.cluster import adopted
from berth.cluster.agent_client import _recompute_alive


def _e(cid, port=1): return adopted.AdoptedEndpoint(
    name=cid, model_name="m", served_model_name="m", address="127.0.0.1",
    port=port, container_id=cid, gpu_ids=[0], vram_reserved_mb=1, image_tag="x")


def test_alive_stays_true_on_single_failure():
    entries=[_e("a")]; fails={}; alive={}
    # first probe ok -> alive True, no change vs default-true is fine
    _recompute_alive(entries, fails, alive, probe=lambda a,p: True)
    # one failure: debounced, stays alive
    changed=_recompute_alive(entries, fails, alive, probe=lambda a,p: False)
    assert alive["a"] is True and changed is False and fails["a"]==1


def test_two_failures_flip_to_down_then_recover():
    entries=[_e("a")]; fails={}; alive={"a":True}
    _recompute_alive(entries, fails, alive, probe=lambda a,p: False)  # fail 1
    changed=_recompute_alive(entries, fails, alive, probe=lambda a,p: False)  # fail 2
    assert alive["a"] is False and changed is True
    changed=_recompute_alive(entries, fails, alive, probe=lambda a,p: True)   # recover
    assert alive["a"] is True and changed is True and fails["a"]==0


def test_removed_endpoint_is_pruned():
    entries=[_e("a")]; fails={"a":0}; alive={"a":True}
    changed=_recompute_alive([], fails, alive, probe=lambda a,p: True)
    assert "a" not in alive and "a" not in fails and changed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_health_probe.py -v`
Expected: FAIL — `cannot import name '_recompute_alive'`.

- [ ] **Step 3a: Add the probe + recompute helpers**

In `src/berth/cluster/agent_client.py` (module level, near `build_adopted_report`), add:

```python
def _probe_endpoint(address: str, port: int, *, timeout: float = 5.0) -> bool:
    """True if the adopted endpoint answers /v1/models with 2xx."""
    import httpx
    try:
        r = httpx.get(f"http://{address}:{port}/v1/models", timeout=timeout)
        return r.status_code < 400
    except httpx.HTTPError:
        return False


def _recompute_alive(entries, fails_by_cid, alive_by_cid, *, probe=_probe_endpoint,
                     threshold: int = 2) -> bool:
    """Update alive/fail state by probing each entry. Returns True if any
    endpoint's alive flag flipped (so the caller knows to re-report). An
    endpoint stays alive until `threshold` consecutive failures."""
    changed = False
    seen = set()
    for e in entries:
        cid = e.container_id
        seen.add(cid)
        if probe(e.address, e.port):
            fails_by_cid[cid] = 0
            new_alive = True
        else:
            fails_by_cid[cid] = fails_by_cid.get(cid, 0) + 1
            new_alive = fails_by_cid[cid] < threshold
        if new_alive != alive_by_cid.get(cid, True):
            changed = True
        alive_by_cid[cid] = new_alive
    for cid in [c for c in alive_by_cid if c not in seen]:
        alive_by_cid.pop(cid, None)
        fails_by_cid.pop(cid, None)
        changed = True
    return changed
```

- [ ] **Step 3b: Wire the probe task into `run_agent`**

In `run_agent`'s connected block: (1) introduce shared state before the initial report, (2) pass it to the initial report and to `watch_adopted`, (3) add a `health_probe` task, (4) cancel it in the `finally`.

Replace the initial-report block:
```python
                _adopted = adopted_mod.load(berth_home)
                if _adopted:
                    register_adopted_endpoints(disp, _adopted)
                    await sender.send(encode_frame(
                        build_adopted_report(_adopted, alive_by_cid={})))
```
with:
```python
                alive_by_cid: dict[str, bool] = {}
                fails_by_cid: dict[str, int] = {}
                _adopted = adopted_mod.load(berth_home)
                if _adopted:
                    register_adopted_endpoints(disp, _adopted)
                    await sender.send(encode_frame(
                        build_adopted_report(_adopted, alive_by_cid)))
```

In `watch_adopted`, change the report call from `alive_by_cid={}` to the shared dict:
```python
                        await sender.send(encode_frame(
                            build_adopted_report(entries, alive_by_cid)))
```

After `wa = asyncio.create_task(watch_adopted())`, add:
```python
                async def health_probe(sender=sender, disp=disp):
                    while True:
                        await asyncio.sleep(15.0)
                        entries = adopted_mod.load(berth_home)
                        if not entries:
                            continue
                        changed = await asyncio.to_thread(
                            _recompute_alive, entries, fails_by_cid, alive_by_cid)
                        if changed:
                            register_adopted_endpoints(disp, entries)
                            log.info("adopted liveness changed, re-reporting")
                            await sender.send(encode_frame(
                                build_adopted_report(entries, alive_by_cid)))
                wp = asyncio.create_task(health_probe())
```

In the `finally:` that cancels `hb` and `wa`, also cancel `wp`:
```python
                    wp.cancel()
                    with suppress(asyncio.CancelledError):
                        await wp
```

- [ ] **Step 4: Run tests + import check**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_adopted_health_probe.py -v && uv run python -c "import berth.cluster.agent_client"`
Expected: PASS (3 tests), import exits 0.

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/cluster/agent_client.py tests/unit/test_adopted_health_probe.py
git commit -m "feat(agent): health-probe adopted endpoints; mark down after 2 misses"
```

---

## Task 4: `berth agent install-service`

**Files:**
- Modify: `src/berth/cli/agent_cmd.py` (add `_render_agent_unit` + `install-service` command)
- Test: `tests/unit/test_agent_install_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_agent_install_service.py
from berth.cli.agent_cmd import _render_agent_unit


def test_user_unit_has_execstart_and_home():
    u = _render_agent_unit(berth_bin="/opt/b/bin/berth",
                           berth_home="/home/x/.berth", system=False, run_user=None)
    assert "ExecStart=/opt/b/bin/berth agent start" in u
    assert "Environment=BERTH_HOME=/home/x/.berth" in u
    assert "Restart=on-failure" in u
    assert "User=" not in u                       # --user unit runs as the session user


def test_system_unit_sets_user():
    u = _render_agent_unit(berth_bin="/opt/b/bin/berth",
                           berth_home="/home/x/.berth", system=True, run_user="x")
    assert "User=x" in u
    assert "ExecStart=/opt/b/bin/berth agent start" in u
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_agent_install_service.py -v`
Expected: FAIL — `cannot import name '_render_agent_unit'`.

- [ ] **Step 3a: Add the renderer**

In `src/berth/cli/agent_cmd.py` (module level):

```python
def _render_agent_unit(*, berth_bin: str, berth_home: str, system: bool,
                       run_user: str | None) -> str:
    user_line = f"User={run_user}\n" if (system and run_user) else ""
    return f"""[Unit]
Description=berth agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=exec
{user_line}Environment=BERTH_HOME={berth_home}
ExecStart={berth_bin} agent start
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy={"multi-user.target" if system else "default.target"}
"""
```

- [ ] **Step 3b: Add the `install-service` command**

In `src/berth/cli/agent_cmd.py`, under the `agent_app` group:

```python
@agent_app.command("install-service")
def install_service(
    system: bool = typer.Option(False, "--system",
        help="Install a system unit (needs root); default is a --user unit."),
    user: bool = typer.Option(False, "--user", help="Install a user unit (default)."),
    berth_home: Path = typer.Option(None, "--berth-home",
        help="BERTH_HOME for the service (default: resolved agent home)."),
):
    """Install + enable a systemd unit that runs `berth agent start`."""
    import os
    import shutil
    import subprocess  # nosec

    home = str(berth_home or _berth_home())
    berth_bin = shutil.which("berth") or sys.argv[0]
    is_system = system and not user

    run_user = None
    if is_system:
        run_user = os.environ.get("SUDO_USER") or (
            None if os.geteuid() == 0 else os.environ.get("USER"))
        if not run_user:
            typer.echo(
                "--system needs a non-root account for the unit's User= "
                "(run via sudo, or use --user).", err=True)
            raise typer.Exit(1)
        unit_dir = Path("/etc/systemd/system")
    else:
        unit_dir = Path.home() / ".config" / "systemd" / "user"

    unit = _render_agent_unit(berth_bin=berth_bin, berth_home=home,
                              system=is_system, run_user=run_user)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "berth-agent.service"
    unit_path.write_text(unit)
    typer.echo(f"wrote {unit_path}")

    sysctl = shutil.which("systemctl")
    if not sysctl:
        typer.echo("systemctl not found; enable the unit manually.", err=True)
        return
    scope = [] if is_system else ["--user"]
    subprocess.run([sysctl, *scope, "daemon-reload"], check=False)  # nosec
    subprocess.run([sysctl, *scope, "enable", "--now", "berth-agent"], check=False)  # nosec
    typer.echo("berth-agent enabled and started.")
    if not is_system:
        typer.echo("tip: `loginctl enable-linger $USER` keeps it running after logout.")
```

(Confirm `sys` and `Path` are imported in `agent_cmd.py`; both are already used there.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit/test_agent_install_service.py -v && uv run python -c "import berth.cli.agent_cmd"`
Expected: PASS (2 tests), import exits 0.

- [ ] **Step 5: Commit**

```bash
cd /mnt/data/mark/projects/berth
git add src/berth/cli/agent_cmd.py tests/unit/test_agent_install_service.py
git commit -m "feat(cli): berth agent install-service (systemd persistence)"
```

---

## Task 5: Regression + lint/type gates

**Files:** none (verification only)

- [ ] **Step 1: Full unit suite**

Run: `cd /mnt/data/mark/projects/berth && uv run pytest tests/unit -q`
Expected: PASS, no regressions (report totals).

- [ ] **Step 2: CI static gates (these failed us before — run them locally)**

Run:
```bash
cd /mnt/data/mark/projects/berth
uv run --frozen ruff check src tests
uv run --frozen mypy src
uv run --with bandit bandit -r src --severity-level medium --confidence-level medium
```
Expected: ruff "All checks passed!", mypy "no issues", bandit "No issues identified." Fix anything they flag (e.g. `# nosec` already on the subprocess calls; sort imports with `ruff check --fix`).

- [ ] **Step 3: Commit any fixes**

```bash
cd /mnt/data/mark/projects/berth
git commit -am "style: satisfy ruff/mypy/bandit for hardening changes"   # only if needed
```

---

## Self-Review notes
- **Spec coverage:** Feature 1 health-check → Task 3 (probe + recompute + wiring); Feature 2 systemd → Task 4 (renderer + command); Feature 3 stop/adapter semantics → Task 2 (both guards); Feature 4 Responses → Task 1. Task 5 covers the CI gates that bit us last time (ruff on `tests`, mypy, bandit).
- **Naming consistency:** `_recompute_alive(entries, fails_by_cid, alive_by_cid, *, probe, threshold)`, `_probe_endpoint(address, port)`, `alive_by_cid`/`fails_by_cid` shared dicts, `_render_agent_unit(*, berth_bin, berth_home, system, run_user)`, `build_adopted_report(entries, alive_by_cid)` (unchanged signature) used consistently across Tasks 3 & 4.
- **No new frames/protocol:** all four reuse existing seams (proxy `_proxy`, the `ReportAdopted`/`alive_by_cid` report path, admin handlers). Leader needs zero changes.
- **Deferred (explicit, not dropped):** `GET/DELETE /v1/responses/{id}` (stateless); leader-driven unadopt (chose reject-with-guidance); managed-engine health (HealthMonitor already covers).
