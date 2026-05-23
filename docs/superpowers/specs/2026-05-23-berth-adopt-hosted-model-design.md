# Adopt an externally-hosted model from the agent

**Status:** approved design, pre-implementation
**Date:** 2026-05-23
**Scope:** single-node agent feature + leader reconciliation

## Motivation

Today a model only becomes reachable through berth if the leader *launches* it:
the operator creates a deployment on the leader, which dispatches a
`StartDeployment` to an agent, which runs a `berth-` container. Operators who
already run an OpenAI-compatible server by hand (e.g. a `docker run …
sglang.launch_server` on the GPU box) have no way to put that running server
behind berth's gateway.

This feature lets the operator **adopt** an already-running, OpenAI-compatible
endpoint from the agent host. berth routes cluster traffic to it and reserves
its GPUs, but launches and stops nothing for it. Because berth runs no
container, adoption bypasses the `allow_unsafe_deploy_options` gate entirely —
there is no image or engine flag to vet.

Concrete first use case: the hand-started `minimax-m27` container on GPU 7
(`nvidia/MiniMax-M2.7-NVFP4` on `:30011`) becomes reachable through
`cluster.berth.run/v1` without re-deploying it under berth.

## Goals

- `berth agent adopt` on the agent host registers a running OpenAI-compatible
  endpoint as a deployment, identified either by docker container name
  (introspect) or by explicit `--port`/`--model`.
- The leader gets a deployment row marked `source='adopted'` and routes `/v1/*`
  to it exactly like a managed model.
- Adopted endpoints reserve their GPU(s) in the scheduler so berth will not
  place other models on the same GPUs.
- berth never starts, stops, restarts, or idle-evicts an adopted endpoint. It
  health-probes it and marks the deployment `down` if it dies.
- Adoption self-heals across agent restarts.

## Non-goals (YAGNI)

- Multiple replicas / load-balanced adoption of one model.
- Auto-discovery or port scanning.
- berth managing the external process's lifecycle (start/stop/restart).
- Non-OpenAI-compatible protocols.
- Leader-side adoption trigger (`berth run --adopt-endpoint`); rejected because
  the requirement is to trigger from the agent host.

## Architecture

Approach: **agent-authoritative adoption.** The agent owns the set of adopted
endpoints (persisted locally), registers them into its dispatcher, and reports
the full set to the leader on every (re)connect. The leader reconciles its
`adopted` deployment rows for that node to match the report. This reuses berth's
existing grain — the agent already enumerates containers, re-attaches endpoints
on startup, and reports host info/GPU stats to the leader.

### Why routing needs no new path

The leader proxies a request via `_proxy_via_link(node_id, container_id)` →
`link.proxy_request(container_id=…)`; the agent's `AgentFrameDispatcher` looks up
`self._endpoints[container_id] → (address, port)` and forwards. So the routing
key is `container_id`, and `register_endpoint(container_id, address, port)` is
the exact hook. Once an adopted deployment row exists with `node_id` +
`container_id`, and the agent has registered that endpoint, proxying already
works unchanged.

## Data model

Migration `016_adopted.sql`:

- `ALTER TABLE deployments ADD COLUMN source TEXT NOT NULL DEFAULT 'managed';`
  Values: `'managed'` | `'adopted'`.

Reuse existing `deployments` columns for adopted rows:

| column | meaning for an adopted row |
|---|---|
| `container_address`, `container_port` | the external endpoint host:port |
| `container_id` | routing key: the real docker id (when adopted by `--container`) or a synthetic `adopted-<model>-<port>` (raw process) |
| `gpu_ids` | GPUs the external server occupies (reserved) |
| `vram_reserved_mb` | full per-GPU size × #gpus (treat as fully occupied) |
| `backend` | `'adopted'` |
| `image_tag` | introspected image ref, or `'external'` |
| `node_id` | the adopting agent's node |
| `status` | `ready` when the endpoint is live, `down`/`failed` when not |

`store/deployments.py`: add `source` to the `Deployment` dataclass (default
`'managed'`), `_row_to_dep` (via `row_get` for forward-compat), and a `create`
path / dedicated `upsert_adopted(...)` helper. Add `list_adopted_for_node(conn,
node_id)` for reconciliation.

## Protocol

One new agent→leader frame in `cluster/protocol.py`, added to `_REGISTRY` and the
`Frame` union:

```python
@dataclass
class ReportAdopted:
    endpoints: list[dict[str, Any]]  # full current set for THIS node
    type: str = "report_adopted"
```

Each entry: `{model_name, served_model_name, address, port, container_id,
gpu_ids, vram_reserved_mb, alive}`.

**Model naming (routing).** For an adopted endpoint the berth model-registry
name and the routing name are the upstream's **`served_model_name`** (learned
from `/v1/models`). Clients call the leader with that exact `model` value and the
request is forwarded to the upstream unchanged — no `model`-field rewriting. The
optional `--name` is only a human label for `adopted ls` / deployment display; it
is **not** a routing alias. (A public-alias-with-rewrite feature is out of scope.)

The frame is **full-state** (not
incremental): the leader makes its `source='adopted'` rows for that node equal
the report — absent rows are removed and their GPUs freed. This is idempotent
and recovers automatically after a reconnect.

Sent: immediately after `Hello` on connect, and again whenever the local adopted
set or any `alive` flag changes. Liveness transitions also ride the existing
`Heartbeat.metrics` so the leader can flip `ready`↔`down` without a full report.

## Agent side

### `~/.berth/adopted.yaml`

Local source of truth, a list of entries:
```yaml
- name: minimax
  model_name: nvidia/MiniMax-M2.7-NVFP4
  served_model_name: nvidia/MiniMax-M2.7-NVFP4
  address: 127.0.0.1
  port: 30011
  container_id: <docker-id-or-synthetic>
  gpu_ids: [7]
  vram_reserved_mb: 268000
```

### CLI (`cli/agent_cmd.py`, under the existing `agent` Typer group)

- `berth agent adopt (--container NAME | --port P --model M) [--name N]
  [--host H] [--gpus 7,8] [--served-model-name S] [--vram-mb N]`
  1. Resolve endpoint: `--container` → docker inspect for the published host
     port and `HostConfig.DeviceRequests`/`NVIDIA_VISIBLE_DEVICES` to infer
     `gpu_ids`; `--port` → use explicit values.
  2. Probe `http://host:port/v1/models` (timeout): must respond. Learn / verify
     `served_model_name`. If unreachable, **abort and write nothing**.
  3. Validate: no GPU overlap with another local adopted entry; no name
     collision.
  4. Append to `adopted.yaml`.
- `berth agent unadopt <name>` — remove the entry; route drops on next report.
- `berth agent adopted ls` — show local entries + last-known live status.

### Agent runtime (`cluster/agent_client.py`)

- On connect, load `adopted.yaml`; for each entry call
  `disp.register_endpoint(container_id, address, port)` (existing hook), then
  send `ReportAdopted`.
- **Watch** `adopted.yaml` with `watchfiles` (already a dependency); on change,
  re-register/unregister endpoints and re-send `ReportAdopted` → adopt/unadopt
  take effect without restarting the agent.
- Health loop: probe each adopted endpoint's `/v1/models` (or backend
  `health_path`) on an interval; on liveness change, update the dispatcher and
  surface `alive` via heartbeat / a fresh `ReportAdopted`. **Never restart.**

## Leader side

- Handler for `ReportAdopted` in the cluster hub (`cluster/leader_hub.py`):
  reconcile `source='adopted'` rows for the reporting node against the report —
  upsert present entries (create the model row if needed via the model store;
  set `node_id`, endpoint columns, `gpu_ids`, `vram_reserved_mb`, status from
  `alive`), delete absent ones and free their GPUs.
- GPU reservation goes through the existing node-GPU/placement bookkeeping so the
  scorer/placement treats adopted GPUs as occupied.
- **Guardrails:**
  - `lifecycle/reaper.py`: skip `source='adopted'` (alongside the existing
    `pinned` skip) so idle-eviction never touches it.
  - `lifecycle/manager.py` `stop()`: for `source='adopted'`, deregister the
    endpoint + mark `stopped`; do **not** issue a docker stop / `StopDeployment`.
  - Reject (log a conflict, keep the entry `down`) a reported endpoint whose
    `gpu_ids` are already held by a *managed* deployment on that node.

## Error handling

- `adopt` writes nothing unless `/v1/models` responds.
- GPU overlap or name collision at adopt time → clear CLI error, no write.
- Leader-side GPU conflict with a managed deployment → entry stays `down`, logged;
  does not crash reconciliation of the other entries.
- Synthetic-`container_id` (raw process) endpoints have no docker logs;
  `berth logs` for them returns a clear "no container (adopted endpoint)" notice.

## Security

- No new authz surface beyond the existing agent mTLS link: the report travels
  over the already-authenticated agent WebSocket and can only create
  `adopted` rows pinned to the reporting node's own host:port and GPUs.
- Adoption launches no container, so the `allow_unsafe_deploy_options` image/flag
  gate is not involved. (A future per-node "allow adoption" policy is possible
  but out of scope.)

## Testing

**Unit**
- `ReportAdopted` encode/decode round-trip through `protocol.py`.
- Migration `016` applies; `Deployment.source` defaults to `'managed'`; adopted
  upsert + `list_adopted_for_node`.
- `reaper` skips `source='adopted'`.
- `manager.stop` adopted-branch deregisters without a docker stop (mock docker).
- `adopt` CLI resolution: container-introspection path (fake docker client) and
  explicit `--port/--model` path, both against a stub `/v1/models`; abort when
  the probe fails; GPU-overlap and name-collision rejections.

**Integration** (extends `tests/integration` + `scripts/smoke_p03_engines.sh`)
- Stub OpenAI server on a port → agent `adopt` → agent reports → leader upserts a
  `ready` adopted row → `POST /v1/chat/completions` through the leader reaches the
  stub. `unadopt` removes the route and frees the GPUs.

## File-change summary (for the implementation plan)

- `store/migrations/016_adopted.sql` — new.
- `store/deployments.py` — `source` field, adopted upsert, `list_adopted_for_node`.
- `cluster/protocol.py` — `ReportAdopted` frame + registry/union.
- `cluster/agent_client.py` — load/watch `adopted.yaml`, register endpoints, send
  report, health loop.
- `cli/agent_cmd.py` — `adopt`, `unadopt`, `adopted ls`.
- `cluster/leader_hub.py` — `ReportAdopted` reconciliation + GPU reserve/free.
- `lifecycle/reaper.py` — skip adopted.
- `lifecycle/manager.py` — `stop()` adopted-branch.
- Tests as above.
