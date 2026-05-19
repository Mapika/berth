# Pre-Deploy Hardening Plan

> **For agentic workers:** Implement task-by-task using TDD where the change is behavioral; refactor-only items get a "no regression" check via existing tests. Steps use checkbox (`- [ ]`) for tracking.

**Goal:** Close every audit-flagged blocker between today's `main` and a public VPS deploy. Once these land, `serve deploy bootstrap` can assume a hardened, ops-friendly daemon and the command itself stays short.

**Source audits**: three parallel reviews dispatched 2026-05-19 (security, tech-debt, deploy-readiness). Consolidated punch list in chat history; this plan implements every high-severity item plus the high-leverage mediums called out by the deploy-readiness audit.

**Sequencing principle**: independent quick wins first (compounding momentum, smaller PR-style commits), then refactors, then config/deploy story. Each task is its own commit.

---

## Task 1: bound `_rl_buckets` growth + add `/admin/stream-token` to the limiter

**Files:**
- Modify: `src/serve_engine/daemon/admin.py` (around `_rate_limit`, `_rl_buckets`, the stream-token route)
- Test: `tests/unit/test_rate_limit.py` (extend)

- [ ] Add a test that calls `_rate_limit` with 100 distinct IPs then asserts `len(_rl_buckets)` shrinks back to 0 after the window expires.
- [ ] In `_rate_limit`, after popping stale timestamps, delete the key when the deque is empty.
- [ ] Apply `_rate_limit` to `POST /admin/stream-token` (60/60s default).
- [ ] Run `pytest tests/unit/test_rate_limit.py -v`.
- [ ] Commit: `fix(admin): evict empty rate-limit buckets + cap stream-token issuance`.

## Task 2: request body size cap on `/v1/*` and the public listener

**Files:**
- Modify: `src/serve_engine/daemon/app.py` (add a Starlette `BaseHTTPMiddleware` that caps `Content-Length`)
- Test: `tests/integration/test_proxy_body_size_limit.py` (new)

- [ ] Test: POST 11 MB body to `/v1/chat/completions` → expect 413.
- [ ] Test: POST 9 MB body → reaches the resolver (we hit 503 "no ready deployment" but not 413 — proves the cap doesn't fire below the limit).
- [ ] Implement middleware capping at 10 MB on `public_app`. Configurable via `app.state.max_body_size_bytes` with a default constant; `_attach_state` reads `request.app.state.max_body_size_bytes` or the default.
- [ ] Skip the cap entirely on `uds_app` (operator may upload adapter weights via admin endpoints).
- [ ] Run `pytest tests/integration/test_proxy_body_size_limit.py -v` and the existing proxy suite.
- [ ] Commit: `feat(daemon): 10 MB request-body cap on the public listener`.

## Task 3: fix `_local_metrics_tick` to read the right collector instance

**Files:**
- Modify: `src/serve_engine/daemon/app.py` (the lifespan closure)
- Test: covered indirectly by existing proxy tests + a new unit assertion

**Root cause** (from audit): the closure captures `public_app.state.in_flight`, but in the `build_app()` path used by tests the proxy increments `uds_app.state.in_flight` (different object). Even in production, the closure binding is fragile.

- [ ] Construct one shared `InFlightCounter`/`LatencyRecorder` pair outside `_attach_state`, store them on the `build_apps`-level scope, and pass them into `_attach_state` so all three apps share the same instances.
- [ ] `_local_metrics_tick` reads from the shared instances directly (no `app.state` lookup).
- [ ] Test: in a unit test, build the app and verify `public_app.state.in_flight is uds_app.state.in_flight is cluster_app.state.in_flight`.
- [ ] Run full suite (focus on metrics + proxy tests).
- [ ] Commit: `fix(daemon): share InFlightCounter+LatencyRecorder across all three apps`.

## Task 4: expose `manager.models_dir` as a property, remove seven `_models_dir` reaches

**Files:**
- Modify: `src/serve_engine/lifecycle/manager.py` (add property)
- Modify call sites in `daemon/openai_proxy.py`, `daemon/admin.py` (5 sites)
- Test: none directly; existing suite covers these paths

- [ ] Add `@property def models_dir(self) -> Path: return self._models_dir` to `LifecycleManager`.
- [ ] Replace every `manager._models_dir` (or `self._models_dir` from outside the class) with `manager.models_dir`.
- [ ] Run full suite.
- [ ] Commit: `refactor(lifecycle): models_dir public property; drop 7 private-attr reaches`.

## Task 5: `DockerClient.get_container` public method

**Files:**
- Modify: `src/serve_engine/lifecycle/docker_client.py`
- Modify: `src/serve_engine/lifecycle/manager.py` (reconcile call site)

- [ ] Add `def get_container(self, container_id: str) -> ContainerHandle | None` to `DockerClient` that wraps `self._client.containers.get(id)` and returns `None` on `NotFound`.
- [ ] Replace `self._docker._client.containers.get(...)` in `manager.reconcile` with `self._docker.get_container(...)`.
- [ ] Run `pytest tests/unit/test_docker_client.py` + integration suite.
- [ ] Commit: `refactor(docker): public get_container; reconcile drops private-API reach`.

## Task 6: `/metrics` auth — bearer or move to UDS

**Decision**: bearer-auth on the public listener. Prometheus operators can scrape with a dedicated API key. Moving to UDS would break the standard Prometheus deployment pattern.

**Files:**
- Modify: `src/serve_engine/daemon/metrics_router.py`
- Modify: `src/serve_engine/auth/middleware.py` (or admin.py) — add a `require_metrics_key` dep that accepts any non-revoked key (no tier requirement)
- Test: `tests/unit/test_metrics_auth.py` (new)

- [ ] Test: unauth GET `/metrics` → 401.
- [ ] Test: GET `/metrics` with a valid API key → 200 + non-empty body.
- [ ] Wire `require_metrics_key` dep onto the router. Allow the UDS bypass so `serve metrics` CLI still works.
- [ ] Run new test + existing metrics tests.
- [ ] Commit: `feat(observability): require an API key for /metrics on the public listener`.

## Task 7: close the no-keys auth bypass for `/v1/*` over TCP

**Files:**
- Modify: `src/serve_engine/auth/middleware.py`
- Test: `tests/integration/test_no_keys_bypass.py` (new)

- [ ] Test (regression): POST `/v1/chat/completions` over the UDS with no keys registered → still works (operator bootstrap).
- [ ] Test (new): POST `/v1/chat/completions` over TCP transport with no keys registered → 401.
- [ ] In `require_auth_dep`, allow the no-keys bypass only when `_is_uds_request(request)` returns True. Add a startup log warning when count_active==0.
- [ ] Run the proxy suite to confirm no regressions; the existing UDS-based fixtures should keep passing.
- [ ] Commit: `fix(auth): no-keys bypass only on UDS; TCP /v1/* requires auth from boot`.

## Task 8: upstream response header allowlist

**Files:**
- Modify: `src/serve_engine/daemon/openai_proxy.py` (the `forward_headers` construction)
- Test: `tests/integration/test_proxy_header_allowlist.py` (new)

- [ ] Test: configure a fake engine that emits `Set-Cookie: x=1`, `Access-Control-Allow-Origin: *`, `Link: …`. Proxy response must not contain those.
- [ ] Test: `Content-Type` and `X-Request-Id` still pass through.
- [ ] Define `_FORWARDABLE = {"content-type", "content-encoding", "cache-control", "x-request-id", "x-trace-id"}`. Replace the blocklist with `{k: v for k, v in upstream.headers.items() if k.lower() in _FORWARDABLE}`. Content-type handled via media_type as today.
- [ ] Run.
- [ ] Commit: `fix(proxy): allowlist upstream response headers; drop blocklist`.

## Task 9: API-key storage — switch to HMAC-pepper

**Decision**: HMAC-SHA256 with a 32-byte pepper stored in `~/.serve/key_pepper` (mode 0600), generated at first daemon start. Faster than bcrypt/argon2 (key verification on the hot path of every `/v1/*` request), still defeats offline brute-force if the DB leaks. Stop-the-world for existing keys is acceptable — this is a single-user dev install.

**Files:**
- Modify: `src/serve_engine/store/api_keys.py` (the hash + compare functions)
- Modify: `src/serve_engine/daemon/app.py` (load/create pepper at startup, attach to `app.state.key_pepper`)
- Migration: `src/serve_engine/store/migrations/016_key_hash_v2.sql` (drop existing rows or mark them invalid)
- Test: `tests/unit/test_api_keys_pepper.py` (new)

- [ ] Test: hash with pepper A ≠ hash with pepper B for the same secret.
- [ ] Test: verify is constant-time (use `hmac.compare_digest` — assert against `==`).
- [ ] Test: missing pepper file at startup → generate one, mode 0600.
- [ ] Migration drops/clears existing key rows (write a comment in the migration explaining the stop-the-world choice).
- [ ] Implement `_hash(secret, pepper)` and `_verify(secret, stored_hash, pepper)`.
- [ ] Existing `create`/`find_by_secret` accept the pepper; load it from app.state where needed.
- [ ] Run unit + integration suites; create a fresh admin key via CLI to confirm the flow.
- [ ] Commit: `feat(security): HMAC-pepper for API-key storage; migration 016 invalidates old hashes`.

## Task 10: reverse-proxy mode (`scheme`, `trust_proxy_headers`, uvicorn kwargs)

**Files:**
- Modify: `src/serve_engine/config.py` (add `[public].scheme`, `[public].trust_proxy_headers`, `[public].forwarded_allow_ips`)
- Modify: `src/serve_engine/daemon/__main__.py` (pass `proxy_headers=True`, `forwarded_allow_ips=...` to uvicorn when configured)
- Modify: `src/serve_engine/cli/daemon_cmd.py` (read + log the resolved values)
- Test: `tests/unit/test_config_resolution.py` (extend)

- [ ] Test: config with `[public] scheme = "http"` + `trust_proxy_headers = true` resolves to expected uvicorn kwargs (assertions on a helper that maps config → uvicorn kwargs).
- [ ] Test: default config (no [public] block) produces today's behaviour (TLS direct).
- [ ] In `__main__.py`, when `scheme=="http"` skip ssl_keyfile/ssl_certfile and bind plain HTTP; when `trust_proxy_headers=true` pass `proxy_headers=True, forwarded_allow_ips=cfg.forwarded_allow_ips or "127.0.0.1"`.
- [ ] Run.
- [ ] Commit: `feat(config): behind-reverse-proxy mode (scheme=http, trust_proxy_headers)`.

## Task 11: rate limiter consumes `X-Forwarded-For` when configured

**Files:**
- Modify: `src/serve_engine/daemon/admin.py` (`_client_ip`)
- Test: `tests/unit/test_rate_limit.py` (extend)

- [ ] Test: when `app.state.trust_proxy_headers = True`, `_client_ip(req)` reads the rightmost untrusted hop from `X-Forwarded-For`.
- [ ] Test: when `trust_proxy_headers = False` (default), header is ignored and ASGI scope IP is used.
- [ ] Implement: `_client_ip` reads `app.state.trust_proxy_headers`; uses last-untrusted parsing.
- [ ] Run.
- [ ] Commit: `fix(admin): honour X-Forwarded-For when trust_proxy_headers is set`.

## Task 12: fix `public_host` autodetect + advertise

**Files:**
- Modify: `src/serve_engine/config.py` (`resolve_config`)
- Modify: `src/serve_engine/cli/daemon_cmd.py` (startup banner / warning)
- Test: `tests/unit/test_config_resolution.py` (extend)

- [ ] Test: when `public_host` is autodetected to a loopback (127.x) AND public_bind is 0.0.0.0 AND no explicit override, resolve() emits a warning + flag (`source["public_host"] == "autodetect-loopback-suspect"`).
- [ ] Banner refuses to silently advertise loopback when bind is global; require operator to set explicit `public_host` or pass `--allow-loopback-advertise`.
- [ ] Run.
- [ ] Commit: `fix(config): refuse to advertise loopback as public_host when bound globally`.

## Task 13: migration advisory lock + version stamp

**Files:**
- Modify: `src/serve_engine/store/db.py` (`init_schema`)
- Migration: `src/serve_engine/store/migrations/017_schema_version.sql`
- Test: `tests/unit/test_migrations.py` (new)

- [ ] Test: two threads calling `init_schema` concurrently both succeed; no duplicate migrations.
- [ ] Test: running on a DB with a higher `schema_version` than the binary knows → init_schema raises a clear error ("DB schema 18 newer than this binary's known 17 — refusing to start").
- [ ] Add `schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT)` table (migration 017 self-installs it on first run).
- [ ] `init_schema` acquires `BEGIN EXCLUSIVE`; reads max applied version; iterates migrations newer than that; on each apply, INSERTs into `schema_versions`.
- [ ] Run.
- [ ] Commit: `feat(store): advisory-locked migrations + schema_version stamp`.

## Task 14: remote-deploy health probe before marking `ready`

**Files:**
- Modify: `src/serve_engine/lifecycle/manager.py` (the remote-deploy success path)
- Test: `tests/unit/test_remote_dispatch.py` (extend)

- [ ] Test: remote-deploy where probe returns 200 within timeout → row → ready.
- [ ] Test: remote-deploy where probe never returns 200 within timeout → row → failed, error message references the probe.
- [ ] After OpResult success, call `link.probe_container(health_path=backend.healthz_path)` in a loop with 2 s interval, 30 s ceiling. Only mark `ready` on first 200; otherwise mark `failed` with the last error.
- [ ] Run.
- [ ] Commit: `fix(lifecycle): remote deploys gated on health probe before marking ready`.

## Task 15: `/readyz` distinct from `/healthz`

**Files:**
- Modify: `src/serve_engine/daemon/app.py` (`_attach_state` adds both endpoints; `lifespan` sets a ready flag)
- Test: `tests/unit/test_readyz.py` (new)

- [ ] Test: `/healthz` returns 200 at all times (lifespan not yet up included).
- [ ] Test: `/readyz` returns 503 until lifespan signals ready.
- [ ] Test: `/readyz` returns 503 if DB select 1 fails.
- [ ] Implement: an `app.state.ready = False` flag; lifespan sets to True after reconcile+task starts; `/readyz` checks the flag AND does a `SELECT 1` against the DB AND verifies the local AgentLink is registered.
- [ ] Run.
- [ ] Commit: `feat(daemon): split /healthz (liveness) from /readyz (readiness)`.

## Task 16: systemd unit + `--foreground` daemon mode

**Files:**
- New: `packaging/serve-engine.service`
- Modify: `src/serve_engine/daemon/__main__.py` (respect `SERVE_FOREGROUND=1` or a `--foreground` arg — log to stderr, no PID file)
- Modify: `src/serve_engine/cli/daemon_cmd.py` (pass-through flag)
- Test: smoke-script under `scripts/` (manual; document expected output)

- [ ] Add `daemon start --foreground` to typer (default False).
- [ ] In `__main__`, when foreground: skip PID file write, route logging to stderr, no `start_new_session`.
- [ ] Ship `packaging/serve-engine.service`: Type=exec, ExecStart=`uv run serve daemon start --foreground` (or `python -m serve_engine.daemon`), User=serve, WorkingDirectory=/var/lib/serve, Restart=on-failure, StandardOutput=journal.
- [ ] Document under `docs/deploy.md` (skeleton).
- [ ] Commit: `feat(daemon): --foreground mode + ship systemd unit`.

## Task 17: CA private key permissions + backup story

**Files:**
- Modify: `src/serve_engine/cluster/ca.py` (`generate_ca` / `load_ca` — ensure mode 0o600 on the key, 0o700 on ca/ dir)
- Modify: `docs/multi-node.md` (backup warning)
- New: `src/serve_engine/cli/backup_cmd.py` + register in `cli/__init__.py`
- Test: `tests/unit/test_ca_permissions.py` (new) + `tests/unit/test_backup_cmd.py` (new)

- [ ] Test: `generate_ca` creates `ca.key` with stat-mode 0o600 and `ca/` dir 0o700.
- [ ] Test: `serve backup <dir>` produces a tarball containing db.sqlite (via `.backup`), ca/, config.toml; restores from tarball give the same fingerprints.
- [ ] Implement `serve backup` (uses `sqlite3.Connection.backup()` for consistent DB copy).
- [ ] Document the recipe in `docs/multi-node.md` ("ca/ca.key + db.sqlite + config.toml is your DR set").
- [ ] Commit: `feat(security): tighten CA key perms; ship serve backup`.

## Task 18: documentation pass

**Files:**
- Modify: `docs/multi-node.md` (cross-link new things)
- New: `docs/deploy.md` (the operator-facing bring-up guide stub the bootstrap command will reference)
- New: `docs/caddy.md` (example Caddyfile for the Caddy-front mode)

- [ ] `docs/deploy.md` covers: required VPS specs, DNS + ACME, ports, `serve deploy bootstrap` usage (placeholder until built), backup story, common pitfalls.
- [ ] `docs/caddy.md` shows `your.domain { reverse_proxy 127.0.0.1:11500 }` plus the cluster-port note + `trust_proxy_headers` config.
- [ ] Commit: `docs: deploy + caddy guides`.

---

## Self-Review

**Spec coverage** (against the 18 consolidated audit findings):

| # | Finding | Task |
|---|---|---|
| 1 | /metrics unauth | 6 |
| 2 | /v1/* no-keys bypass | 7 |
| 3 | SHA-256 keys | 9 |
| 4 | Rate limit ASGI IP | 11 (depends on 10) |
| 5 | Body size unbounded | 2 |
| 6 | Upstream header forwarding | 8 |
| 7 | CA key on disk | 17 |
| 8 | metrics_tick wrong instance | 3 |
| 9 | _models_dir reach | 4 |
| 10 | docker._client reach | 5 |
| 11 | _rl_buckets unbounded | 1 |
| 12 | Remote deploy no health | 14 |
| 13 | No reverse-proxy mode | 10 |
| 14 | public_host vs bind | 12 |
| 15 | Migration safety | 13 |
| 16 | /readyz missing | 15 |
| 17 | systemd + logs | 16 |
| 18 | Backup | 17 |

**Sequencing**: independent quick wins (1, 2, 3) → refactors (4, 5) → security fixes (6, 7, 8) → key-storage migration (9) → config + deploy story (10, 11, 12, 13) → lifecycle hardening (14, 15) → packaging (16, 17) → docs (18).

**Placeholder scan**: clean — every task has files, an explicit change, and a verification.

**Risk hotspots**: Task 9 (key migration, stop-the-world for existing keys) and Task 13 (migrations on running boxes) are the two that need care. Both have explicit test gates.

---

## Execution

Subagent-driven would be ideal here (18 reviewable units), but given session continuity I'll execute inline, one task per commit, full suite after each. Stopping points after Tasks 9 and 13 (the two highest-risk landings) for visual confirmation.
