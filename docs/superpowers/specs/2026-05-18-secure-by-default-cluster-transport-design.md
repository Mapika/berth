# Secure-by-Default Cluster Transport — Design

Status: approved for planning
Date: 2026-05-18

## Summary

The existing multi-node design (`2026-05-18-multi-node-serving-design.md`)
specifies an mTLS WebSocket between leader and agents but leaves TLS
termination, address configuration, and enrollment UX to the operator.
In practice the daemon serves plain HTTP, binds to `127.0.0.1` by
default, treats the advertised leader URL as a hand-typed env var, and
splits enrollment into three loose strings (URL, token, CA). The result
is a setup that only works behind a reverse proxy on a trusted LAN.

This spec extends the daemon to:

1. Serve TLS directly from uvicorn — no reverse proxy required.
2. Split traffic across **two listeners** with distinct trust domains:
   a **public** listener for client/SDK API traffic (operator-supplied
   public-CA cert) and a **cluster** listener for agent transport
   (self-signed cluster CA + mTLS). This makes the system safe to expose
   on the public internet, not just a trusted LAN.
3. Resolve the daemon's public address from a config file (with
   autodetect fallback) and advertise it honestly in the startup output.
4. Bundle enrollment into a single signed URI (leader + token + CA
   fingerprint) that an agent host can paste in one line.

## Goals

- A leader on a public IP can serve `/v1/*` to external SDK clients with
  a browser-trusted certificate and accept agent connections from across
  the internet with cryptographically pinned identity — no reverse proxy.
- `serve daemon start` with no flags produces a daemon that is reachable
  on the LAN with TLS (cluster-CA fallback cert) for development.
- The startup message tells the user the exact URLs (public + cluster)
  and the CA fingerprint that agents should pin.
- Agent registration is one copy-paste line.
- mTLS for `/cluster/agent` is enforced inside the daemon process.
- Single-node installs work without configuration changes; the change
  for them is "daemon now speaks TLS on its public port."

## Non-Goals

- ACME / Let's Encrypt auto-issuance. v1 requires operator-supplied
  cert+key (or accepts the cluster-CA fallback with a warning). Separate
  spec.
- mDNS / zeroconf / cloud-tag auto-discovery.
- IPv6 (autodetect returns IPv4; manual config accepts any address).
- WireGuard / Tailscale / overlay integration. The `cluster_bind` knob
  lets operators pin the cluster listener to a VPN interface; that's the
  full extent of our overlay support.
- WAF / DDoS protection — CDN/Cloudflare territory.
- IP allowlist for the cluster listener — firewall is the right layer.
- Cert rotation beyond SAN-mismatch regeneration of the cluster-CA
  fallback cert.
- Multi-leader / HA / failover.
- QR codes for the enrollment URI.

## Security model

Trust anchors:

- **Cluster CA** (`~/.serve/ca/ca.{crt,key}`). Existing. Root of trust
  for the cluster transport. Whoever holds `ca.key` can mint agent or
  cluster-server certs. CA key lives only on the leader.
- **Leader cluster-server cert** (new). Signed by the cluster CA. SAN
  includes the configured cluster host + `127.0.0.1` + `localhost`. Used
  by uvicorn on the **cluster listener** only. Regenerated automatically
  if its SAN no longer covers the configured host.
- **Leader public-server cert** (operator-supplied). Used by uvicorn on
  the **public listener**. Should chain to a public CA so external
  clients work without trusting the cluster CA. Falls back to the
  cluster-CA-signed cert with a startup warning if no public cert is
  configured.
- **Agent client certs** (existing). Signed by the cluster CA at
  enrollment. Used as the TLS client cert on the cluster listener.
- **Enrollment token** (existing). Short-TTL, single-use. Travels inside
  the enrollment URI bundled with the CA fingerprint.

Threat model and mitigations:

| Threat | Mitigation |
|---|---|
| Passive eavesdrop on cluster traffic | TLS on the cluster listener; mTLS on `/cluster/agent` |
| Active MITM during CA bootstrap on first enrollment | CA fingerprint is part of the enrollment URI; agent refuses to register if leader-served `ca.pem` doesn't hash to the pinned fingerprint |
| Replay of a leaked enrollment URI | Single-use token + short TTL (default 5 min when cluster listener is internet-reachable, 15 min otherwise) |
| Stolen agent cert reused after `serve nodes remove` | WS handshake re-checks the `nodes` row from the DB on every connection |
| Public-internet brute force of admin bearer key | Per-IP rate limit on `/admin/*`; failed-auth backoff with temporary IP bans |
| Public-internet abuse of `/admin/nodes/register` | Endpoint is on the cluster listener only; strict per-IP rate limit (10/min); audit log on every attempt |
| Enumeration of cluster CA via `/admin/ca.pem` | Cluster listener only; CA cert is public by design (pinning is what matters) |
| Public-internet exposure of cluster surfaces | Cluster surfaces (`/cluster/agent`, `/admin/nodes/register`, `/admin/ca.pem`) live on the cluster listener which can be bound to a private/VPN interface via `cluster_bind` |
| MITM of agent ↔ leader over public internet | Agent pins the cluster CA by fingerprint at registration; subsequent connections verify the cluster-server cert against the pinned CA |
| Header-spoofed fingerprint | Removed: the WS handler now inspects the real TLS peer cert |

Honest limits:

- **Root on the leader = cluster compromise.** Unchanged from today.
- **CA key not externally rooted.** Cluster traffic is verified against
  the operator's self-signed CA; an external CA breach doesn't affect us
  but neither does an external CA help us. Operator must protect
  `~/.serve/ca/`.
- **Public-internet exposure still requires firewall discipline.** The
  defaults aim to be safe-by-default, but operators should set
  `cluster_bind` to a private interface when feasible and use a
  firewall in front of both listeners.
- **The enrollment URI is plain text.** Treat it like a password.
- **No CRL / OCSP for revoked agent certs.** Revocation is by DB row
  removal; we depend on the per-connection DB check, not a CRL.

## Architecture

```text
                 +----------------------------------------------+
                 | serve leader (one process, three listeners)  |
                 |                                              |
external SDK     |  uvicorn PUBLIC                              |
clients   ---->  |    bind:  public_bind   (default 0.0.0.0)    |
                 |    port:  public_port   (default 11500)      |
                 |    cert:  operator-supplied (or cluster-CA)  |
                 |    routes:                                   |
                 |      /v1/*                bearer auth        |
                 |      /admin/keys/*        bearer auth        |
                 |      /admin/deployments/* bearer auth        |
                 |      /admin/nodes (GET)   bearer auth        |
                 |      /healthz, /metrics                      |
                 |    hardening:                                |
                 |      per-IP rate limit on /admin/*           |
                 |      failed-auth backoff (5/60s → 5m ban)    |
                 |      HSTS                                    |
                 |      optional admin IP allowlist             |
                 |                                              |
remote agents    |  uvicorn CLUSTER                             |
   ---->         |    bind:  cluster_bind  (default 0.0.0.0)    |
                 |    port:  cluster_port  (default 11501)      |
                 |    cert:  leader cluster-server (cluster CA) |
                 |    routes:                                   |
                 |      /cluster/agent  WS, mTLS required       |
                 |      /admin/nodes/register  token gate       |
                 |      /admin/ca.pem  unauth (fp-pinned)       |
                 |      /healthz                                |
                 |    hardening:                                |
                 |      strict rate limit on /register (10/min) |
                 |      audit log on every register attempt     |
                 |                                              |
local CLI        |  uvicorn UDS                                 |
   ---->         |    uds:   ~/.serve/sock                      |
                 |    full admin + openai surface, unauth       |
                 +----------------------------------------------+
```

### Address resolution

For each of `public` and `cluster`, the resolved address is the first
hit in this order:

1. Explicit flag on `serve daemon start` (`--public-host`, `--public-port`,
   `--cluster-host`, `--cluster-port`, `--public-bind`, `--cluster-bind`).
2. Env vars: `SERVE_PUBLIC_HOST`, `SERVE_PUBLIC_PORT`,
   `SERVE_CLUSTER_HOST`, etc.
3. `~/.serve/config.toml`.
4. Autodetect (host only): UDP-connect to `8.8.8.8:80`, read
   `getsockname()`. Fallback to `socket.gethostbyname(gethostname())`.
   Fallback to `127.0.0.1`.

`SERVE_LEADER_URL` continues to work — it now overrides the **advertised
cluster URL** specifically (i.e. what enrollment URIs contain). It does
not change bind addresses.

The **bind** address is always `0.0.0.0` for the public listener
(operator firewalls it). The cluster bind defaults `0.0.0.0` but
operators on a VPN should set `cluster_bind` to the VPN interface.

### Cert source for the public listener

Precedence:

1. `--public-cert <crt> --public-key <key>` CLI flags.
2. `SERVE_PUBLIC_CERT` / `SERVE_PUBLIC_KEY` env vars (paths).
3. `[public_tls] cert = "..." key = "..."` in `config.toml`.
4. Fall back to the cluster-CA-signed cluster-server cert and print a
   loud warning on startup. (Useful for single-machine demos; not safe
   for browser clients.)

If a public cert is configured, it is loaded as-is — we don't validate
its chain. If it fails to load, uvicorn errors out at startup with a
clear message; we do not silently downgrade.

## Components

### `cluster/ca.py`

Add:

```python
def generate_server_cert(
    ca: CA, *, hosts: list[str]
) -> CertBundle:
    """Mint a server cert signed by `ca` with SAN entries for `hosts`.
    Entries are auto-classified as DNS or IP. Includes ExtendedKeyUsage
    SERVER_AUTH. Validity = 5 years."""

def fingerprint_ca_pem(ca_cert_pem: bytes) -> str:
    """Compute the sha256:<hex> fingerprint we put in enrollment URIs.
    Hashes the PEM bytes exactly as served by /admin/ca.pem so that
    `openssl dgst -sha256` on the downloaded file produces a matching
    value. (We hash the PEM, not the DER, intentionally — see spec.)"""
```

A helper `write_cert_bundle(bundle, crt_path, key_path)` writes both
files with key mode `0o600`.

### `config.py`

```python
DEFAULT_CLUSTER_PORT = 11501  # new

@dataclass
class ResolvedConfig:
    public_host: str
    public_port: int
    public_bind: str
    cluster_host: str
    cluster_port: int
    cluster_bind: str
    public_cert_path: Path | None
    public_key_path: Path | None
    source: dict[str, str]  # field -> "flag"|"env"|"file"|"autodetect"|"default"

def resolve_config(cli_overrides: dict) -> ResolvedConfig: ...

def load_config_file() -> dict: ...   # reads ~/.serve/config.toml
def save_config_file(updates: dict) -> None: ...

def autodetect_outbound_ip() -> str | None: ...
```

`~/.serve/config.toml`:

```toml
[public]
host = "api.example.com"   # optional; advertised in /admin/nodes
port = 11500               # optional
bind = "0.0.0.0"           # optional

[public_tls]
cert = "/etc/letsencrypt/live/api.example.com/fullchain.pem"
key  = "/etc/letsencrypt/live/api.example.com/privkey.pem"

[cluster]
host = "cluster.example.com"  # advertised in enrollment URIs
port = 11501
bind = "10.0.0.1"             # bind cluster listener to VPN iface
```

All fields optional; missing fields fall through to env/autodetect/default.

### `daemon/__main__.py`

`serve` now starts three uvicorn servers in parallel:

```python
public_cfg  = uvicorn.Config(
    public_app,
    host=cfg.public_bind, port=cfg.public_port,
    ssl_keyfile=str(public_key), ssl_certfile=str(public_cert),
    log_level="info",
)
cluster_cfg = uvicorn.Config(
    cluster_app,
    host=cfg.cluster_bind, port=cfg.cluster_port,
    ssl_keyfile=str(cluster_key), ssl_certfile=str(cluster_cert),
    ssl_cert_reqs=ssl.CERT_OPTIONAL,
    log_level="info",
)
uds_cfg = uvicorn.Config(uds_app, uds=str(sock_path), log_level="info")

await asyncio.gather(
    uvicorn.Server(public_cfg).serve(),
    uvicorn.Server(cluster_cfg).serve(),
    uvicorn.Server(uds_cfg).serve(),
)
```

If a public cert is not configured, the cluster-server cert+key are
used for the public listener too, and a warning is logged.

### `daemon/app.py`

Now returns three apps: `(public_app, cluster_app, uds_app)`. Each one
mounts a subset of the routers:

- `public_app`: `/v1/*` (openai_router), `/admin/keys/*`,
  `/admin/deployments/*`, `/admin/nodes` (GET only), `/healthz`,
  `/metrics`. Adds HSTS middleware and the abuse-mitigation middleware.
- `cluster_app`: `/cluster/agent` (LeaderHub),
  `/admin/nodes/register`, `/admin/ca.pem`, `/healthz`.
- `uds_app`: full surface (unchanged from today).

All three share the same `LifecycleManager`, `AgentRegistry`,
`EnrollmentTokens`, DB connection, etc. — they're three views over one
state.

Splitting the admin routes is mechanical: `admin.py` already groups
them. We replace the single `admin_router` with two sub-routers
(`admin_public_router` and `admin_cluster_router`) and the unauthed
`/admin/nodes/register` and `/admin/ca.pem` move under the cluster app.

### `cluster/leader_hub.py`

Replace the header-trust default with TLS peer-cert inspection:

```python
def _peer_cert_fingerprint(ws: WebSocket) -> str | None:
    ssl_obj = ws.scope.get("extensions", {}).get(
        "websocket.tls", {}
    ).get("ssl_object")
    if ssl_obj is None:
        return None
    der = ssl_obj.getpeercert(binary_form=True)
    if not der:
        return None
    return "sha256:" + hashlib.sha256(der).hexdigest()
```

(The exact API for getting at the peer cert from uvicorn's websocket
varies by version; the implementation will pin it down.)

If no peer cert is presented, the handler closes with `1008`.

The fingerprint lookup queries `nodes_store.find_by_fingerprint(conn, fp)`
on **every connection** — no in-process cache. That guarantees
`serve nodes remove` takes effect on the very next agent attempt.

The header-based path is kept as an optional injected resolver for
test purposes only.

### `daemon/admin.py`

- Add `unauthed_router.get("/admin/ca.pem")` returning the CA PEM with
  header `X-Serve-CA-Fingerprint`.
- Add `_ratelimit(request, key, limit, window_s)` helper. Apply to
  `/admin/nodes/register` (limit=10/minute/IP). Apply per-IP failed-auth
  backoff to admin auth dependency: 5 failures in 60s → 5-minute
  per-IP ban, return 429 with `Retry-After`.
- Audit log line on every `/admin/nodes/register` attempt (success +
  failure, source IP, token prefix only — never the full token).

### `cli/daemon_cmd.py`

Resolution + cert provisioning + honest output:

```text
$ serve daemon start
daemon started (pid 143197)

public  : https://api.example.com:11500    (cert: api.example.com)
cluster : https://cluster.example.com:11501  (cert: serve cluster CA)
            ca fingerprint: sha256:7f3a4d…

cluster listener on 0.0.0.0:11501 — internet-reachable
  consider setting [cluster] bind = "10.0.0.1" to restrict to your VPN
```

The fallback case:

```text
public  : https://192.168.0.164:11500    ⚠  using cluster-CA cert
            external clients must trust sha256:7f3a4d… or supply
            [public_tls] in ~/.serve/config.toml
cluster : https://192.168.0.164:11501    (cert: serve cluster CA)
            ca fingerprint: sha256:7f3a4d…
```

### `cli/config_cmd.py` (new)

```text
serve config show
serve config set-public host=api.example.com port=11500
serve config set-cluster host=cluster.example.com bind=10.0.0.1
serve config set-public-tls cert=/path/to/fullchain.pem key=/path/to/privkey.pem
```

`serve config show` displays the resolved values and their source:

```text
public.host       : api.example.com           (file)
public.port       : 11500                     (default)
public.bind       : 0.0.0.0                   (default)
public_tls.cert   : /etc/le/.../fullchain.pem (file)
public_tls.key    : /etc/le/.../privkey.pem   (file)
cluster.host      : cluster.example.com       (file)
cluster.port      : 11501                     (default)
cluster.bind      : 10.0.0.1                  (file)
```

### `cli/nodes_cmd.py`

`serve nodes enroll <label>` output:

```text
Enrollment URI (single-use, expires in 5 min):

  serve://enroll?leader=https%3A%2F%2Fcluster.example.com%3A11501&token=abc123&ca_fp=sha256%3A7f3a…

On the agent host, run:

  serve agent register --uri 'serve://enroll?leader=…&token=…&ca_fp=…'
```

The URI scheme: `serve://enroll?` with `leader`, `token`, `ca_fp` (all
URL-encoded).

### `cli/agent_cmd.py`

Add `--uri` flag, mutually exclusive with `--leader/--token`. Flow:

1. Parse URI.
2. `GET <leader>/admin/ca.pem` with TLS verification *disabled for this
   one request*, then verify the fetched body hashes to the pinned
   fingerprint. Abort with a loud error if not.
3. Write CA to `~/.serve/ca.crt`. All subsequent HTTPS uses it.
4. `POST <leader>/admin/nodes/register` with token — existing flow.
5. Write `agent.crt`, `agent.key`, `agent.yaml`.

The bare `--leader/--token` flags stay for scripted setups.

## State

No new tables. Existing schema is sufficient.

Filesystem layout grows:

```text
~/.serve/
├── ca/
│   ├── ca.crt           (existed)
│   └── ca.key           (existed)
├── leader/              (new)
│   ├── server.crt       cluster-CA-signed, SAN bound
│   └── server.key
├── config.toml          (new, optional)
├── daemon.pid           (existed)
└── ...
```

## Backward compatibility

- Existing daemons running plain HTTP on `127.0.0.1:11500` will, after
  upgrade, listen on TLS on `0.0.0.0:11500` (public) and `0.0.0.0:11501`
  (cluster). Local CLI is unaffected (uses UDS). External HTTP clients
  pointed at the daemon must switch to `https://` and either trust the
  cluster CA or supply `[public_tls]` so the daemon serves a
  publicly-trusted cert.
- Existing enrolled agents have `agent.yaml` with `leader_url:
  http://...` — they need to re-register (their old URL is wrong now).
  We'll print upgrade notes; not solved automatically.
- `SERVE_LEADER_URL` continues to work as an override of the advertised
  cluster URL.

## Testing

- **Unit:** server-cert SAN generation, fingerprint computation,
  config-resolution precedence, URI parse/round-trip, rate limiter,
  failed-auth backoff.
- **Integration:**
  - End-to-end enrollment via URI on loopback (two `serve` processes).
  - Wrong-fingerprint URI rejected with a clear error.
  - `serve nodes remove` followed by an agent reconnect → reconnect
    rejected at the WS handshake.
  - Daemon restart with a different cluster `host` → cluster-server
    cert regenerated; old agents reconnect cleanly with the same CA.
  - Public listener with operator-supplied cert and cluster listener
    with cluster-CA cert both serving correctly.

## Build sequence

1. `cluster/ca.py` — `generate_server_cert`, `fingerprint_ca_pem`,
   `write_cert_bundle`.
2. `config.py` — `ResolvedConfig`, `resolve_config`, `load_config_file`,
   `save_config_file`, `autodetect_outbound_ip`.
3. `daemon/app.py` — split into `build_apps()` returning
   `(public_app, cluster_app, uds_app)`.
4. `daemon/__main__.py` — three uvicorn servers; cert provisioning;
   cluster-CA fallback for public.
5. `daemon/admin.py` — split admin router; add `/admin/ca.pem`; add
   rate-limit helper and apply.
6. `cluster/leader_hub.py` — peer-cert verification + DB lookup.
7. `cli/daemon_cmd.py` — resolution, cert refresh, honest output.
8. `cli/config_cmd.py` — new subcommand.
9. `cli/nodes_cmd.py` — emit enrollment URI.
10. `cli/agent_cmd.py` — `--uri` parsing and CA fingerprint pin.
11. Tests in lockstep.
12. README update.

## Open calls made by author

- **Three listeners, not SNI-based cert selection.** uvicorn has no
  native SNI cert-selection hook; a custom TLS frontend is too much.
- **Cluster default port 11501.** Avoids collision with any existing
  reverse-proxy config that already terminates on 11500.
- **Hash the full CA PEM for the fingerprint.** Matches `openssl dgst
  -sha256` on the downloaded `ca.pem` so operators can verify by hand.
- **Cluster-CA fallback for the public listener with a loud warning.**
  Makes the dev/demo path one command. Production operators see a clear
  signal to fix it.
- **`CERT_OPTIONAL` on the cluster listener** with per-route mTLS
  enforcement. Lets `/admin/ca.pem` and `/admin/nodes/register` skip
  mTLS while `/cluster/agent` requires it.
- **`/admin/nodes` GET stays on the public listener** because it's how
  the operator's own admin UI/scripts read fleet state via Bearer auth.
  Only the unauthenticated `/admin/nodes/register` and `/admin/ca.pem`
  move to the cluster listener.
