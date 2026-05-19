from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import yaml

from serve_engine.backends.base import Backend
from serve_engine.cluster.agent_link import StartedContainer
from serve_engine.cluster.agent_registry import AgentRegistry
from serve_engine.lifecycle.docker_client import DockerClient
from serve_engine.lifecycle.downloader import download_model
from serve_engine.lifecycle.kv_estimator import (
    KVEstimateInput,
    default_target_concurrency,
    estimate_vram_mb,
)
from serve_engine.lifecycle.placement import (
    AllocatedDeployment,
    EvictThenFit,
    NoRoom,
    PlacementRequest,
    plan_placement,
)
from serve_engine.lifecycle.plan import DeploymentPlan
from serve_engine.lifecycle.topology import Topology
from serve_engine.observability.events import Event, EventBus
from serve_engine.store import deployment_adapters as da_store
from serve_engine.store import deployment_plans as plan_store
from serve_engine.store import deployments as dep_store
from serve_engine.store import models as model_store
from serve_engine.store import nodes as nodes_store

log = logging.getLogger(__name__)


async def download_model_async(**kwargs) -> str:
    # snapshot_download is blocking; offload to a thread
    return await asyncio.to_thread(download_model, **kwargs)


def _json_safe_docker_kwargs(kw: dict) -> dict:
    """Convert docker SDK objects (Ulimit, etc.) to plain JSON-safe dicts
    so a remote-agent plan can be encoded across the WS. Agent-side
    `_rehydrate_docker_kwargs` reverses this.

    Note: docker.types.Ulimit IS a dict subclass with PascalCase keys
    ('Name'/'Soft'/'Hard'); the .name/.soft/.hard properties read those.
    We normalise to lowercase-key plain dicts so the agent can
    reconstruct without depending on attribute names."""
    def _ulimit_to_dict(u) -> dict:
        # Attribute access works for both Ulimit and plain-dict shapes.
        return {
            "name": getattr(u, "name", None) if not isinstance(u, dict)
                    else u.get("name") or u.get("Name"),
            "soft": getattr(u, "soft", None) if not isinstance(u, dict)
                    else u.get("soft") if "soft" in u else u.get("Soft"),
            "hard": getattr(u, "hard", None) if not isinstance(u, dict)
                    else u.get("hard") if "hard" in u else u.get("Hard"),
        }

    out = dict(kw)
    ulimits = out.get("ulimits")
    if ulimits:
        out["ulimits"] = [_ulimit_to_dict(u) for u in ulimits]
    return out


async def _dispatch_start(
    registry: AgentRegistry,
    *,
    node_id: int,
    plan: dict,
) -> StartedContainer:
    """Route a start_deployment call to the right AgentLink for `node_id`.

    Module-level for unit testability — LifecycleManager calls through this
    so tests can exercise the routing logic without spinning up FastAPI.
    """
    link = registry.get(node_id)
    if link is None or not link.is_ready:
        raise RuntimeError(f"node {node_id} not connected")
    return await link.start_deployment(plan)


async def _dispatch_stop(
    registry: AgentRegistry,
    *,
    node_id: int,
    container_id: str,
) -> None:
    """Route a stop_deployment call to the right AgentLink for `node_id`."""
    link = registry.get(node_id)
    if link is None:
        raise RuntimeError(f"node {node_id} not connected")
    await link.stop_deployment(container_id)


async def wait_healthy(url: str, *, timeout_s: float = 600.0, interval_s: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    async with httpx.AsyncClient(timeout=5.0) as client:
        while loop.time() < deadline:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(interval_s)
    return False


class LifecycleManager:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        docker_client: DockerClient,
        backends: dict[str, Backend],
        models_dir: Path,
        topology: Topology | None = None,
        load_timeout_s: float = 600.0,
        event_bus: EventBus | None = None,
        configs_dir: Path | None = None,
        agent_registry: AgentRegistry | None = None,
    ):
        self._conn = conn
        self._docker = docker_client
        self._backends = backends
        self._models_dir = models_dir
        # Public accessor lives at LifecycleManager.models_dir — defined
        # as a @property below. Callers must not reach _models_dir.
        # Per-deployment engine YAML configs, mounted into containers at
        # /serve/configs:ro. Backends opt-in via engine_config(plan).
        self._configs_dir = configs_dir or (models_dir.parent / "configs")
        self._topology = topology
        self._load_timeout_s = load_timeout_s
        self._events = event_bus
        # Cluster registry — when present, start/stop routes through the
        # AgentLink for the chosen node. Single-node deployments work
        # without it (the docker_client path stays).
        self._registry = agent_registry
        self._lock = asyncio.Lock()
        self._adapter_locks: dict[int, asyncio.Lock] = {}

    def adapter_lock(self, dep_id: int) -> asyncio.Lock:
        lock = self._adapter_locks.get(dep_id)
        if lock is None:
            lock = asyncio.Lock()
            self._adapter_locks[dep_id] = lock
        return lock

    @property
    def models_dir(self) -> Path:
        """Public accessor for the models cache root. External callers
        (proxy, admin endpoints) must use this rather than reaching the
        private attribute — keeps the field swappable without grepping
        seven call sites."""
        return self._models_dir

    async def _emit(self, kind: str, **payload) -> None:
        if self._events is not None:
            await self._events.publish(Event(kind=kind, payload=payload))

    def _resolve_target_node_id(self, plan: DeploymentPlan) -> int:
        """Resolve plan.node_label → node_id. Defaults to the local node.

        Validates remote targets are currently `ready` and have a live
        AgentLink — fails fast with a clear message rather than letting
        dispatch error mid-flight. For the local default, a missing
        `nodes` row falls back to 0 so single-node tests / older DBs
        (pre-migration-014) still load."""
        label = (plan.node_label or "local").strip() or "local"
        node = nodes_store.find_by_label(self._conn, label)
        if label == "local":
            return node.id if node is not None else 0
        if node is None:
            raise RuntimeError(f"node {label!r} not found")
        if node.status != "ready":
            raise RuntimeError(
                f"node {label!r} is {node.status!r}; need 'ready' to deploy"
            )
        if self._registry is None or self._registry.get(node.id) is None:
            raise RuntimeError(
                f"node {label!r} has no live AgentLink (agent process not connected)"
            )
        return node.id

    async def load(self, plan: DeploymentPlan):
        async with self._lock:
            target_node_id = self._resolve_target_node_id(plan)
            is_remote = (plan.node_label or "local") not in ("", "local")

            # 1. Ensure model row
            model = model_store.get_by_name(self._conn, plan.model_name)
            if model is None:
                model = model_store.add(
                    self._conn,
                    name=plan.model_name,
                    hf_repo=plan.hf_repo,
                    revision=plan.revision,
                )

            # 2. Ensure weights are local on the LEADER, but only to read
            # config.json for KV/concurrency estimation. The agent will
            # download its own copy when StartDeployment fires. For local
            # deploys, this download IS the on-disk copy the engine
            # container mounts; for remote deploys it's just metadata.
            local_path = model.local_path
            if local_path is None:
                local_path = await download_model_async(
                    hf_repo=plan.hf_repo,
                    revision=plan.revision,
                    cache_dir=self._models_dir,
                )
                model_store.set_local_path(self._conn, model.id, local_path)

            # 3. Resolve target_concurrency (None -> model-size-aware default)
            #    and estimate VRAM.
            if plan.target_concurrency is None:
                target_concurrency = default_target_concurrency(
                    Path(local_path),
                    max_model_len=plan.max_model_len,
                    dtype=plan.dtype,
                )
                log.info(
                    "auto target_concurrency=%d for %s (ctx=%d, dtype=%s)",
                    target_concurrency, plan.model_name, plan.max_model_len, plan.dtype,
                )
            else:
                target_concurrency = plan.target_concurrency
            vram_mb = estimate_vram_mb(KVEstimateInput(
                model_dir=Path(local_path),
                max_model_len=plan.max_model_len,
                target_concurrency=target_concurrency,
                dtype=plan.dtype,
            ))

            # 4. Replace any prior ready deployment of this same model name.
            # CLI contract ("Stops the current model first"): `serve run X`
            # supersedes the existing X. Pinned deployments are excluded
            # from the replace - pin is the operator's commitment that the
            # deployment is special; replacing requires an explicit
            # `serve unpin` first. Doing the cutover after weight prep but
            # before placement keeps the old container live during any HF
            # download and frees its VRAM so placement can reuse the GPU.
            priors = [
                d for d in dep_store.list_ready(self._conn) if d.model_id == model.id
            ]
            for prior in priors:
                if prior.pinned:
                    raise RuntimeError(
                        f"deployment #{prior.id} for {plan.model_name!r} is pinned; "
                        f"run `serve unpin {plan.model_name}` before replacing it"
                    )
            for prior in priors:
                await self._stop_locked(prior.id)

            # 5. Placement (local target only — remote nodes manage their
            # own GPU layout; the operator picks gpu_ids explicitly in
            # plan.gpu_ids for now and we trust it. A future iteration
            # could ask the agent for live GPU stats and run placement
            # against the remote topology.)
            if is_remote:
                gpu_ids = list(plan.gpu_ids)
                if not gpu_ids:
                    raise RuntimeError(
                        "remote deployments require explicit gpu_ids "
                        "(target node's GPU indices)"
                    )
            else:
                if self._topology is None:
                    raise RuntimeError(
                        "topology not initialized; "
                        "pass topology=read_topology() to LifecycleManager"
                    )
                ready = dep_store.list_ready(self._conn)
                # Map id -> LRU rank (lower rank = more evictable). Pinned absent.
                lru_rank = {
                    d.id: idx
                    for idx, d in enumerate(dep_store.list_evictable(self._conn))
                }
                allocated = sorted(
                    [
                        AllocatedDeployment(
                            id=d.id,
                            gpu_ids=d.gpu_ids,
                            vram_reserved_mb=d.vram_reserved_mb,
                            pinned=d.pinned,
                        )
                        for d in ready
                        if d.node_id == 0 or d.node_id == target_node_id
                    ],
                    # Pinned last (never evicted); within auto, LRU rank ascending
                    key=lambda a: (a.pinned, lru_rank.get(a.id, 0)),
                )
                request = PlacementRequest(
                    tensor_parallel=plan.tensor_parallel,
                    vram_reserved_mb=vram_mb,
                    model_name=plan.model_name,
                )
                decision = plan_placement(
                    self._topology, allocated=allocated, request=request,
                )

                if isinstance(decision, NoRoom):
                    raise RuntimeError(decision.reason)
                if isinstance(decision, EvictThenFit):
                    for victim_id in decision.evict_ids:
                        await self._stop_locked(victim_id)
                    gpu_ids = decision.gpu_ids
                else:  # Fit
                    gpu_ids = decision.gpu_ids

            # 5. Create row + spawn container
            dep = dep_store.create(
                self._conn,
                model_id=model.id,
                backend=plan.backend,
                image_tag=plan.image_tag,
                gpu_ids=gpu_ids,
                tensor_parallel=len(gpu_ids),
                max_model_len=plan.max_model_len,
                dtype=plan.dtype,
                pinned=plan.pinned,
                idle_timeout_s=plan.idle_timeout_s,
                vram_reserved_mb=vram_mb,
                max_loras=plan.max_loras,
                max_lora_rank=plan.max_lora_rank,
            )
            # Base pre-warming history. Capture the operator's plan as JSON
            # before the long health-check window so a daemon
            # crash mid-load doesn't lose it. `reached_ready_at` stays NULL
            # until the engine's healthz answers - failed loads must not
            # tempt the predictor into replaying a bad config.
            plan_record_id = plan_store.record(
                self._conn, model_id=model.id, plan=plan, deployment_id=dep.id,
            )
            dep_store.update_status(self._conn, dep.id, "loading")
            await self._emit(
                "deployment.loading",
                dep_id=dep.id,
                model=plan.model_name,
                backend=plan.backend,
            )

            backend = self._backends[plan.backend]
            tp = len(gpu_ids)

            if is_remote:
                # Remote: skip the leader-side gpu_memory_utilization tuning
                # (we don't know the agent's per-GPU MB without asking it).
                # The backend's default util factor + the agent's docker run
                # is enough for the demo. Future: query agent gpu stats.
                mem_util = plan.gpu_memory_utilization
            else:
                # Rebuild plan with the placement-chosen GPU set AND a
                # per-deployment gpu_memory_utilization derived from our
                # reservation. Without this override every vLLM container
                # takes its requested fraction of the *whole* GPU, so two
                # co-located deployments fight for memory and the later
                # one OOMs.
                assert self._topology is not None
                per_gpu_mb = self._topology.gpus[gpu_ids[0]].total_mb
                mem_util = backend.headroom.effective_util(
                    reserved_mb=int(vram_mb / tp),
                    per_gpu_mb=per_gpu_mb,
                )
            effective_plan = replace(
                plan,
                gpu_ids=list(gpu_ids),
                tensor_parallel=tp,
                gpu_memory_utilization=mem_util,
                target_concurrency=target_concurrency,
            )

            # 6. Per-deployment engine YAML. For LOCAL deploys we write
            # it to the leader's configs dir; for REMOTE deploys we
            # ship the YAML body inline in the plan dict so the agent
            # can materialise it on its own host before mounting.
            container_config_path: str | None = None
            engine_config_body: str | None = None
            cfg = backend.engine_config(effective_plan)
            if cfg is not None:
                engine_config_body = yaml.safe_dump(cfg, sort_keys=True)
                if not is_remote:
                    self._configs_dir.mkdir(parents=True, exist_ok=True)
                    host_cfg = self._configs_dir / f"{dep.id}.yml"
                    host_cfg.write_text(engine_config_body)
                container_config_path = f"/serve/configs/{dep.id}.yml"

            container_env = backend.container_env(effective_plan)

            if is_remote:
                # ------------------------------------------------------------
                # Remote dispatch: build a self-contained plan dict the
                # agent can execute with no leader-side filesystem
                # dependencies. The agent downloads the model itself,
                # writes the per-deployment config locally, and runs
                # docker on its own host.
                # ------------------------------------------------------------
                assert self._registry is not None
                link = self._registry.get(target_node_id)
                if link is None:
                    raise RuntimeError(
                        f"node {plan.node_label!r} link disappeared before dispatch"
                    )

                # Container model path will be agent-local. We tell the
                # agent the HF coordinates; it resolves the on-disk path
                # before substituting into argv via a known sentinel.
                MODEL_SENTINEL = "__SERVE_MODEL_PATH__"
                argv = backend.build_argv(
                    effective_plan,
                    local_model_path=MODEL_SENTINEL,
                    config_path=container_config_path,
                )

                # backend.container_kwargs(...) contains docker.types.Ulimit
                # instances that aren't JSON-serialisable; the WS frame
                # encoder needs plain dicts. The agent rehydrates them
                # via _rehydrate_docker_kwargs before calling docker.run.
                agent_plan = {
                    "image": plan.image_tag,
                    "name": f"serve-{plan.backend}-{plan.model_name}-{dep.id}",
                    "command": argv,
                    "environment": container_env,
                    "kwargs": _json_safe_docker_kwargs(
                        backend.container_kwargs(effective_plan),
                    ),
                    "internal_port": backend.internal_port,
                    # Agent-side staging instructions.
                    "model_hf_repo": plan.hf_repo,
                    "model_revision": plan.revision,
                    "model_sentinel": MODEL_SENTINEL,
                    "engine_config_body": engine_config_body,
                    "engine_config_container_path": container_config_path,
                    "deployment_id": dep.id,
                }
                # Any failure dispatching to the remote agent must mark
                # the row failed so it doesn't sit in 'loading' forever
                # — the local path has wait_healthy doing this, the
                # remote path needs its own.
                try:
                    started = await link.start_deployment(agent_plan)
                except Exception as e:
                    msg = f"remote start_deployment failed: {e}"
                    log.exception(
                        "remote deploy %s on node %s failed",
                        dep.id, target_node_id,
                    )
                    dep_store.update_status(
                        self._conn, dep.id, "failed", last_error=msg,
                    )
                    await self._emit(
                        "deployment.failed", dep_id=dep.id, error=msg,
                    )
                    raise RuntimeError(msg) from e
                dep_store.set_container(
                    self._conn, dep.id,
                    container_id=started.container_id,
                    container_name=agent_plan["name"],
                    container_port=started.port,
                    container_address=started.address,
                    node_id=target_node_id,
                )
                # No leader-side wait_healthy for v1: the agent's OpResult
                # is the success signal (its docker.run completed).
                # /v1/* traffic flows through the WS tunnel via _proxy_via_link.
                await self._emit(
                    "deployment.spawned",
                    dep_id=dep.id, container_id=started.container_id,
                )
                dep_store.update_status(self._conn, dep.id, "ready")
                plan_store.mark_ready(self._conn, plan_record_id)
                await self._emit("deployment.ready", dep_id=dep.id)
                return dep_store.get_by_id(self._conn, dep.id)

            # ----------------------------------------------------------------
            # Local dispatch: original docker.run path.
            # ----------------------------------------------------------------
            container_model_path = "/cache/" + str(
                Path(local_path).resolve().relative_to(self._models_dir.resolve())
            )
            volumes = {str(self._models_dir.resolve()): {"bind": "/cache", "mode": "ro"}}
            if container_config_path is not None:
                volumes[str(self._configs_dir.resolve())] = {
                    "bind": "/serve/configs", "mode": "ro",
                }

            argv = backend.build_argv(
                effective_plan,
                local_model_path=container_model_path,
                config_path=container_config_path,
            )

            handle = self._docker.run(
                image=plan.image_tag,
                name=f"serve-{plan.backend}-{plan.model_name}-{dep.id}",
                command=argv,
                environment=container_env,
                kwargs=backend.container_kwargs(effective_plan),
                volumes=volumes,
                internal_port=backend.internal_port,
            )
            # Persist node_id alongside container info. The local node is
            # node_id == target_node_id (the leader is target by default).
            dep_store.set_container(
                self._conn, dep.id,
                container_id=handle.id,
                container_name=handle.name,
                container_port=handle.port,
                container_address=handle.address,
                node_id=target_node_id,
            )
            # Capture the docker image's content-addressable id so the row
            # records what actually ran, not just the tag. Tags are mutable;
            # if upstream retags `vllm/vllm-openai:vX.Y.Z`, the digest is
            # the only reproducible reference. Best-effort: a failure to
            # resolve the id must not block the load.
            try:
                image_digest = self._docker.container_image_id(handle.id)
            except Exception:
                image_digest = None
            if isinstance(image_digest, str) and image_digest:
                dep_store.set_image_digest(self._conn, dep.id, image_digest)
            await self._emit("deployment.spawned", dep_id=dep.id, container_id=handle.id)

            health_url = f"http://{handle.address}:{handle.port}{backend.health_path}"
            ok = await wait_healthy(health_url, timeout_s=self._load_timeout_s)
            if not ok:
                # Leave the failed container around so its logs survive - without
                # them, "engine did not become healthy" is unactionable. The
                # operator can `docker logs <name>` to find the real error, then
                # `serve stop <id>` (which removes the container) when done.
                self._docker.stop(handle.id, timeout=10, remove=False)
                msg = (
                    f"engine did not become healthy within load timeout "
                    f"({health_url}); container {handle.name} preserved for "
                    f"inspection (`docker logs {handle.name}`)"
                )
                dep_store.update_status(self._conn, dep.id, "failed", last_error=msg)
                await self._emit("deployment.failed", dep_id=dep.id, error=msg)
                raise RuntimeError(msg)

            dep_store.update_status(self._conn, dep.id, "ready")
            plan_store.mark_ready(self._conn, plan_record_id)
            await self._emit("deployment.ready", dep_id=dep.id)

            return dep_store.get_by_id(self._conn, dep.id)

    async def stop(self, dep_id: int) -> None:
        async with self._lock:
            await self._stop_locked(dep_id)

    async def pin(self, dep_id: int, pinned: bool = True) -> None:
        dep_store.set_pinned(self._conn, dep_id, pinned)
        await self._emit("deployment.pinned" if pinned else "deployment.unpinned", dep_id=dep_id)

    async def reconcile(self) -> None:
        """At startup: walk ready deployments, verify their containers exist.

        If the daemon crashed between marking 'ready' and (e.g.) the user
        running `serve stop`, the DB row is stale. We can't reliably re-bind
        to a running container (we'd need to repopulate routing tables and
        recompute the host port). Simpler and safer: mark stale rows failed
        and let the user re-load.
        """
        async with self._lock:
            local_node_id = self._local_node_id()
            for d in dep_store.list_all(self._conn):
                if d.status not in dep_store.ACTIVE_STATUSES and d.status != "stopping":
                    continue
                # Remote deployments live on agent hosts; the leader's
                # docker can't see those containers. The agent's own
                # state is the source of truth — when it reconnects,
                # the row is either still valid (link.is_ready) or stale.
                # For now leave remote 'ready' rows untouched on
                # reconcile so a leader restart doesn't kill them.
                if d.node_id != 0 and d.node_id != local_node_id:
                    log.info(
                        "reconcile: deployment %s is on remote node %s; leaving as-is",
                        d.id, d.node_id,
                    )
                    continue
                if d.container_id is None:
                    dep_store.update_status(
                        self._conn, d.id, "failed",
                        last_error=f"daemon found stale {d.status!r} row without container",
                    )
                    continue
                try:
                    container = self._docker._client.containers.get(d.container_id)
                    status = container.status
                except Exception:
                    log.warning(
                        "reconcile: deployment %s container %s missing; marking failed",
                        d.id, d.container_id,
                    )
                    dep_store.update_status(
                        self._conn, d.id, "failed",
                        last_error="container disappeared while daemon was down",
                    )
                    continue
                if status != "running":
                    log.warning(
                        "reconcile: deployment %s container %s status=%s; cleaning",
                        d.id, d.container_id, status,
                    )
                    try:
                        container.remove(force=True)
                    except Exception:
                        pass
                    dep_store.update_status(
                        self._conn, d.id, "failed",
                        last_error=f"container exited (status={status}) while daemon was down",
                    )
                    continue
                if d.status == "stopping":
                    self._docker.stop(d.container_id, timeout=30)
                    dep_store.update_status(self._conn, d.id, "stopped")
                    continue
                if d.status == "loading":
                    backend = self._backends.get(d.backend)
                    if (
                        backend is not None
                        and d.container_address is not None
                        and d.container_port is not None
                    ):
                        health_url = (
                            f"http://{d.container_address}:{d.container_port}"
                            f"{backend.health_path}"
                        )
                        if await wait_healthy(health_url, timeout_s=5.0, interval_s=1.0):
                            dep_store.update_status(self._conn, d.id, "ready")
                            await self._emit("deployment.ready", dep_id=d.id)
                            log.info("reconcile: deployment %s became ready", d.id)
                            continue
                    self._docker.stop(d.container_id, timeout=30)
                    dep_store.update_status(
                        self._conn, d.id, "failed",
                        last_error="daemon restarted while deployment was loading",
                    )
                    continue
                log.info("reconcile: deployment %s re-adopted (%s running)",
                         d.id, d.container_id)

    async def stop_all(self) -> None:
        """Stop every deployment that has not already reached stopped."""
        async with self._lock:
            for d in dep_store.list_all(self._conn):
                if d.status == "stopped":
                    continue
                await self._stop_locked(d.id)

    async def _stop_locked(self, dep_id: int) -> None:
        dep = dep_store.get_by_id(self._conn, dep_id)
        if dep is None:
            return
        dep_store.update_status(self._conn, dep.id, "stopping")
        # Decide whether the container lives on this host or on a remote
        # agent. node_id 0 (or matching the local node) → local docker;
        # otherwise dispatch the stop through the AgentLink for that node.
        local_node_id = self._local_node_id()
        is_remote = (
            dep.node_id != 0
            and dep.node_id != local_node_id
        )
        if dep.container_id:
            if is_remote and self._registry is not None:
                link = self._registry.get(dep.node_id)
                if link is not None and link.is_ready:
                    try:
                        await link.stop_deployment(dep.container_id)
                    except Exception as e:
                        log.warning(
                            "remote stop_deployment failed for dep %s on node %s: %s",
                            dep.id, dep.node_id, e,
                        )
                else:
                    log.warning(
                        "remote node %s offline; can't stop remote container %s; "
                        "row will still be marked stopped",
                        dep.node_id, dep.container_id,
                    )
            else:
                self._docker.stop(dep.container_id, timeout=30)
        da_store.detach_all(self._conn, dep.id)
        # Remove the per-deployment engine config file if one was written
        # locally (remote configs live on the agent host, never here).
        cfg_path = self._configs_dir / f"{dep.id}.yml"
        if cfg_path.exists():
            try:
                cfg_path.unlink()
            except OSError:
                pass
        dep_store.update_status(self._conn, dep.id, "stopped")
        await self._emit("deployment.stopped", dep_id=dep_id)

    def _local_node_id(self) -> int:
        """The leader's own node_id from the DB (0 if the row is missing)."""
        local = nodes_store.find_by_label(self._conn, "local")
        return local.id if local is not None else 0
