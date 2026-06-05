# Multi-node berth

This is the operator's guide to running a leader plus one or more GPU agents. It covers setup, verification, and the things that go wrong. If you only have one box, you don't need any of this — start with the [README](../README.md) and come back when the second box shows up.

The short version: one leader serves the API. Each extra GPU host runs a thin agent that dials home to the leader over an mTLS WebSocket. Start, stop, health checks, logs, and `/v1/*` traffic all flow over that one link. The agent needs nothing but outbound HTTPS to the leader's cluster port — no inbound ports, no VPN, no NAT punching.

> **Status.** The cluster fabric, enrollment, remote start/stop, tunneled proxying, health probes, and remote log streaming are wired up and tested. Direct-LAN data-plane mode, replicas, automatic public ACME, and fine-grained per-agent request metrics are still on the list. See [Status: what works today](#status-what-works-today) for the honest line on each.

## Concepts

There are six words you need before the rest of this makes sense.

**Leader.** The plain `berth daemon` you already know. It owns the SQLite state, the OpenAI-compatible API, the admin API, the router, and the lifecycle manager. There is exactly one leader.

**Agent.** A thin daemon (`berth agent start`) on each extra GPU host. It dials home to the leader over an mTLS WebSocket and exposes that host's local Docker through the channel. No API of its own.

**Public listener.** The leader's external HTTPS port, default `:11500`. Serves `/v1/*`, `/admin/*` (bearer auth), `/healthz`, `/metrics`. You supply a publicly-trusted cert for browser and SDK clients; in dev it falls back to a self-signed cluster-CA cert.

**Cluster listener.** A second HTTPS port, default `:11501`, with mTLS and its own cert chain. Serves `/cluster/agent` (the WebSocket), `/admin/nodes/register` (token-gated), and `/admin/ca.pem` (fingerprint-pinned). This is the only port an agent ever touches.

**Cluster CA.** A self-signed root that lives only on the leader, at `~/.berth/ca/`. It mints the leader's cluster-listener server cert and every agent's mTLS client cert. Agents pin its SHA-256 fingerprint at enrollment, so a swapped CA during bootstrap fails loudly.

**Node.** A row in the leader's `nodes` table. The leader's own host is always there as `label=local`. Each enrolled agent adds one more row.

## Install

Leader and agent install the same package. There is no separate agent build — the role is decided at runtime.

```bash
git clone https://github.com/Mapika/berth
cd berth
uv tool install --editable .
berth doctor
```

Linux, NVIDIA, and Docker 24+ apply to every agent host, because that's where the engines actually run. The leader needs Docker too — it owns lifecycle orchestration and can run local deployments — but it can sit mostly as a control plane when all the workloads target remote node labels. If that's your plan, run `berth doctor --leader-only` on the leader to skip the GPU checks, and set `[server] leader_only = true` (or `berth deploy bootstrap --leader-only`) so the daemon doesn't expect local GPUs.

## Quick start: LAN, one afternoon

On a LAN you don't need a reverse proxy or a real cert. The leader autodetects its LAN IP and serves TLS on both ports straight from uvicorn.

### 1. Start the leader

```bash
berth daemon start
```

The banner prints both URLs and the CA fingerprint:

```
daemon started (pid …)

public  : https://192.168.0.164:11500    ⚠  using cluster-CA cert
            external clients must trust sha256:7f3a… or set [public_tls]
cluster : https://192.168.0.164:11501  (cert: berth cluster CA)
            ca fingerprint: sha256:7f3a…
```

The "using cluster-CA cert" warning is expected on a LAN. You don't need a publicly-trusted cert for traffic that never leaves your network. Browser and SDK clients hitting the public URL will have to trust the cluster CA — or you just ignore the warning if you only call from loopback, the local CLI, or a trusting `httpx` client.

### 2. Mint an enrollment URI

On the leader, name the new GPU host:

```bash
$ berth nodes enroll gpu-rig-2
Enrollment URI (single-use, expires in 10 min):

  berth://enroll?leader=https%3A%2F%2F192.168.0.164%3A11501&token=…&ca_fp=sha256%3A7f3a…

On the agent host, run:
  berth agent register --uri 'berth://enroll?leader=…&token=…&ca_fp=…'
```

Copy the whole `berth agent register --uri '…'` line. The token is single-use and expires in 10 minutes.

### 3. Register and start the agent

On the new GPU host:

```bash
# Same install as the leader.
git clone https://github.com/Mapika/berth
cd berth
uv tool install --editable .

# Paste the line you copied from the leader.
berth agent register --uri 'berth://enroll?leader=…&token=…&ca_fp=…'
berth agent start
```

Here's what `register --uri` actually does:

1. Parses the leader URL, token, and CA fingerprint out of the URI.
2. Fetches `https://<leader>/admin/ca.pem` with TLS verification off — just for this one request, because the agent doesn't trust the CA yet.
3. Checks that `sha256(downloaded ca.pem)` equals the pinned fingerprint. This closes the MITM-during-CA-bootstrap hole: an attacker who substitutes their own CA fails the check and registration aborts.
4. Writes the verified CA to `~/.berth/ca.crt`.
5. POSTs the token to `/admin/nodes/register`, gets back a per-host mTLS client cert, and writes `agent.crt`, `agent.key`, and `agent.yaml`.

After that, `berth agent start` runs the agent loop and connects to `wss://<cluster URL>/cluster/agent` over mTLS. In an interactive terminal it shows a compact live status panel — status, leader, node, uptime, current operation, last error, recent activity. The full reconnect, download, Docker, and warning detail goes to `~/.berth/logs/agent.log`. Tail it with `berth agent logs --follow`, or run `berth agent start --verbose` when you want the raw logs and progress bars in the foreground instead of the panel.

To keep the agent running across reboots, install it as a service:

```bash
berth agent install-service            # a --user unit (default)
berth agent install-service --system   # a system unit; run via sudo
```

The `--user` unit lands in `~/.config/systemd/user/berth-agent.service`. Run `loginctl enable-linger $USER` so it survives logout. The `--system` unit goes to `/etc/systemd/system/` and needs a non-root account for `User=` (it picks up `$SUDO_USER`). Both enable and start the unit immediately if `systemctl` is present.

### 4. Verify, from the leader

```bash
berth nodes ls            # the new node should show up `ready`
berth nodes show <id>     # GPU inventory, agent version, last heartbeat
```

`nodes ls` prints ID, label, status, GPU count, total VRAM, and agent version. `nodes show <id>` adds CPU/RAM and the per-GPU list.

### 5. Run a model on the agent

Target the node label when you launch:

```bash
berth run qwen-0_5b --node gpu-rig-2 --gpu 0 --engine vllm
```

Or set `node_label` on a service profile. Either way the leader sends the start request through the agent tunnel, the agent downloads and mounts the model on its own host, and `/v1/*` traffic gets proxied back through the same mTLS WebSocket. The default for `--node` is the leader host; any other value has to match an enrolled, currently-ready agent.

## Configuring the leader

Every address-shaped setting resolves in this order, flag winning over everything:

1. Flags on `berth daemon start`: `--public-host`, `--public-port`, `--public-bind`, `--public-cert`, `--public-key`, `--cluster-host`, `--cluster-port`, `--cluster-bind`.
2. Env vars: `BERTH_PUBLIC_HOST`, `BERTH_PUBLIC_PORT`, `BERTH_PUBLIC_BIND`, `BERTH_CLUSTER_HOST`, `BERTH_CLUSTER_PORT`, `BERTH_CLUSTER_BIND`.
3. `~/.berth/config.toml`.
4. Autodetect for the host (a UDP-connect trick plus `gethostbyname`), `127.0.0.1` for the bind, `11500` / `11501` for the ports.

`BERTH_LEADER_URL` is a separate thing: it overrides only the *advertised* cluster URL, i.e. what goes into enrollment URIs. It does not change what the daemon binds to.

### Inspect

```bash
berth config show
```

```
public.host          api.example.com                   (file)
public.port          11500                             (default)
public.bind          127.0.0.1                         (default)
public_tls.cert      /etc/le/.../fullchain.pem         (file)
public_tls.key       /etc/le/.../privkey.pem           (file)
cluster.host         cluster.example.com               (file)
cluster.port         11501                             (default)
cluster.bind         10.0.0.1                          (file)
server.leader_only   False                             (default)
leader_url_override  -                                 (default)

resolved public_url : https://api.example.com:11500
resolved cluster_url: https://cluster.example.com:11501
config file         : /home/you/.berth/config.toml
```

Each value shows where it came from, so you can tell a flag from a file from a default.

### Edit

```bash
berth config set-public host=api.example.com port=11500
berth config set-cluster host=cluster.example.com bind=10.0.0.1
berth config set-public-tls cert=/etc/le/.../fullchain.pem \
                            key=/etc/le/.../privkey.pem
```

`set-public` and `set-cluster` accept `host`, `port`, `bind`. `set-public-tls` accepts `cert`, `key` and warns if the paths don't exist yet. Anything else is rejected.

The file is `~/.berth/config.toml`:

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
bind = "10.0.0.1"   # bind the cluster listener to a VPN interface

[server]
leader_only = false  # true on a control-plane-only VPS with no local GPUs
```

Every section is optional. Missing keys fall through to env, then autodetect.

## Cross-network setup: different networks, VPN, NAT

This is the case the tunnel-by-default design was built for. The agent only needs outbound reach to the leader's cluster port. No port forwarding on the agent side, no NAT punching, no VPN.

What you need:

- The leader has a stable, routable address from the agent's network — public DNS plus a public IP, a Tailscale or WireGuard peer, whatever.
- The leader's cluster port is reachable from the agent.
- The agent's firewall allows outbound HTTPS to that port.

Topology:

```
[ public internet ]
                  |
                  v
       cluster.example.com:11501      (berth daemon, cluster listener)
                  |  (TLS + mTLS, terminated by uvicorn directly)
                  |
                  +-- agents dial in from anywhere

       api.example.com:11500          (berth daemon, public listener)
                  |  (TLS, terminated by uvicorn directly)
                  |
                  +-- SDK clients call /v1/*
```

The flow is identical to the LAN setup. The enrollment URI carries the cluster URL, so the agent dials the right port on its own:

```bash
# On the leader:
berth nodes enroll home-rig

# On the agent, anywhere with outbound HTTPS to the cluster port:
berth agent register --uri 'berth://enroll?…'
berth agent start
```

### About latency and throughput

I'll be straight about the cost. Every prompt byte and every generated token traverses the leader↔agent WebSocket. For one stream at 30 tok/s and ~80 B/token that's about 2.4 KB/s — nothing. At 50 concurrent streams it's a few hundred KB/s through one persistent socket on the leader process. The leader's uplink is the bottleneck.

This is fine for moderate scale. It is not what you want for serving hundreds of concurrent users from a residential leader. Direct-LAN mode — agent advertises a reachable address, leader probes it, data plane goes direct when both ends can actually reach each other — is on the list precisely to skip this hop when it isn't needed.

## Public-internet exposure

Both listeners bind to localhost by default. To expose either one, make the bind explicit and then harden.

For a full VPS bring-up (config, DB, CA, first admin key, a Caddyfile), use `berth deploy bootstrap --domain api.example.com [--cluster-domain cluster.example.com]`. It writes everything and prints the next steps. To set binds by hand, use `config set-*`.

### Public listener cert

External SDK clients won't trust your self-signed cluster CA. Give the public listener a real cert through `[public_tls]` in `config.toml` or the `--public-cert` / `--public-key` flags. Usual sources: Let's Encrypt (`certbot --standalone --preferred-challenges http -d api.example.com`, or a DNS-01 challenge if you're behind NAT), an internal corp CA, or a managed cert from your cloud provider.

Skip it and the daemon prints a loud warning on startup and serves the cluster-CA cert on the public listener. Fine for demos and internal use. Not fine for browser clients.

### Cluster listener bind

If the leader has a VPN or private interface, bind the cluster listener to it:

```bash
berth config set-cluster bind=10.0.0.1
```

Now only hosts on the VPN can reach `/cluster/agent`. If some agents live on the open internet with no VPN, they need the public interface — leave `bind = 0.0.0.0` and firewall the cluster port down to your known agent source IPs.

### What's built in

- `/admin/nodes/register` is rate-limited to 10 attempts per source IP per minute. Overflow gets a 429 with `Retry-After`.
- Every register attempt, success or failure, is logged through the `berth.audit` logger with the source IP and a token prefix.
- Enrollment tokens are single-use and expire after 10 minutes.
- `berth nodes remove <id>` makes the next WS handshake from that agent fail. The cert fingerprint is re-checked against the DB on every connection — it's never cached in-process, so revocation is immediate.

### What this design does not protect against

I'd rather be clear about the edges than imply they're covered.

- **Root on the leader.** Anyone with root can mint agent certs from `~/.berth/ca/ca.key`. Protect that file.
- **A stolen public-cert key.** If your `[public_tls]` key leaks, an attacker can MITM the public listener. Normal cert hygiene applies; this design neither helps nor hurts.
- **DDoS and volumetric abuse.** The fixed-window rate limit slows brute force. It is not a DDoS defense. Put a CDN or WAF in front of the public listener if you expect hostile traffic.
- **A compromised agent host.** mTLS authenticates a cert, not a human. Whoever controls an enrolled agent host can stream traffic into your fleet until you `berth nodes remove` them.

## Reverse-proxy fingerprint mode

The older deployment style — a reverse proxy in front of plain-HTTP uvicorn, forwarding `x-berth-client-fingerprint` — is still supported as an opt-in, for operators who already terminate TLS upstream:

```bash
BERTH_TRUST_FORWARDED_FP=1 berth daemon start
```

With that set, `LeaderHub` accepts the proxy-forwarded fingerprint header when no TLS peer cert is present, but only from direct peers in `BERTH_FORWARDED_ALLOW_IPS` (default `127.0.0.1`). Do not set this on an internet-exposed leader without an upstream proxy doing real mTLS verification. The header is unauthenticated on its own, so anything that can reach the port can spoof it.

## Status: what works today

What you can rely on:

- Two-listener TLS termination inside uvicorn — no reverse proxy required for either `/v1/*` or `/cluster/agent`.
- Enrollment via single-paste URI with a CA-fingerprint pin.
- Single-use token consumption, rate-limited registration, an audit log.
- mTLS WebSocket connections with real TLS peer-cert verification on every connection (a DB lookup, no in-process fingerprint cache).
- `berth nodes ls / show / enroll / remove`, `berth agent register / start / status / logs / adopt / adopted / unadopt / install-service`.
- `berth config show / set-public / set-cluster / set-public-tls`.
- A heartbeat health watcher: `ready` flips to `unreachable` after 15 s of silence and recovers on reconnect.
- Remote `start_deployment`, `stop_deployment`, health probe, and log streaming through `AgentLink`.
- A tunneled `/v1/*` data plane through `RemoteAgentLink.proxy_request`, covered by `tests/integration/test_remote_agent_roundtrip.py`. The same path serves `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/responses`, and `/v1/models`.
- `berth run --node <label>` and service-profile `node_label` targeting.
- A cluster UI for nodes, transport summary, GPU inventory, and sparkline metrics.

What's still missing:

- Direct-LAN data-plane mode (advertised `reachable_as` plus leader probing).
- ACME / Let's Encrypt auto-issuance for the public-listener cert. Bring your own cert today.
- Replicas of one service profile across nodes.
- Per-agent request attribution is still coarse — see [Observability](#observability).
- Reconnect reconciliation favors keeping remote rows live over aggressively killing orphaned remote containers.

In other words: the tunneled path is real, but this is still a small control plane, not a full scheduler.

## Troubleshooting

**`berth agent register --uri` fails with "CA fingerprint mismatch".**
Either the URI was tampered with in transit, or you're talking to a different leader than the one that minted it — e.g. the leader's CA got regenerated because someone deleted `~/.berth/ca/`. Re-mint:

```bash
berth nodes enroll <label>
```

**`berth agent start` connects, then the WS handshake closes immediately with 1008.**
The leader rejected the peer cert. Usual causes:

- The agent's `agent.yaml` points at the wrong leader or port. The cluster port is `:11501`, not `:11500`. Re-register.
- The leader's CA rotated since enrollment. Re-register.
- The node was removed from the DB. Check `berth nodes ls` on the leader.
- The leader's `~/.berth/logs/daemon.log` should have a `cluster ws reject:` line with the cause.

**`berth nodes ls` shows the node `unreachable` even though `berth agent start` is running.**
The health watcher flips to `unreachable` after 15 s without a heartbeat. If the agent is alive but the row is stale, the WS isn't actually connected — check the agent logs for reconnect attempts.

**`berth agent register` returns 403 "invalid or expired enrollment token".**
Tokens are single-use and expire after 10 minutes. Re-run `berth nodes enroll <label>` for a fresh URI.

**`berth daemon start` fails with "port already in use" on 11501.**
Either another daemon is up (`berth status`) or another process holds the port. Pick a different cluster port:

```bash
berth config set-cluster port=21501
berth daemon start
```

Existing agents will keep trying the old port, so re-enroll them.

**Re-enrolling an existing agent.**
`berth nodes enroll <existing-label>` is allowed. It rotates the cert fingerprint on the next register, so the agent needs a fresh `berth agent register --uri …` run.

## Decommissioning a node

On the leader:

```bash
berth nodes remove <id>
```

This unregisters any live AgentLink, deletes the row, and stops the cert fingerprint from authenticating new connections. The DB lookup runs on every WS handshake, so revocation takes effect immediately. The agent process keeps trying to reconnect — stop it on the agent host (Ctrl-C, or your service manager: `systemctl --user stop berth-agent`). The local node (`label=local`) can't be removed.

## Adopting an externally-hosted model

An agent can adopt an OpenAI-compatible server that's already running on its host. The model becomes routable through the leader's gateway, but berth never starts, stops, or manages the server — it registers the endpoint and routes `/v1/*` to it. Useful for a legacy service, a third-party provider, or something you deployed by hand.

### Register an adopted endpoint

By **docker container name** — berth introspects the published port, the GPU device-requests, and the `/v1/models` list:

```bash
berth agent adopt --container my-inference-server
```

By **host and port**, where you supply the details:

```bash
berth agent adopt --port 30011 --model nvidia/MiniMax-M2.7-NVFP4 \
  --host 127.0.0.1 --gpus 0 --vram-mb 5000
```

The flags for the `--port` form:

- `--port <p>` — the port the external server listens on. Required.
- `--model <m>` — the local registry/label. Required with `--port`.
- `--host <h>` — defaults to `127.0.0.1`.
- `--gpus <ids>` — comma-separated GPU ids, e.g. `0,7`.
- `--vram-mb <n>` — VRAM to reserve for those GPUs (0 leaves the scheduler to treat them as full).
- `--served-model-name <s>` — the routing name (see below).
- `--name <label>` — a local label; defaults to the model name.

The **served name** is the name clients pass to the leader's gateway. It's resolved in this order:

- If you pass `--served-model-name S`, that's it.
- Otherwise berth queries the endpoint's `/v1/models` and takes the first model's `id`.

`--model` is the local registry name. When you don't pass `--served-model-name`, it is not necessarily the routing name. Clients reach the model at the leader using the served name. Adoption takes effect on the running agent — the agent re-reports its adopted set on the next tick.

### List adopted endpoints

```bash
berth agent adopted
```

Prints each endpoint's name, address:port, served model name, and reserved GPUs, for the current agent host.

### Remove an adopted endpoint

```bash
berth agent unadopt nvidia/MiniMax-M2.7-NVFP4
```

The argument is the adopted endpoint's name (the local label — `--name`, or the model name if you didn't set one). The route drops on the next agent report. The external server keeps running; berth only stops routing to it.

### Notes

A few things worth knowing about adopted endpoints.

- **Routing.** They're reachable at the leader gateway under their served name (`--served-model-name`, else the first `/v1/models` `id`). SDK clients call the leader with that name and the request is proxied through the agent to the external server.
- **Lifecycle.** berth never starts, stops, restarts, or idle-evicts an adopted server. Keeping it running is on you or whatever else manages it. `unadopt` removes the route only.
- **Liveness.** berth does not currently health-probe adopted endpoints. They're reported alive on (re)connect. If the upstream is down, requests fail at proxy time and surface as errors to the client. Active liveness probing is planned.
- **GPU reservation.** Adopted endpoints reserve their declared GPUs in berth's scheduler so other models won't get placed there. If you change the external server's GPU allocation, re-adopt or run `berth agent adopt --gpus …` again. Note that berth's automatic placement runs only for leader-local deployments — on remote agent nodes the operator assigns GPUs, so an adopted endpoint's reserved GPUs are recorded for accounting and visibility but you're the one who has to avoid double-assigning them.
- **Persistence.** The adopted set lives in `~/.berth/adopted.yaml` on the agent and is reported to the leader on every connect (and re-reported when the file changes), so adoptions survive agent restarts.

## Tuning

Most defaults are fine for a small fleet. If you need to change one, here's where it lives. Several are hard-coded for now — lift them to config when you actually hit the limit.

- **Heartbeat interval.** Agent sends every 5 s; the leader marks a node `unreachable` after 15 s of silence. See the heartbeat task in `src/berth/cluster/agent_client.py` and `stale_after_s` in `src/berth/cluster/health_watcher.py`.
- **Reconnect backoff.** Agent starts at 1 s and doubles to a 30 s cap. See `run_agent()` in `agent_client.py`.
- **Enrollment token TTL.** Default 600 s. See `EnrollmentTokens` in `src/berth/cluster/enrollment.py`.
- **Registration rate limit.** Default 10 attempts / IP / 60 s. See `_rate_limit` in `src/berth/daemon/admin.py`.
- **Server-cert validity.** 5 years. Regenerated automatically when `public_host` or `cluster_host` changes and the existing SAN doesn't cover the new value. See `ensure_server_cert` in `src/berth/cluster/ca.py`.

## Observability

Each agent sends GPU stats on every heartbeat tick (5 s by default) over the existing WebSocket. The leader's own node publishes local request counters into the same `MetricsAggregator`. The schema has per-deployment request and latency fields, though remote-agent request attribution is still coarse (noted at the end). The aggregator keeps a 60 s rolling window per node and exposes it three ways.

### Prometheus

`GET /metrics` on the public listener appends the cluster series after the existing daemon and engine sections. The label sets are stable, so it's safe to scrape at the default 30 s interval.

| Series | Type | Labels |
| --- | --- | --- |
| `berth_node_gpu_util_pct` | gauge | `node`, `gpu` |
| `berth_node_gpu_mem_used_bytes` | gauge | `node`, `gpu` |
| `berth_deployment_in_flight` | gauge | `node`, `deployment`, `model` |
| `berth_deployment_requests_total` | counter | `node`, `deployment`, `model` |
| `berth_deployment_latency_p50_ms` | gauge | `node`, `deployment`, `model` |
| `berth_deployment_latency_p95_ms` | gauge | `node`, `deployment`, `model` |
| `berth_deployment_errors_total` | counter | `node`, `deployment`, `model` |

The leader's own node shows up in these series too. A small background task on the daemon mirrors the agent heartbeat for the local node, so you get one consistent surface whether a metric is local or remote.

### Grafana dashboard

`docs/dashboards/berth.json` is a six-panel starter: GPU utilization, GPU memory, in-flight, p95 latency, request rate, error rate. Import it from Grafana's "Import dashboard" page. There's no auto-provisioning, and you'll want to tune the panel queries to your own label conventions.

### Admin snapshot endpoint

`GET /admin/metrics/snapshot` returns the latest per-node sample plus short sparkline series. The UI's Cluster page polls it every 5 s and renders inline SVG sparklines on each node card.

### Known limitations today

- The leader's in-flight counter increments for every request the proxy dispatches, including the ones routed to remote nodes. So the local sample over-counts when remote deploys are active. Per-node attribution is part of the smart-routing follow-up.
- Remote agents currently report empty deployment lists. Instrumenting the agent's `_run_http_stream` dispatch path to populate in-flight and latency is a small follow-up.

## Routing and resilience

When more than one deployment can serve a request, a load-aware scorer (`src/berth/routing/scorer.py`) picks one. For a given (base, adapter) target, the proxy:

1. Collects every ready deployment as a candidate.
2. For adapter requests, applies the tier filter — already-loaded beats free-slot beats needs-evict. Only the best tier gets scored. Mixing tiers would hide cold-load latency behind a fast idle node.
3. Calls the scorer with per-node `NodeSignals` from the metrics aggregator: free memory, in-flight, p95 latency.

### Default scorer

A hard memory filter, then a lexicographic rank:

1. `affinity_hit` — 1 if the leader's affinity map already points at this node.
2. `-in_flight` — less loaded first.
3. `-p95_latency_ms` — faster recent performance first.

Explicit tiers, not weighted floats, so the decision is debuggable. A just-enrolled node with no aggregator entry yet stays a candidate but ranks last — you can't memory-filter it without evidence, and preferring a known-fit, known-fast node is the right bias.

### Affinity

`RoutingAffinity` is a bounded LRU map of `affinity_key → node_id`. The key precedence is `X-Session-Id`, then `X-Conversation-Id`, then the API key. On every successful dispatch the proxy records the chosen node; later requests with the same key get an `affinity_hit=1` boost, which keeps a conversation pinned to whichever node holds its KV cache. It's best-effort: lost on process restart, and cleared per-node when a node goes `unreachable`.

### Node-loss audit

When the health watcher demotes a node to `unreachable`, it emits a `node_loss_audit` warning with the node id and label, and clears the affinity entries pointing at that node. Routing treats the node as gone until it re-handshakes.

### Request-level retry

`dispatch_with_retry` (`src/berth/daemon/retry_dispatcher.py`) wraps the dispatch step. For a bare-base request the proxy gets the full scorer-ranked candidate list and walks it, retrying when the chosen node raises a retryable pre-first-byte error: connection refused, timeout, a 502/503/504 upstream, or `NodeUnreachableError` (the AgentLink isn't ready or has no container for that deployment).

- **Budget.** 2 retries (3 attempts total) by default for bare-base requests. Zero retries for adapter requests — the adapter is loaded on the chosen head deployment only, so falling through would land on an engine without the right LoRA slot.
- **Distinct nodes.** Each node is tried at most once per request. Multiple candidates on the same node don't burn extra attempts.
- **Pre-first-byte only.** Once any body byte has reached the client, the streamer commits to that upstream. A node dying mid-generation propagates the error — KV-cache transfer is not on the roadmap.

The dispatch step itself was carved out of `openai_proxy.py` into `src/berth/daemon/dispatch.py:open_upstream_stream`. That unit knows nothing about request context or usage tracking — it opens a stream and returns once status and headers are known. The proxy wraps the returned `body_iter` in one unified streamer that handles in-flight, latency, and usage attribution against the **landing** deployment.

### SSE backpressure

`_bounded_pipe` (`src/berth/daemon/openai_proxy.py`) sits between the upstream reader task and the FastAPI streamer. It uses an `asyncio.Queue(maxsize=N)` to cap how far the engine can run ahead of a slow client: once the queue is full, the reader blocks on `put()` and that backpressure propagates through the upstream stream to the engine, so no more bytes get pulled until the client drains a chunk.

- **Default depth.** 64 chunks. Tunable per-app via `app.state.sse_queue_depth`.
- **Exception forwarding.** Errors from the upstream iterator pass through the queue and re-raise in the streamer, so the usage-tracker and tracer finalize path always runs.
- **Early-close cleanup.** If the client disconnects, the streamer's `finally` cancels the reader task — no lingering pull on the upstream.

## Production deploy

For a public-facing leader — Caddy plus ACME TLS, systemd, real auth, backups — see [deploy.md](deploy.md) and [caddy.md](caddy.md). `berth backup create` snapshots the disaster-recovery set: the DB, `ca/`, `key_pepper`, and `config.toml`.

## Roadmap

Likely next work, in rough priority order:

1. **Direct-LAN ingress.** The agent opens a guarded mTLS ingress on its `reachable_as` address; the leader probes it and switches the data plane to direct routing when reachable, falling back to the tunnel automatically.
2. **ACME for the public listener.** Auto-issue and auto-renew via Let's Encrypt, so a one-command public deploy works without external certbot orchestration.
3. **Replica fan-out across nodes.**
4. **Per-agent request metrics.** Attribute in-flight and latency on the agent side instead of over-counting on the leader.
5. **Reconnect reconciliation polish** — orphan-container kill, drift correction.
