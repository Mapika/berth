# Multi-Node Serving — Design

Status: approved for planning
Date: 2026-05-18

## Summary

Extend serve-engine from a single-host inference router to a small fleet:
one **leader** daemon (control plane, router, state, UI) plus one or more
**agent** daemons running on additional GPU hosts. Each model still runs on
one node — this is capacity sharding, not multi-host tensor parallelism. The
leader and agents share one codebase and one `serve` binary with a `--role`
flag; the agent is the existing Docker driver wrapped in an RPC shim.

The design is built around the realistic constraint that nodes may not share
a network: an agent might sit behind a residential NAT or only be reachable
over a VPN. The agent always dials the leader, never the reverse.

## Goals

- Manage N GPU hosts from one leader with one OpenAI-compatible endpoint.
- Work across networks: agent only needs outbound reach to the leader.
- Fit the existing driver/lifecycle contract — no architecture rewrite.
- Preserve current behavior for single-node installs with zero migration
  steps.
- Stay inside the project's stated scope (no multi-host TP, no leader HA, no
  k8s-lite).

## Non-Goals

- Multi-host tensor parallelism for one model.
- Replicas of the same service profile across nodes (natural follow-up).
- Live migration of a running deployment between nodes.
- Leader high availability / multi-leader.
- Cross-node KV cache sharing.
- Auto-discovery (mDNS, cloud tag lookup). Enrollment is explicit.
- Built-in TLS termination for `/v1/*` traffic from external clients —
  still a reverse-proxy concern.

## Architecture

```text
client / SDK
    |
    | HTTPS /v1/*
    v
+----------------------------+
| serve leader               |
| - OpenAI API + admin API   |
| - auth, limits, routes     |   <- mTLS WS -->  +-------------------+
| - router (proxy)           |                    | serve agent (N)   |
| - lifecycle manager        |   <- mTLS WS -->  | - docker driver   |
| - SQLite state             |                    | - gpu stats       |
| - in-process agent (local) |   <- mTLS WS -->  | - log tail        |
+----------------------------+                    +-------------------+
                                                          |
                                                          | docker API
                                                          v
                                                    engine containers
```

One binary, one Python package. `serve daemon start` defaults to `--role
leader` (current behavior). `serve agent register` and `serve agent start`
flip the same binary into agent mode.

## Connectivity & data plane

### Control channel

Each agent opens a long-lived mTLS WebSocket to the leader. The agent always
initiates the connection, so the agent host needs only outbound reach to the
leader. The leader needs no route back. Reconnect with exponential backoff
(1s → 30s capped, jittered).

The WS multiplexes:

- Control frames: start/stop, status, events.
- Telemetry frames: heartbeat, GPU stats, container health, log tails.
- Data-plane frames: per-request virtual streams for `/v1/*` proxying.

### Data plane: tunnel-by-default, direct-when-reachable

**Tunneled (default).** Inference traffic is multiplexed over the same WS.
The router opens a virtual stream per inflight `/v1/*` request; the agent
forwards to the local engine container and streams response chunks
(including SSE token deltas) back up the WS. This works through NAT, VPNs,
residential ISPs, and any topology where the leader cannot reach the agent
directly.

**Direct LAN (opt-in).** An agent may advertise a reachable address at
enrollment:

```text
serve agent register --leader https://… --token T \
    --reachable-as 10.0.0.7 --ingress-port 11600
```

When `--reachable-as` is set, the agent opens an inbound mTLS **ingress
port** on that address (default `11600`). The ingress is a single guarded
endpoint that authenticates the leader by client cert (same CA as the WS)
and proxies to the right local engine container based on a deployment
identifier in the request path. Engine containers stay bound to the
agent's loopback, exactly as today; the ingress is the only LAN-exposed
port. The leader probes the ingress every 10s with a tiny health request.
While probing succeeds, the router takes the direct path — proxying
`/v1/*` traffic through the ingress instead of the WS. When probing fails,
the leader silently falls back to tunneled mode for that node. Operator
opts in only where the LAN is trusted and the perf matters.

In tunneled mode (the default), no inbound port is opened on the agent at
all — only the outbound WS to the leader.

### Performance honesty

Tunneled mode means every prompt byte and every generated token traverses
the WS. For one LLM streaming 30 tokens/sec at ~80 bytes/token, that's
~2.4 KB/s per stream — negligible. At 50 concurrent streams it's a few
hundred KB/s through one persistent connection on the leader process.
Fine for the project's intended scale, but the leader is now a data-plane
participant for tunneled nodes. Direct mode keeps today's perf profile.

### Security

- **mTLS** on every agent↔leader WS. The leader acts as its own CA.
- **Enrollment**: leader mints a short-lived one-time token (`serve nodes
  enroll`). Agent presents the token on first connect, leader issues a
  per-agent client certificate, agent stores it in `~/.serve/agent.yaml`.
  The certificate is the durable identity; the enrollment token is single-
  use.
- **Identity pinning**: leader stores each agent's cert fingerprint and
  refuses connections whose presented cert fingerprint doesn't match the
  node row.
- **Revocation**: `serve nodes remove <label>` deletes the fingerprint on
  the leader. No fleet-wide re-enrollment needed.
- **Local agent exception**: the leader's in-process agent runs over a
  local channel (no TLS, no cert) since it's the same process.

## State

All persistent state stays on the leader's SQLite. New tables:

```text
nodes
    id              integer pk
    label           text unique
    fingerprint     text                  -- mTLS cert SHA-256
    reachable_as    text nullable          -- LAN hint for direct mode
    status          text                  -- ready | unreachable | gone
    first_seen      timestamp
    last_seen       timestamp
    agent_version   text
    cpu_count       integer
    total_ram_mb    integer
    gpu_count       integer
    total_vram_mb   integer

node_gpus
    node_id         integer fk
    gpu_index       integer
    name            text
    total_vram_mb   integer
    driver_version  text
    (node_id, gpu_index) primary key
```

`deployments` gains a `node_id` foreign key. Every other table is unchanged.

GPU stats and per-node telemetry stream into an in-memory per-node cache,
same shape as today's GPU stats path. No new persistence path for high-rate
telemetry.

The leader's own host is `node_id=0`, label `local`, fingerprint `local`,
with an in-process agent. A one-shot migration on first leader startup
inserts that row and sets `node_id=0` on every existing deployment.
Single-node installs see no behavioral change.

## Placement

`lifecycle/placement.py` currently picks a GPU by VRAM fit. The candidate
set expands from "all GPUs" to "all (node, gpu) pairs on ready nodes." The
existing fit / headroom logic is reused unchanged.

Order of operations:

1. **Filter** to nodes with status=`ready` (heartbeat fresh — within 15s).
2. **Affinity** — service profile gains an optional `node_label` field. If
   set, hard-restrict to that node. Default is unrestricted.
3. **Score** — tightest fit per node first (today's behavior), then break
   ties across nodes by lowest current GPU utilization. Prevents every new
   deployment from piling onto whichever node was enumerated first.
4. **Eviction** — LRU eviction is scoped per node. You can't free VRAM on
   node A to make room on node B.
5. **Fail** with a placement error if nothing fits anywhere — same surface
   as today.

The KV cache estimator and per-GPU VRAM math are untouched.

## Failure handling

A node has three states: `ready`, `unreachable`, `gone`.

- **Heartbeat miss** (default 15s). Node → `unreachable`. Router stops
  sending new requests to its deployments. Inflight requests fail with
  HTTP 503. Deployment rows are preserved; no eviction.
- **Reconnect.** Agent re-handshakes and sends a full state snapshot:
  containers currently running, their health, GPU status. Leader
  reconciles:
  - Deployments the leader expected ready but the agent doesn't know about
    → mark `failed`.
  - Containers running on the agent that the leader has no record of →
    kill (orphans).
  - Then resume normal routing.
- **Decommission** — `serve nodes remove <label>`. Leader attempts to stop
  the agent's deployments through the tunnel; if the agent is unreachable
  the deployments are marked `failed`. Row is deleted, cert fingerprint is
  revoked.

**Not in this design**: automatic failover of a running deployment to
another node. The operator restarts elsewhere if they want it on a
different host. Workload migration is its own feature.

## Operator surface

### CLI additions

```text
serve nodes ls                                list nodes, status, gpu summary
serve nodes show <label>                      detail: gpus, deployments, last seen
serve nodes enroll                            (on leader) mint enrollment token
serve nodes remove <label>                    decommission
serve agent register --leader URL --token T   (on agent host) bootstrap
    [--reachable-as <addr>]                   opt into direct LAN mode
serve agent start                             start agent daemon
serve agent status                            local agent state
serve ps                                      gains NODE column
serve run --node <label>                      optional pin at launch
```

`serve daemon start` continues to default to leader mode. No flag changes
for existing single-node users.

### UI additions

- **Nodes page**: one card per node with status pill, GPU list, deployment
  count, agent version, last heartbeat, direct/tunneled mode indicator.
- **GPU view**: existing GPU cards group by node.
- **Deployment cards**: show a node chip.
- **Service profile / route editors**: optional node-affinity field.

These are additive — no existing screen needs a redesign.

## Migration & backwards compatibility

- New `nodes` and `node_gpus` tables added via a new migration in
  `store/migrations/`.
- `deployments.node_id` column added in the same migration, defaulted to
  `0` for existing rows.
- A one-shot startup step inserts the `node_id=0` local row using detected
  host info (`pynvml`, `/proc/meminfo`, `os.cpu_count()`).
- Existing CLI invocations, existing service profiles, existing routes —
  all keep working unchanged.

## File layout (anticipated)

```text
src/serve_engine/
    cluster/              -- new package
        __init__.py
        protocol.py        -- WS frame schema, virtual stream codec
        leader_hub.py      -- WS server, agent registry, cert pinning
        agent_client.py    -- WS client, reconnect, frame dispatch
        agent_daemon.py    -- agent --role entrypoint
        local_agent.py     -- in-process agent for node_id=0
        ca.py              -- CA helpers, cert mint, fingerprint pin
    lifecycle/
        placement.py       -- expand to (node, gpu)
        manager.py         -- routes lifecycle ops through agents
    daemon/
        openai_proxy.py    -- direct vs tunneled dispatch
    store/
        nodes.py           -- new
        node_gpus.py       -- new
        migrations/        -- add nodes / node_gpus / deployments.node_id
    cli/
        nodes.py           -- new
        agent.py           -- new
```

Existing files referenced above keep their current responsibilities; the
diff is bounded.

## Open questions to resolve at planning time

These are implementation-shape questions, not architecture questions. They
do not need to be settled before the plan is written; they need to be
settled in it.

1. WS framing format — JSON envelopes vs. msgpack vs. a small binary
   header. Likely JSON for control + length-prefixed binary for data
   plane chunks.
2. SSE backpressure across the tunnel — how to surface a slow client to
   the engine so generation doesn't run unbounded ahead of consumption.
3. Per-node rate limits — does the existing limiter scope correctly when a
   key's traffic lands on multiple nodes, or does it need to be moved
   leader-side?
4. UI for enrollment — show the one-time token once, copy-to-clipboard,
   never store it again.
