# berth — Security Review (June 2026)

Reviewer: automated security review on branch `claude/repo-security-review-dn8gpl`.
Scope: full repository — auth & secrets, HTTP API surface, container/exec layer,
persistence/CLI/packaging/CI, and the web UI.

## Executive summary

berth's security posture is, on the whole, strong and clearly built with intent.
The high-value controls are present and correct: API keys are HMAC-SHA256 hashed
with a random on-disk pepper, tokens are 256-bit `secrets`-generated, there is no
`shell=True` anywhere (all container launches use exec-form argv), dangerous
deploy options are opt-in behind a config flag, the cluster uses real mTLS with
per-connection fingerprint checks against the node DB, SQL is fully parameterized,
serialization is `yaml.safe_load`/JSON only (no pickle/eval), the web UI has zero
XSS sinks with a no-`unsafe-inline` script CSP, and CI is exemplary (every action
SHA-pinned, default-deny permissions, no `pull_request_target`, secret scanning).

No **Critical** issues were found. The findings below are one **High** (a real
local credential leak in the VPS installer), a cluster of **Medium** items
(mostly hardening and lateral-movement reduction), and several **Low/Info**
defense-in-depth notes.

## Findings by severity

| # | Severity | Area | Finding |
|---|----------|------|---------|
| 1 | **High** | Packaging | Admin API key written to world-readable `/var/log/berth-install.log` |
| 2 | Medium | API/Auth | `/metrics` readable by any tenant key; leaks internal engine addresses & cluster topology |
| 3 | Medium | API/Auth | Admin routes mounted on the public TCP listener; auth bypass keyed on a fragile `scope["client"] is None` heuristic |
| 4 | Medium | Container | Engine images pinned by mutable tag, not digest |
| 5 | Medium | Container | Engine containers run with `ipc_mode: "host"` |
| 6 | Medium | API | SSRF: proxy/adapter/metrics dial `container_address` with no host validation for adopted endpoints |
| 7 | Medium | Auth | Per-key token-rate limits are advisory/post-hoc; non-`/v1` routes unmetered |
| 8 | Low | UI/Auth | Admin API key persisted in browser `localStorage` |
| 9 | Low | API/Auth | Stream ticket transmitted in URL query string (lands in proxy/access logs) |
| 10 | Low | API | Docker log-stream endpoint has no client-disconnect handling (privileged DoS) |
| 11 | Low | Docs | Examples/docs teach `curl -k` while sending Bearer tokens |
| 12 | Low | CLI | `berth wipe --home` guard allows wiping an entire user home dir |
| 13 | Low | CLI | Enrollment token / secrets exposed via argv and shell history |
| 14 | Low | CLI/Backup | Backup hot-snapshot created with default umask |
| 15 | Low | API | Verbose `{e}` / `response.text[:200]` exception echoes to authenticated clients |
| 16 | Info | Container | Cluster mTLS listener uses `CERT_OPTIONAL` rather than `CERT_REQUIRED` |
| 17 | Info | Packaging | Shipped `berth.service` far less hardened than the installer-generated unit |
| 18 | Info | Packaging | curl-pipe-bash install chain; uv installer unpinned |

---

## Detailed findings

### 1. HIGH — Admin API key leaks into world-readable install log
**File:** `scripts/setup-leader-vps.sh` — `LOG` at line 96, `step` harness line 158,
`do_bootstrap` lines 293–298, invocation line 559, log creation line 525.

The `step` harness redirects each step's output to `$LOG`
(`/var/log/berth-install.log`) via `( ... ) >>"$LOG" 2>&1`. The bootstrap step
mints the admin-tier API key and `tee`s it to a carefully-protected temp file
(`$BOOTSTRAP_OUT`, `chmod 0600`, deleted on EXIT) — but `tee`'s stdout still flows
into `$LOG`. `$LOG` is created with `: > "$LOG"` under root's default umask
(typically **0644, world-readable**), is never cleaned up, and is advertised in
the install summary.

**Impact:** any local unprivileged user on the leader VPS can read the admin-tier
key (`sk-…`) and gain full control of the `/admin/*` API — mint keys, deploy
containers, enroll nodes. The contrast with the meticulous `$BOOTSTRAP_OUT`
handling shows the risk was understood; the log path was just missed.

**Fix:** create the log private — `install -m 0600 /dev/null "$LOG"` or `umask 077`
at the top of the script — and/or redact `sk-[A-Za-z0-9_-]+` from logged output.

### 2. MEDIUM — `/metrics` readable by any tenant key, leaks cluster internals
**Files:** `src/berth/daemon/metrics_router.py:22-65`, `src/berth/auth/middleware.py:78-102`

`require_metrics_key` accepts any non-revoked key, including the lowest `trial`
tier. `/metrics` exposes deployment inventory, engine URLs
(`http://{container_address}:{port}`), per-node labels, and active key counts, with
no per-key rate limit. A low-tier tenant can scrape it to map internal container
addresses/ports and cluster topology for lateral-movement reconnaissance.

**Fix:** require `admin` tier (or a dedicated metrics scope) for `/metrics`, or
strip internal engine addresses from the tenant-visible output.

### 3. MEDIUM — Admin routes on the public listener gated by a fragile heuristic
**Files:** `src/berth/daemon/app.py:389`, `src/berth/daemon/admin.py:25-36`,
`src/berth/auth/middleware.py:13-17`

`admin_router` is mounted on the public TCP app, so the full `/admin/*` surface is
externally reachable, gated by `require_admin_key`. That guard bypasses auth when
`_is_uds_request()` is true, which keys on `request.scope.get("client") is None`.
This is correct for stock uvicorn TCP today (peer is always populated), but it is a
single load-bearing implicit check with no defense in depth. berth supports
reverse-proxy mode where proxy-header middleware rewrites `scope["client"]`; any
present or future middleware/listener wiring that leaves `client` unset on a TCP
listener silently turns full admin auth **off**.

**Fix:** rely solely on the explicit per-app `local_control_surface` flag (set only
on the UDS app); drop the `client is None` clause. Consider not mounting
`admin_router` on the public listener at all if remote admin isn't required.

### 4. MEDIUM — Engine images pinned by mutable tag, not digest
**Files:** `src/berth/backends/backends.yaml`, `src/berth/lifecycle/docker_client.py:167-168`, `:46-56`

Images are referenced as `image:tag` (e.g. `vllm/vllm-openai:v0.20.2`) and
pulled/run by tag with no digest pin or verification. Tags are mutable; a registry
compromise or upstream re-tag would launch a substituted image with full GPU access
on every host (including remote agents). The resulting image id is recorded
post-launch but never compared to an expected value.

**Fix:** pin `@sha256:…` digests per engine and pull/run by digest, or verify the
resolved `container.image.id` against a pinned digest before marking ready.

### 5. MEDIUM — Engine containers run with host IPC namespace
**File:** `src/berth/backends/base.py:104-119` (`ipc_mode: "host"`, `shm_size: "2g"`)

`ipc_mode="host"` places engine containers in the host's IPC namespace (shared
SysV shm/semaphores), weakening isolation between container and host and between
co-located engines — a useful primitive in an escape chain, especially given
TRT-LLM's `--trust_remote_code`. The explicit `shm_size: 2g` already provides a
private `/dev/shm`, so host IPC is likely unnecessary for the common case.

**Fix:** drop `ipc_mode: "host"` unless a specific tensor-parallel/NCCL path needs
it; if so, scope it to those deployments only.

### 6. MEDIUM — SSRF via unvalidated `container_address` for adopted endpoints
**Files:** `src/berth/daemon/dispatch.py:131-133`, `admin_adapters.py:310-319`/`362-368`, `metrics_router.py:54`

Upstream URLs are built by interpolating `container_address`/`container_port`. For
managed local containers this comes from the Docker bridge, but for *adopted*
deployments and agent-reported handles it is populated from registration input. A
compromised enrolled agent (or an attacker-influenced adopted endpoint) can set it
to an arbitrary internal host/port, which the leader then dials on the inference
hot path and on adapter load/unload. The `"tunnel"` sentinel correctly blocks
direct-dial for remote deployments — local/adopted addresses are dialed verbatim.

**Fix:** validate `container_address` is within the expected Docker network range
(or a resolvable container name on the managed network) before dialing; reject
loopback/link-local/metadata addresses for adopted endpoints.

### 7. MEDIUM — Token-rate limits are advisory; non-`/v1` routes unmetered
**Files:** `src/berth/auth/middleware.py:60-75`, `auth/limiter.py:54-68`, `store/key_usage.py:41-59`

The usage event is recorded only for `/v1/*` paths, so other authenticated routes
consume no quota. Token-per-window limits (`tpm`/`tpd`) are evaluated against
previously-completed requests because a request's own token cost isn't known at
admission — so a single request can exceed a token budget. Request limits are
enforced correctly; token ceilings are soft by design.

**Fix:** document token limits as advisory/post-hoc; if hard ceilings are required,
reserve an estimate at admission. Confirm non-`/v1` routes are intended unmetered.

### 8. LOW — Admin API key persisted in browser `localStorage`
**Files:** `ui/src/api.ts:1-13`, `ui/src/components/TokenGate.tsx:39-51`

The raw long-lived admin key is stored in `localStorage` (readable by any JS on the
origin, survives restarts). Theft requires XSS or a malicious extension — and the
UI has no XSS sinks today — but the blast radius is total, permanent admin
compromise. Most operators never sign out.

**Fix:** keep the key in memory (re-prompt per session) or use `sessionStorage`;
longer term, exchange it server-side for a short-lived session token.

### 9. LOW — Stream ticket transmitted in URL query string
**Files:** `ui/src/api.ts:43-50`, `src/berth/daemon/admin.py:54-86`, `src/berth/auth/stream_tokens.py`

SSE auth uses a `?stream_token=` ticket because `EventSource` can't set headers.
The ticket is strong (`token_urlsafe(32)`, 60s TTL, single-use, path-bound, minted
only via authenticated POST) and `Referrer-Policy: no-referrer` blocks referrer
leak — but query strings persist in reverse-proxy/access logs. An attacker with log
read access could replay an unconsumed ticket within 60s to read admin event/log
streams. *(Flagged independently by three of the five audits.)*

**Fix:** deliver the ticket via a short-lived cookie or header rather than the URL,
or document that `forwarded_allow_ips` proxies must not log query strings.

### 10. LOW — Docker log-stream endpoint lacks disconnect handling
**File:** `src/berth/daemon/admin_runtime.py:135-156`

`/admin/deployments/current/logs` is a synchronous `follow=True` generator with no
`await request.is_disconnected()` check (unlike the sibling SSE handlers). Abandoned
connections leave blocking log streams attached, tying up threads/Docker attach
handles. Admin-gated, so it's a privileged-DoS/cleanup issue.

**Fix:** add disconnect detection and an upper bound, mirroring `stream_engine_logs_sse`.

### 11. LOW — Docs/examples teach `curl -k` with Bearer tokens
**Files:** `examples/README.md:18,23`, `docs/troubleshooting.md:203`

`curl -k … -H "Authorization: Bearer $BERTH_TOKEN"` sends admin/API tokens over TLS
with verification disabled — an on-path attacker with any cert captures the token.
berth ships its own CA and prints its fingerprint, so the pattern should be
`curl --cacert ~/.berth/ca/ca.crt …`. (Using `-k` solely to fetch the unauthenticated
`/admin/ca.pem` as a TOFU step is acceptable.)

**Fix:** update docs to pin the CA; reserve `-k` for unauthenticated endpoints.

### 12. LOW — `berth wipe --home` can wipe an entire user home
**File:** `src/berth/cli/wipe_cmd.py:18-36,112-122`

`_validated_home` rejects a denylist and paths with `< 3` parts, but `/home/alice`
(exactly 3 parts) passes, after which `_wipe_home` deletes everything in it — not
just berth state. There is a confirm prompt (skippable with `-y`), and the wrapper
auto-escalates `wipe` to root via sudo, compounding the blast radius.

**Fix:** require a marker (only wipe dirs containing `db.sqlite`/`config.toml`, or
named `.berth`/matching `BERTH_HOME`) and delete only known berth artifacts.

### 13. LOW — Enrollment token / secrets exposed via argv
**Files:** `src/berth/cli/agent_cmd.py:387-408`, `nodes_cmd.py:81-86`

`berth agent register --uri 'berth://enroll?…&token=…'` puts the enrollment token
into shell history and `ps`. Mitigations are good (single-use, 10-min expiry, CA
pin), so impact is a narrow race.

**Fix:** offer reading the URI from stdin / `typer.prompt(hide_input=True)`.

### 14. LOW — Backup hot-snapshot created with umask permissions
**File:** `src/berth/cli/backup_cmd.py:40-50`

`sqlite3.connect(snapshot_path)` creates the intermediate snapshot with default
umask (often 0644); the dest dir is `mkdir`ed with default perms. Shielded by
`BERTH_DIR` being 0700 and unlinked in `finally`, so exposure is conditional. The
tarball itself is correctly `0o600`. *(`backup restore` is advertised but not
implemented — no tar-extraction traversal surface exists today; if added, use
`tarfile.extractall(filter="data")`.)*

**Fix:** pre-create the snapshot via `os.open(..., 0o600)` and `mkdir(mode=0o700)`.

### 15. LOW — Verbose exception echoes to authenticated clients
**Files:** `src/berth/daemon/admin_workloads.py:385`, `admin_adapters.py:157/321/324`,
`admin_runtime.py:196`, `openai_proxy.py:351/370-371`, `admin_cluster.py:151`

Several handlers surface raw `{e}` / `response.text[:200]` in HTTP responses,
leaking internal paths, hostnames, and engine internals to authenticated principals.

**Fix:** log full detail server-side; return generic messages to clients.

### 16–18. INFO — hardening notes
- **16** `src/berth/daemon/__main__.py:204-212`: cluster mTLS listener uses
  `ssl.CERT_OPTIONAL`. The app layer rejects certless connections (`leader_hub.py`),
  so not exploitable, but `CERT_REQUIRED` would reject anonymous TLS at the handshake.
- **17** `packaging/berth.service` has only basic sandboxing
  (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`, `ProtectHome`) while the
  installer-generated unit adds `ProtectSystem=strict`, `CapabilityBoundingSet=`,
  `SystemCallFilter=@system-service`, `UMask=0077`, etc. Sync the sample to the
  hardened profile. (Inherent: `ReadWritePaths=/var/run/docker.sock` makes the berth
  user root-equivalent on the host — document it.)
- **18** `scripts/install.sh:7-8,87`: curl-pipe-bash usage and an unpinned
  `astral.sh/uv/install.sh | sh`. Consider pinning a uv version + checksum and
  publishing the outer script's SHA.

---

## Verified-sound practices (checked and found correct)

- **API-key hashing:** HMAC-SHA256 with a 32-byte random pepper (`secrets.token_bytes`,
  file mode 0600, race-guarded init); keys are `sk-` + 256-bit `token_urlsafe`. The
  documented rationale for not using `hmac.compare_digest` post-DB is correct (the
  secret is hashed to fixed length before an equality `WHERE key_hash=?` lookup).
- **No `shell=True` / no string-interpolated commands** anywhere; container launches
  use docker-py exec-form argv; `extra_args` appended as discrete list items.
- **Dangerous deploy options opt-in:** custom image tags, raw `extra_args`, and the
  `trust_remote_code` TRT-LLM path are disabled unless `allow_unsafe_deploy_options`.
- **Input sanitization:** `model_name`/adapter/node-label regex-constrained;
  model/adapter paths guarded with `resolve().relative_to(models_dir)`; model cache
  mounted read-only.
- **No HF_TOKEN leakage** into engine containers; downloads happen host-side, only
  resolved weights are mounted ro.
- **Anti-SSRF tunnel sentinel** for remote deployments; **real mTLS** with
  per-connection peer-cert fingerprint checks against the node DB (not header-trust);
  heartbeat metrics size-capped.
- **SQL fully parameterized;** the only SQL f-strings interpolate program constants.
  No pickle/marshal/eval; `yaml.safe_load`/`safe_dump` throughout; malformed
  `allowed_models` JSON fails closed to deny-all.
- **No sensitive data persisted:** request metrics store only model/route/status/
  latency; prompts and Authorization headers are never written; the request tracer is
  in-memory only.
- **IPC auth:** CLI→daemon is a 0600 Unix socket inside a 0700 dir; no unauthenticated
  TCP control channel; TCP always requires Bearer, admin tier enforced on `/admin/*`.
- **File permissions:** `write_private_file` uses `O_NOFOLLOW` + `fchmod(0600)` before
  write (no TOCTOU); CA dir 0700, keys 0600.
- **Web UI:** zero XSS sinks (no `dangerouslySetInnerHTML`/`innerHTML`/`eval`, no
  markdown renderer); `script-src 'self'` with no `unsafe-inline`/`unsafe-eval`;
  `frame-ancestors 'none'` + `X-Frame-Options: DENY`; cookie-free bearer auth (no
  CSRF); no source maps or secrets in the bundle; `ignore-scripts=true`; only three
  runtime deps, all integrity-hashed.
- **Upstream proxy hardening:** request/response header allowlists strip the client
  Authorization and any engine-injected `Set-Cookie`/CORS headers; per-key model
  allowlist enforced on the *requested* name before alias resolution (no aliasing
  bypass); body-size cap on the public listener.
- **CI exemplary:** all actions SHA-pinned, default-deny `permissions`, no
  `pull_request_target`, no untrusted `${{ }}` in `run:`, `npm ci --ignore-scripts`,
  bandit + pip-audit + npm audit + gitleaks, provenance + SBOM on releases,
  weekly Dependabot + `uv lock --upgrade`. Dependencies current.

## Recommended remediation order

1. **Finding 1** (High) — fix the install-log key leak; smallest change, highest impact.
2. **Findings 2, 3** — `/metrics` tier gate and the explicit UDS-flag auth check;
   both are small, high-value reductions of admin/recon exposure.
3. **Findings 4, 5, 6** — image digest pinning, drop host IPC, validate
   `container_address`; reduce container/lateral-movement risk.
4. **Findings 8–15** — UI token storage, stream-ticket delivery, and the CLI/docs
   hardening items as a follow-up batch.
