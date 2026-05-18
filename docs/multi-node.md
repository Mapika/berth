# Multi-Node serve-engine

This page is the operator's guide to running a leader plus one or more
agent hosts. The architectural rationale is in
`docs/superpowers/specs/2026-05-18-multi-node-serving-design.md`; here we
stick to setup, verification, and troubleshooting.

> **Preview status.** The cluster fabric (enrollment, mTLS WebSocket,
> registry, heartbeat) is in place and tested. The lifecycle manager
> does **not yet** dispatch deployment starts through the AgentLink, so
> `serve run <model>` still spawns the engine container on the leader
> host even when agents are connected. See the **Status** section below
> before planning a production rollout.

## Concepts

- **Leader.** The existing `serve daemon`. Owns SQLite state, the
  OpenAI-compatible API, the admin API, the router, and the lifecycle
  manager. There is exactly one leader.
- **Agent.** A thin daemon (`serve agent start`) that runs on each
  additional GPU host. It dials home to the leader over an mTLS
  WebSocket and exposes the host's local Docker through that channel.
- **Node.** A row in the leader's `nodes` table. The leader's own host
  is always present as `label=local`; each enrolled agent gets one more
  row.
- **AgentLink.** The protocol the leader uses to talk to a node, with
  two implementations: `LocalAgentLink` (in-process, wraps the local
  Docker client) and `RemoteAgentLink` (WS-backed, multiplexes lifecycle
  RPCs and per-request `/v1/*` proxy streams over one socket).

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

## Same-network setup (LAN)

Even on a trusted LAN, the leader expects a reverse proxy to terminate
TLS and forward the agent's verified client-cert fingerprint as the
`x-serve-client-fingerprint` header. Direct uvicorn TLS termination is
a separate follow-up. This is the v1 simplification — plan accordingly.

Minimum LAN topology:

```
[ leader host ]
    serve daemon  ->  127.0.0.1:11500
    caddy / nginx ->  10.0.0.1:11501 (tls, mtls verify)

[ agent host ]
    serve agent start  ->  wss://10.0.0.1:11501/cluster/agent
```

### Leader

```bash
serve daemon start
# Reverse proxy in front of 11500 — see "Reverse proxy" below.
serve nodes enroll gpu-rig-2
```

`serve nodes enroll` prints a single-use token and the exact
`serve agent register …` command for the agent to run.

### Agent

On the new GPU host:

```bash
serve agent register \
    --leader https://leader.lan:11501 \
    --token <pasted>
serve agent start
```

`serve agent register` writes `~/.serve/agent.crt`, `~/.serve/agent.key`,
`~/.serve/ca.crt`, and `~/.serve/agent.yaml`. `serve agent start` runs
the daemon in the foreground; in production wrap it with systemd or
tmux.

### Verify (from the leader)

```bash
serve nodes ls            # the new node should appear `ready`
serve nodes show <id>     # GPU inventory, agent_version, last heartbeat
```

## Cross-network setup (different networks, VPN, NAT)

The tunnel-by-default design was built for this case. **The agent only
needs outbound reach to the leader.** No port forwarding on the agent
side, no NAT punching, no VPN required.

Requirements:

- The leader has a stable, routable address from the agent's network.
  Public DNS + public IP, a Tailscale / WireGuard peer, anything.
- A reverse proxy in front of the leader terminating TLS (Let's
  Encrypt for public; internal CA if you prefer). It must verify the
  agent's client cert against the leader's CA and forward the
  fingerprint as `x-serve-client-fingerprint`.
- The agent's firewall allows outbound HTTPS to the leader.

Topology:

```
[ public internet ]
                |
                v
        serve.example.com:443  (Caddy / nginx)
                |  (TLS + mTLS verify, forwards fingerprint header)
                v
            127.0.0.1:11500   (serve daemon)

[ agent host, behind residential NAT ]
        serve agent start  ->  wss://serve.example.com/cluster/agent
```

The flow is identical to the LAN setup:

```bash
# On the leader:
serve nodes enroll home-rig

# On the agent (anywhere with outbound HTTPS):
serve agent register --leader https://serve.example.com --token <pasted>
serve agent start
```

### Latency / throughput honesty

Every prompt byte and every generated token traverses the
leader↔agent WebSocket. For one LLM streaming 30 tok/s at ~80 B/token
that's ~2.4 KB/s per request — negligible. At 50 concurrent streams
it's a few hundred KB/s through one persistent socket on the leader
process. The leader's uplink is the bottleneck. Fine for moderate
scale; not what you want for serving hundreds of concurrent users
from a residential leader. The direct-LAN mode (agent advertises a
reachable address; leader probes; uses direct path when available)
is on the follow-up list precisely to avoid this hop when both ends
actually can reach each other.

## Reverse proxy

The leader trusts whatever the proxy in front of it forwards in
`x-serve-client-fingerprint`. Configure the proxy carefully:

1. Terminate TLS.
2. Require + verify client certs against the leader's CA at
   `~/.serve/ca/ca.crt`.
3. Compute the SHA-256 of the verified client cert (DER) and forward
   it as `x-serve-client-fingerprint: sha256:<lowercase hex>`.
4. Drop the header on any path other than `/cluster/agent` so an
   attacker can't spoof it on a different endpoint.

### Caddy example

```caddyfile
serve.example.com {
    tls /etc/caddy/server.crt /etc/caddy/server.key {
        client_auth {
            mode require_and_verify
            trusted_ca_cert_file /home/serve/.serve/ca/ca.crt
        }
    }

    @cluster path /cluster/*
    handle @cluster {
        request_header x-serve-client-fingerprint "sha256:{tls_client_fingerprint}"
        reverse_proxy 127.0.0.1:11500
    }

    handle {
        # Strip fingerprint on non-cluster paths so nobody can spoof it.
        request_header -x-serve-client-fingerprint
        reverse_proxy 127.0.0.1:11500
    }
}
```

### nginx example

nginx exposes `$ssl_client_fingerprint` as SHA-1 by default; SHA-256
requires nginx ≥ 1.11.6 with `$ssl_client_fingerprint_sha256`, or
computing it externally. Recent OpenResty / nginx-plus builds expose
`$ssl_client_s_dn` and a SHA-256 variant. Use what your build supports
and confirm the value matches `serve nodes show <id>`'s `fingerprint`
field after one connection attempt.

## Status — what works today

What you can rely on:

- Enrollment, single-use token consumption, cert issuance.
- mTLS WebSocket connection, Hello/Welcome handshake, fingerprint
  pinning, agent → leader heartbeat.
- `serve nodes ls / show / enroll / remove` and `serve agent
  register / start / status`.
- Heartbeat-based health watcher (`ready` → `unreachable` after 15s of
  silence; auto-recovers on reconnect).
- The `_proxy_via_link` helper and `RemoteAgentLink.proxy_request`
  fully exercise the tunneled data plane in tests
  (`tests/integration/test_remote_agent_roundtrip.py`).

What is **not yet wired** in this release:

- `serve run <model>` always spawns the engine container on the leader
  host. The manager has the `agent_registry` and the dispatch helpers
  but its start path still calls `self._docker.run(...)` directly.
- The `/v1/*` route handler still uses today's direct httpx call, not
  `_proxy_via_link`.
- mTLS termination directly inside uvicorn — today you need a reverse
  proxy.
- Direct-LAN mode (advertised `reachable_as` + leader probing).
- UI surface for nodes (no Nodes page yet; chips on deployments not
  added).
- `node_label` affinity on service profiles.
- Replicas of the same service profile across nodes.

In other words: the fabric is real, but workloads still land on the
leader. The next iteration plan (separate doc) addresses the manager
and proxy routing changes that make remote-agent deployments actually
serve traffic.

## Troubleshooting

**`serve agent start` reconnects forever, never sees `Welcome`.**

The agent reports the underlying error in its logs. Common cases:

- *TLS handshake fails* → the leader has no reverse proxy terminating
  TLS, or the proxy doesn't trust the agent's CA. Check the proxy's
  `client_auth` config and that `~/.serve/ca/ca.crt` on the agent is
  byte-identical to the one the proxy trusts.
- *HTTP 403 / 1008 close* → the proxy isn't forwarding
  `x-serve-client-fingerprint`, or the value doesn't match the row in
  the leader's `nodes` table. Run `serve nodes show <id>` on the
  leader and compare against what the proxy logs are forwarding.

**`serve nodes ls` shows the node as `unreachable` even though `serve
agent start` is running.**

The health watcher flips to `unreachable` after 15 s without a
heartbeat. If the agent is alive but the row is stale, the WS isn't
connected — check the agent logs for reconnect attempts.

**`serve agent register` returns 403 `invalid or expired enrollment
token`.**

Tokens are single-use and expire after 10 minutes. Re-run `serve nodes
enroll <label>` on the leader to mint a new one.

**Re-enrolling an existing agent.**

`serve nodes enroll <existing-label>` is allowed and rotates the cert
fingerprint on the next register. The agent will need a fresh
`serve agent register …` run.

## Decommissioning a node

On the leader:

```bash
serve nodes remove <id>
```

This unregisters any live AgentLink, deletes the row, and forgets the
cert fingerprint. The agent process keeps trying to reconnect; stop it
on the agent host (`Ctrl-C` or your service manager). The local node
(`label=local`) cannot be removed.

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

## Roadmap

Tracked in the design doc, in priority order:

1. **Manager-via-AgentLink.** Route `start_deployment` through the
   chosen node's link; move `wait_healthy` and image digest collection
   behind `AgentLink`. After this, remote-agent deployments serve
   traffic end-to-end.
2. **Proxy via `_proxy_via_link`.** Thread the helper into the `/v1/*`
   route handler so the data plane goes through `AgentLink` instead of
   direct httpx.
3. **Direct-LAN ingress.** Agent opens a guarded mTLS ingress on its
   `reachable_as` address; leader probes and switches the data plane
   to direct routing when reachable. Falls back to tunnel
   automatically.
4. **uvicorn-direct mTLS.** Remove the reverse-proxy requirement for
   the cluster endpoint.
5. **UI.** Nodes page, node chip on deployment cards, profile
   `node_label` editor.
6. **Service-profile `node_label` affinity.**
7. **Replica fan-out across nodes.**
8. **Reconnect reconciliation polish** (orphan-container kill, drift
   correction).
