# Multi-Node serve-engine

This page is the operator's guide to running a leader plus one or more
agent hosts. The architectural rationale is split across two specs:

- `docs/superpowers/specs/2026-05-18-multi-node-serving-design.md` —
  the cluster fabric (mTLS WebSocket, enrollment, registry, routing).
- `docs/superpowers/specs/2026-05-18-secure-by-default-cluster-transport-design.md` —
  the two-listener TLS design covered on this page.

Here we stick to setup, verification, and troubleshooting.

> **Preview status.** The cluster fabric (TLS, enrollment, mTLS
> WebSocket, registry, heartbeat) is in place and tested. The
> lifecycle manager does **not yet** dispatch deployment starts
> through the AgentLink, so `serve run <model>` still spawns the
> engine container on the leader host even when agents are connected.
> See **Status** below before planning a production rollout.

## Concepts

- **Leader.** The existing `serve daemon`. Owns SQLite state, the
  OpenAI-compatible API, the admin API, the router, and the lifecycle
  manager. There is exactly one leader.
- **Agent.** A thin daemon (`serve agent start`) that runs on each
  additional GPU host. It dials home to the leader over an mTLS
  WebSocket and exposes the host's local Docker through that channel.
- **Public listener.** The leader's external-facing HTTPS port
  (default `:11500`). Serves `/v1/*`, `/admin/*` (bearer-auth),
  `/healthz`, `/metrics`. Operator supplies a publicly-trusted cert
  for browser/SDK clients; falls back to a self-signed cluster-CA
  cert in dev.
- **Cluster listener.** A second HTTPS port (default `:11501`) with
  mTLS and its own cert chain. Serves `/cluster/agent` (WS),
  `/admin/nodes/register` (token-gated), `/admin/ca.pem`
  (fingerprint-pinned). This is the only port agents ever touch.
- **Cluster CA.** A self-signed root that lives only on the leader at
  `~/.serve/ca/`. Used to mint both the leader's cluster-listener
  server cert and each agent's mTLS client cert. Agents pin its
  SHA-256 fingerprint at enrollment.
- **Node.** A row in the leader's `nodes` table. The leader's own host
  is always present as `label=local`; each enrolled agent gets one
  more row.

## Install

Both the leader and each agent install the same package. There is no
separate "agent" build — the role is chosen at runtime.

```bash
git clone https://github.com/Mapika/serve-engine
cd serve-engine
uv tool install --editable .
serve doctor
```

Linux + NVIDIA + Docker 24+ requirements apply to every agent host.
The leader host can have no GPU at all if you intend to use it purely
as a control plane (today's lifecycle manager assumes a local GPU so
this is most useful once the manager refactor lands; flagged in
**Status**).

## Quick start (LAN, single afternoon)

The leader autodetects its LAN IP and serves TLS on both ports
directly from uvicorn — no reverse proxy.

### Leader

```bash
serve daemon start
```

The startup banner prints both URLs and the CA fingerprint:

```
daemon started (pid …)

public  : https://192.168.0.164:11500    ⚠  using cluster-CA cert
            external clients must trust sha256:7f3a… or set [public_tls]
cluster : https://192.168.0.164:11501  (cert: serve cluster CA)
            ca fingerprint: sha256:7f3a…
```

The "using cluster-CA cert" warning is expected on a LAN — you don't
need a publicly-trusted cert for traffic that never leaves your
network. Browser/SDK clients hitting the public URL will have to trust
the cluster CA (or you can ignore the warning if you only call from
the loopback / local CLI / a trusting `httpx` client).

Mint an enrollment URI for the new GPU host:

```bash
$ serve nodes enroll gpu-rig-2
Enrollment URI (single-use, expires in 10 min):

  serve://enroll?leader=https%3A%2F%2F192.168.0.164%3A11501&token=…&ca_fp=sha256%3A7f3a…

On the agent host, run:
  serve agent register --uri 'serve://enroll?leader=…&token=…&ca_fp=…'
```

Copy the entire `serve agent register --uri '…'` line.

### Agent

On the new GPU host:

```bash
# Same install as the leader.
git clone https://github.com/Mapika/serve-engine
cd serve-engine
uv tool install --editable .

# Paste the line you copied.
serve agent register --uri 'serve://enroll?leader=…&token=…&ca_fp=…'
serve agent start
```

What `--uri` does under the hood:

1. Parses leader URL + token + CA fingerprint out of the URI.
2. Fetches `https://<leader>/admin/ca.pem` with TLS verification
   disabled for this one request.
3. Verifies `sha256(downloaded ca.pem)` equals the pinned fingerprint.
   This closes the standard MITM-during-CA-bootstrap hole — an
   attacker who substitutes their own CA fails this check.
4. Writes the verified CA to `~/.serve/ca.crt`.
5. POSTs the token to `/admin/nodes/register`, gets back a per-host
   mTLS client cert, writes `agent.crt`, `agent.key`, and
   `agent.yaml`.

After that, `serve agent start` runs the agent loop and connects to
`wss://<cluster URL>/cluster/agent` with mTLS.

### Verify (from the leader)

```bash
serve nodes ls            # new node should appear `ready`
serve nodes show <id>     # GPU inventory, agent_version, last heartbeat
```

## Configuring the leader

Resolution order for every address-shaped setting (flag wins, then env,
then file, then autodetect, then literal default):

1. `--public-host` / `--public-port` / `--public-bind` /
   `--cluster-host` / `--cluster-port` / `--cluster-bind` on
   `serve daemon start`.
2. `SERVE_PUBLIC_HOST`, `SERVE_PUBLIC_PORT`, `SERVE_PUBLIC_BIND`,
   `SERVE_CLUSTER_HOST`, `SERVE_CLUSTER_PORT`, `SERVE_CLUSTER_BIND`
   env vars.
3. `~/.serve/config.toml`.
4. Autodetect (UDP-connect trick + `gethostbyname`) for the host;
   `0.0.0.0` for the bind; `11500` / `11501` for ports.

`SERVE_LEADER_URL` continues to work as an explicit override of the
**advertised** cluster URL only (i.e. what enrollment URIs contain).
It does not change bind addresses.

Inspect:

```bash
serve config show
```

```
public.host          api.example.com                   (file)
public.port          11500                             (default)
public.bind          0.0.0.0                           (default)
public_tls.cert      /etc/le/.../fullchain.pem         (file)
public_tls.key       /etc/le/.../privkey.pem           (file)
cluster.host         cluster.example.com               (file)
cluster.port         11501                             (default)
cluster.bind         10.0.0.1                          (file)

resolved public_url : https://api.example.com:11500
resolved cluster_url: https://cluster.example.com:11501
```

Edit:

```bash
serve config set-public host=api.example.com port=11500
serve config set-cluster host=cluster.example.com bind=10.0.0.1
serve config set-public-tls cert=/etc/le/.../fullchain.pem \
                            key=/etc/le/.../privkey.pem
```

The config file is `~/.serve/config.toml`:

```toml
[public]
host = "api.example.com"
port = 11500
bind = "0.0.0.0"

[public_tls]
cert = "/etc/letsencrypt/live/api.example.com/fullchain.pem"
key  = "/etc/letsencrypt/live/api.example.com/privkey.pem"

[cluster]
host = "cluster.example.com"
port = 11501
bind = "10.0.0.1"   # bind cluster listener to a VPN iface
```

All sections optional; missing keys fall through to env / autodetect.

## Cross-network setup (different networks, VPN, NAT)

The tunnel-by-default design was built for this case. **The agent only
needs outbound reach to the leader's cluster port.** No port forwarding
on the agent side, no NAT punching, no VPN required.

Requirements:

- The leader has a stable, routable address from the agent's network
  (public DNS + public IP, a Tailscale / WireGuard peer, anything).
- The leader's cluster port is reachable from the agent.
- The agent's firewall allows outbound HTTPS to the cluster port.

Topology:

```
[ public internet ]
                  |
                  v
       cluster.example.com:11501      (serve daemon, cluster listener)
                  |  (TLS + mTLS, terminated by uvicorn directly)
                  |
                  +-- agents dial in from anywhere

       api.example.com:11500          (serve daemon, public listener)
                  |  (TLS, terminated by uvicorn directly)
                  |
                  +-- SDK clients call /v1/*
```

Flow is identical to the LAN setup. The enrollment URI carries the
cluster URL, so the agent ends up dialing the right port automatically:

```bash
# On the leader:
serve nodes enroll home-rig

# On the agent (anywhere with outbound HTTPS to the cluster port):
serve agent register --uri 'serve://enroll?…'
serve agent start
```

### Latency / throughput honesty

Every prompt byte and every generated token traverses the leader↔agent
WebSocket. For one LLM streaming 30 tok/s at ~80 B/token that's
~2.4 KB/s per request — negligible. At 50 concurrent streams it's a
few hundred KB/s through one persistent socket on the leader process.
The leader's uplink is the bottleneck. Fine for moderate scale; not
what you want for serving hundreds of concurrent users from a
residential leader. The direct-LAN mode (agent advertises a reachable
address; leader probes; uses direct path when available) is on the
follow-up list precisely to avoid this hop when both ends actually can
reach each other.

## Public-internet exposure

The defaults (`0.0.0.0` bind on both listeners) work on the open
internet, but you should harden:

### Public listener cert

External SDK clients won't trust the self-signed cluster CA. Supply a
publicly-trusted cert via `[public_tls]` in `config.toml` or
`--public-cert` / `--public-key` flags. Standard sources: Let's Encrypt
(`certbot --standalone --preferred-challenges http -d api.example.com`,
or a DNS-01 challenge for behind-NAT), an internal corp CA, or a
managed cert from your cloud provider.

If you don't, the daemon prints a loud warning on startup and serves
the cluster-CA cert on the public listener. Fine for demos and
internal use, not fine for browser clients.

### Cluster listener bind

If your leader has a VPN / private interface, bind the cluster
listener to it:

```bash
serve config set-cluster bind=10.0.0.1
```

Now only hosts on the VPN can reach `/cluster/agent`. Agents on the
public internet (e.g., remote GPU box, no VPN) will need the public
interface — in that case leave `bind = 0.0.0.0` but firewall the
cluster port to known agent source IPs.

### Built-in defenses

- `/admin/nodes/register` is rate-limited: 10 attempts per source IP
  per minute. Returns 429 + `Retry-After` on overflow.
- Every register attempt (success and failure) is logged via the
  `serve_engine.audit` logger with source IP and token prefix.
- Enrollment tokens are single-use and expire after 10 minutes.
- `serve nodes remove <id>` causes the next WS handshake from that
  agent to be rejected — the cert fingerprint is re-checked against
  the DB on every connection, not cached in-process.

### What this design does *not* protect against

- **Root on the leader.** Anyone with root can mint agent certs from
  `~/.serve/ca/ca.key`. Protect that file.
- **Stolen public-cert key.** If your `[public_tls]` key leaks, an
  attacker can MITM your public listener. Standard cert-management
  hygiene applies; this design doesn't help or hurt.
- **DDoS / volumetric abuse.** The fixed-window rate limit slows
  brute force but is not a DDoS defense. Put a CDN / WAF in front of
  the public listener if you expect hostile traffic.
- **Compromised agent host.** mTLS authenticates a cert, not a human;
  whoever controls an enrolled agent host can stream traffic into
  your fleet until you `serve nodes remove` them.

## Legacy: reverse-proxy mode

The pre-secure-by-default deployment style (reverse proxy in front of
plain-HTTP uvicorn, forwarding `x-serve-client-fingerprint`) is still
supported as an opt-in for operators who already run TLS termination
upstream:

```bash
SERVE_TRUST_FORWARDED_FP=1 serve daemon start
```

With that flag set, `LeaderHub` will accept the proxy-forwarded
fingerprint header when no TLS peer cert is present. **Do not set
this on an internet-exposed leader without an upstream proxy doing
real mTLS verification** — the header is unauthenticated by itself.

## Status — what works today

What you can rely on:

- Two-listener TLS termination directly inside uvicorn — no reverse
  proxy required for either `/v1/*` or `/cluster/agent`.
- Enrollment via single-paste URI with CA-fingerprint pin.
- Single-use token consumption, rate-limited registration, audit log.
- mTLS WebSocket connection with real TLS peer-cert verification on
  every connection (DB lookup, no in-process fingerprint cache).
- `serve nodes ls / show / enroll / remove` and `serve agent
  register / start / status`.
- `serve config show / set-public / set-cluster / set-public-tls`.
- Heartbeat-based health watcher (`ready` → `unreachable` after 15 s
  of silence; auto-recovers on reconnect).
- Tunneled `/v1/*` data plane through `RemoteAgentLink.proxy_request`
  (exercised in `tests/integration/test_remote_agent_roundtrip.py`).

What is **not yet wired** in this release:

- `serve run <model>` always spawns the engine container on the
  leader host. The manager has the `agent_registry` and dispatch
  helpers but its start path still calls `self._docker.run(...)`
  directly.
- The `/v1/*` route handler still uses today's direct httpx call,
  not `_proxy_via_link`.
- Direct-LAN data-plane mode (advertised `reachable_as` + leader
  probing).
- ACME / Let's Encrypt auto-issuance for the public-listener cert
  (bring your own cert today).
- UI surface for nodes (no Nodes page yet; chips on deployments not
  added).
- `node_label` affinity on service profiles.
- Replicas of the same service profile across nodes.

In other words: the fabric and the transport are real, but workloads
still land on the leader. The next iteration plan (separate doc)
addresses the manager and proxy routing changes that make remote-agent
deployments actually serve traffic.

## Troubleshooting

**`serve agent register --uri` fails with "CA fingerprint mismatch".**

Either the URI was tampered with in transit, or you're talking to a
different leader than the one that minted it (e.g., the leader's CA
was regenerated by deleting `~/.serve/ca/`). Re-mint:

```bash
serve nodes enroll <label>
```

**`serve agent start` connects but the WS handshake closes immediately
with 1008.**

The leader rejected the peer cert. Possible causes:

- The agent's `agent.yaml` points at the wrong leader / port (it now
  defaults to `:11501` for cluster, not `:11500`). Re-register.
- The leader's CA was rotated since enrollment. Re-register.
- The node was removed from the DB. Check `serve nodes ls` on the
  leader.
- Logs on the leader (`~/.serve/logs/daemon.log`) should show
  `cluster ws reject:` with the cause.

**`serve nodes ls` shows the node as `unreachable` even though
`serve agent start` is running.**

The health watcher flips to `unreachable` after 15 s without a
heartbeat. If the agent is alive but the row is stale, the WS isn't
connected — check the agent logs for reconnect attempts.

**`serve agent register` returns 403 `invalid or expired enrollment
token`.**

Tokens are single-use and expire after 10 minutes. Re-run `serve nodes
enroll <label>` on the leader to mint a fresh URI.

**`serve daemon start` fails with `port already in use` on 11501.**

Either another daemon is running (`serve daemon status`) or another
process holds the port. Pick a different cluster port:

```bash
serve config set-cluster port=21501
serve daemon start
```

Existing agents will still try the old port — re-enroll them.

**Re-enrolling an existing agent.**

`serve nodes enroll <existing-label>` is allowed and rotates the cert
fingerprint on the next register. The agent will need a fresh
`serve agent register --uri …` run.

## Decommissioning a node

On the leader:

```bash
serve nodes remove <id>
```

This unregisters any live AgentLink, deletes the row, and the cert
fingerprint stops authenticating new connections (the DB lookup
happens on every WS handshake, so revocation is immediate). The agent
process keeps trying to reconnect; stop it on the agent host
(`Ctrl-C` or your service manager). The local node (`label=local`)
cannot be removed.

## Tuning

Most defaults are sensible for a small fleet. If you need to change
them:

- **Heartbeat interval.** Agent sends every 5 s; leader marks a node
  `unreachable` after 15 s of silence. Both live in
  `src/serve_engine/cluster/agent_client.py` (heartbeat task) and
  `src/serve_engine/cluster/health_watcher.py` (`stale_after_s`).
  Hard-coded for v1; lift to config when you actually need to tune.
- **Reconnect backoff.** Agent starts at 1 s, doubles to 30 s cap.
  See `run_agent()` in `agent_client.py`.
- **Enrollment token TTL.** Default 600 s. See `EnrollmentTokens` in
  `src/serve_engine/cluster/enrollment.py`.
- **Registration rate limit.** Default 10 attempts / IP / 60 s.
  See `_rate_limit` in `src/serve_engine/daemon/admin.py`.
- **Server-cert validity.** 5 years. Regenerated automatically when
  `public_host` / `cluster_host` change and the existing SAN doesn't
  cover the new value. See `ensure_server_cert` in
  `src/serve_engine/cluster/ca.py`.

## Observability

Each agent samples GPU + per-deployment in-flight + recent latency on
every heartbeat tick (5 s by default) and ships the snapshot on the
existing WebSocket. The leader's `MetricsAggregator` keeps a 60 s
rolling window per node and exposes the data three ways:

### Prometheus

`GET /metrics` (on the public listener) appends the cluster series
after the existing daemon and engine sections. Stable label sets, safe
to scrape with the default 30 s interval.

| Series | Type | Labels |
| --- | --- | --- |
| `serve_node_gpu_util_pct` | gauge | `node`, `gpu` |
| `serve_node_gpu_mem_used_bytes` | gauge | `node`, `gpu` |
| `serve_deployment_in_flight` | gauge | `node`, `deployment`, `model` |
| `serve_deployment_requests_total` | counter | `node`, `deployment`, `model` |
| `serve_deployment_latency_p50_ms` | gauge | `node`, `deployment`, `model` |
| `serve_deployment_latency_p95_ms` | gauge | `node`, `deployment`, `model` |
| `serve_deployment_errors_total` | counter | `node`, `deployment`, `model` |

The leader's own node also appears in these series — a small background
task on the daemon mirrors the agent heartbeat for the local node so
operators get one consistent surface regardless of whether a metric is
local or remote.

### Sample Grafana dashboard

`docs/dashboards/serve-engine.json` ships a six-panel starter:
GPU utilization, GPU memory, in-flight, p95 latency, request rate,
error rate. Import it in Grafana's "Import dashboard" page; no auto-
provisioning. Tune the panel queries for your label conventions.

### Admin snapshot endpoint

`GET /admin/metrics/snapshot` returns the latest per-node sample plus
short sparkline series. The UI Cluster page consumes it on a 5 s poll
and renders inline SVG sparklines on each node card.

### Known limitations (today)

- The leader's in-flight counter increments for every request the
  proxy dispatches, including those routed to remote nodes. The local
  sample therefore over-counts when remote deploys are active. Per-node
  attribution is part of the smart-routing follow-up.
- Remote agents currently report empty deployment lists — instrumenting
  the agent's `_run_http_stream` dispatch path to populate in-flight +
  latency is a small follow-up.

## Routing & resilience

Multi-deployment selection is driven by a load-aware scorer
(`src/serve_engine/routing/scorer.py`). For a given (base, adapter)
target, the proxy:

1. Collects every ready deployment as a candidate.
2. For adapter requests, applies the existing tier filter
   (already-loaded > free-slot > needs-evict). Only the best tier is
   scored — we don't mix tiers, because that would hide cold-load
   latency behind a fast idle node.
3. Calls the scorer with per-node `NodeSignals` derived from the
   metrics aggregator (mem free, in-flight, p95 latency).

### Default scorer

Hard memory filter then lexicographic rank by:

1. `affinity_hit` (1 if the leader's affinity map points at this node).
2. `-in_flight` (less loaded first).
3. `-p95_latency_ms` (faster recent performance first).

Explicit tiers rather than weighted floats so it's debuggable. A node
with no aggregator entry yet (just-enrolled) is kept as a candidate but
ranked last — it can't be filtered for memory without evidence, and
preferring a known-fit known-fast node is the right bias.

### Affinity

`RoutingAffinity` is a bounded LRU `affinity_key → node_id` map. The
key precedence is `X-Session-Id` > `X-Conversation-Id` > the API key.
On every successful dispatch the proxy records the chosen node;
subsequent requests with the same key get a `affinity_hit=1` boost,
which keeps a conversation pinned to whichever node holds its KV
cache. Best-effort: lost on process restart, cleared per-node when a
node transitions to `unreachable`.

### Node-loss audit

When the health watcher demotes a node to `unreachable`, it emits a
`node_loss_audit` warning with the node id and label, and clears the
affinity entries pointing at that node. Routing decisions thereafter
treat the node as gone until it re-handshakes.

### Pending follow-ups

- **Pre-first-byte retry across distinct nodes.** The `dispatch_with_retry`
  helper (`src/serve_engine/daemon/retry_dispatcher.py`) and the
  pre-first-byte classifier (`src/serve_engine/daemon/dispatch_errors.py`)
  are landed and unit-tested. Wiring them into the proxy's dispatch
  site requires carving the existing remote/local branching out of
  `openai_proxy.py` into a dedicated `daemon/dispatch.py` — a focused
  refactor scheduled as a separate change.
- **SSE backpressure.** A bounded `asyncio.Queue` between the engine
  reader and client writer so a slow consumer pauses generation. Same
  proxy file; lands with the dispatch carve.
- **Mid-stream failover** is explicitly out of scope. A node dying
  mid-generation propagates the error; KV-cache transfer is not on the
  roadmap.

## Roadmap

Tracked in the design docs, in priority order:

1. **Manager-via-AgentLink.** Route `start_deployment` through the
   chosen node's link; move `wait_healthy` and image digest collection
   behind `AgentLink`. After this, remote-agent deployments serve
   traffic end-to-end.
2. **Proxy via `_proxy_via_link`.** Thread the helper into the `/v1/*`
   route handler so the data plane goes through `AgentLink` instead
   of direct httpx.
3. **Direct-LAN ingress.** Agent opens a guarded mTLS ingress on its
   `reachable_as` address; leader probes and switches the data plane
   to direct routing when reachable. Falls back to tunnel
   automatically.
4. **ACME for the public listener.** Auto-issue and auto-renew via
   Let's Encrypt so a single-command public deploy works without
   external certbot orchestration.
5. **UI.** Nodes page, node chip on deployment cards, profile
   `node_label` editor.
6. **Service-profile `node_label` affinity.**
7. **Replica fan-out across nodes.**
8. **Reconnect reconciliation polish** (orphan-container kill, drift
   correction).
