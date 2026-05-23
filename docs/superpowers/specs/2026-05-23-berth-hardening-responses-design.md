# berth hardening + Responses API

**Status:** approved design, pre-implementation
**Date:** 2026-05-23
**Builds on:** the adopt feature (`docs/superpowers/specs/2026-05-23-berth-adopt-hosted-model-design.md`)

Four independent, small-to-medium improvements shipped as one increment. Each is its own section with its own tests.

---

## 1. Adopted-endpoint health-check

### Problem
The agent reports every adopted endpoint as `alive=True` (the `alive_by_cid` arg to `build_adopted_report` is always `{}`). If the operator's hand-started server dies, the leader keeps routing to it and clients get errors. berth must detect a dead adopted endpoint and stop routing to it (and resume when it recovers).

### Design
Agent-side periodic probe (only the agent can reach the endpoint's local address). In `run_agent`'s connected scope, alongside the existing `heartbeat` and `watch_adopted` tasks, add a third task `health_probe`:

- Maintain `alive_by_cid: dict[str, bool]` and `fails_by_cid: dict[str, int]` in the connected scope, shared with `watch_adopted` so every `build_adopted_report` call reflects current liveness.
- Every **15 s**, for each entry in `adopted.load(home)`: `GET http://{address}:{port}/v1/models` with a **5 s** timeout.
  - success → `fails=0`, `alive=True`.
  - failure (timeout / connect error / non-2xx) → `fails += 1`; `alive=False` once `fails >= 2` (debounce against a single blip).
- If any endpoint's `alive` changed since the last probe, re-send `ReportAdopted` (built with the shared `alive_by_cid`).
- `watch_adopted` and the initial connect report also use the shared `alive_by_cid` (so a file change doesn't reset everything to alive).

Leader side needs **no change**: `reconcile_adopted` already maps `alive→status` (`ready`/`failed`), and `find_ready_by_model_name` only returns `ready` rows — so a `failed` adopted deployment stops receiving traffic and resumes when it flips back to `ready`.

Probe target is `/v1/models` (engine-agnostic, cheap, already used by `adopt`).

### Testing
- Unit: a `_recompute_alive(entries, fails_by_cid, probe_fn)` helper (pure, injectable probe) — success keeps alive; 1 failure stays alive (debounce); 2 consecutive failures → `alive=False`; recovery → `alive=True`. Assert it only signals "changed" when a value actually flips.
- Unit: `build_adopted_report` already tested; add a case with `alive_by_cid={"cid":False}` → `endpoints[0]["alive"] is False`.

---

## 2. Agent persistence (systemd)

### Problem
The agent runs as a foreground `berth agent start`; only the leader ships a systemd unit. When the operator's shell/session ends, the agent (and its adoptions) stop. There's no supported way to run the agent durably.

### Design
New CLI command `berth agent install-service [--user | --system] [--berth-home PATH]`:

- Resolves the absolute `berth` executable (`sys.argv[0]` / `shutil.which`), and `BERTH_HOME` (flag → env → default).
- Writes a systemd unit `berth-agent.service` running `ExecStart=<berth> agent start`, `Environment=BERTH_HOME=<home>`, `Restart=on-failure`, `RestartSec=5`.
- **`--user` (default):** writes `~/.config/systemd/user/berth-agent.service`, runs `systemctl --user daemon-reload` + `enable --now`. Runs as the invoking user, inheriting their Docker access (needed for `adopt --container` introspection). Prints a note to run `loginctl enable-linger $USER` so it survives logout.
- **`--system`:** writes `/etc/systemd/system/berth-agent.service` (requires root). The unit's `User=` is the human operator account — `$SUDO_USER` when run via sudo, else the current non-root user — so the service keeps that user's Docker access and `~/.berth`; if it can't determine a non-root account (e.g. run as root directly with no `$SUDO_USER`), it errors and tells the operator to pass `--user` or run under sudo. Then `daemon-reload` + `enable --now`.
- Hardening: a modest subset of the leader unit's directives — **not** `ProtectSystem=strict`/`PrivateDevices` (the agent talks to the Docker socket and may need GPU visibility for introspection). Include `NoNewPrivileges`, `RestartSec`, journal logging.
- If `systemctl` is absent, write the unit and print manual-enable instructions (exit 0, don't fail).

The leader's existing `packaging/berth.service` is untouched (it's leader-specific). Optionally drop a reference `packaging/berth-agent.service` template for documentation.

### Testing
- Unit: render the unit-file content for `--user` and `--system` (a pure `_render_agent_unit(berth_bin, berth_home, system: bool)` helper) and assert `ExecStart`, `Environment=BERTH_HOME=`, `Restart=`, and (system) `User=` are present and correct. Don't invoke real `systemctl` in tests — gate the actual install behind a thin wrapper that the test stubs, or test only the renderer + path selection.

---

## 3. Stop / unadopt semantics

### Problem
Two leaks for adopted deployments:
1. Operator `berth stop <adopted>` calls `manager.stop`, which marks the row `stopped` — but the agent re-reports it back to `ready` on its next report, so the stop silently doesn't stick.
2. `hot_unload_adapter` (`admin_adapters.py:338`) only guards `backend is None`; the `AdoptedBackend` sentinel is *registered*, so it slips through to `backend.adapter_unload_path` (`""`) and issues a malformed request instead of refusing. (`hot_load_adapter` already refuses via the `supports_adapters` check.)

### Design
- **Reject operator stop of an adopted deployment.** In `admin_workloads.py` `stop_deployment` (line 301), before `await manager.stop(dep_id)`: load the deployment; if `source == 'adopted'`, raise `HTTPException(409, "adopted deployment N is owned by its agent; run `berth agent unadopt <name>` on node <node_id> to remove it")`. This matches the agent-authoritative model — the agent owns its adopted set; the leader shouldn't fight it. `stop_all_deployments` and internal `manager.stop_all()` are unchanged (the adopted branch in `_stop_locked` already no-ops the container cleanly on shutdown).
- **Reject adapter ops on adopted.** In `hot_unload_adapter`, add the same guard `hot_load_adapter` uses: after resolving `backend`, if `backend is None or not backend.supports_adapters` → `HTTPException(409, "backend <name> does not support adapters")`. `AdoptedBackend.supports_adapters` is already `False`, so this catches it without an adopted-specific branch.

### Testing
- Unit: `stop_deployment` on a `source='adopted'` row → 409 with the unadopt guidance; on a managed row → still calls `manager.stop` (existing behavior).
- Unit: `hot_unload_adapter` against a deployment whose backend is the adopted sentinel → 409, and `adapter_unload_path` is never reached (no engine call). Mirror the existing `hot_load_adapter` adopted/unsupported test pattern.

---

## 4. Responses API routing (`POST /v1/responses`)

### Problem
The proxy enumerates routes (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`); `/v1/responses` 404s. OpenAI Codex (and other Responses-only clients) can't use berth. The engine already implements it (verified: SGLang returns `{"object":"response", ...}` HTTP 200 for `POST /v1/responses`).

### Design
Add one route in `openai_proxy.py`, mirroring `/v1/chat/completions`:

```python
@router.post("/v1/responses")
async def responses(request: Request, key=Depends(require_auth_dep)) -> Response:
    return await _proxy(request, "/responses", key=key)
```

(Use the exact dependency/signature of the sibling POST routes.) `_proxy` already: resolves the deployment from the request body's `model`, builds `engine_path = backend.openai_base + "/responses"` → `/v1/responses` upstream, and handles SSE streaming — so streaming Responses requests (Codex's default) work unchanged.

**Out of scope (deferred):** `GET`/`DELETE /v1/responses/{id}`. Those address server-stored response objects, but the deployment can't be resolved from an opaque id (no `model` in the request) in a multi-model cluster, and the operator runs stateless (`store:false`, no server-side conversation persistence) — so there's nothing to retrieve or delete. Revisit only if server-side storage + multi-model is later required.

### Testing
- Unit: the route exists and forwards to `_proxy(..., "/responses", ...)` — extend the existing proxy-routing test (`tests/unit/test_proxy_adopted_routing.py` style) to drive `POST /v1/responses` against a stub upstream and assert it is **not** 404 and reaches the engine's `/v1/responses` (patch the final httpx call, assert the path).

---

## File-change summary (for the plan)
- `src/berth/cluster/agent_client.py` — `health_probe` task + shared `alive_by_cid`/`fails_by_cid`; `_recompute_alive` helper. (Feature 1)
- `src/berth/cli/agent_cmd.py` — `install-service` command + `_render_agent_unit` helper. (Feature 2)
- `src/berth/daemon/admin_workloads.py` — adopted guard in `stop_deployment`. (Feature 3)
- `src/berth/daemon/admin_adapters.py` — `supports_adapters` guard in `hot_unload_adapter`. (Feature 3)
- `src/berth/daemon/openai_proxy.py` — `POST /v1/responses` route. (Feature 4)
- Optional: `packaging/berth-agent.service` reference template. (Feature 2)
- Tests as above under `tests/unit/`.

## Non-goals
- Leader-driven unadopt over a new control frame (chose reject-with-guidance instead).
- `GET/DELETE /v1/responses/{id}` and server-side response storage.
- Active health-probing of *managed* engines (HealthMonitor already covers those).
- Per-endpoint health history / richer dashboard status (only up/down routing matters here).
