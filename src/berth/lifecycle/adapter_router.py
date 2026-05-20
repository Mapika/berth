"""Resolution and dispatch helpers for adapter-aware OpenAI requests.

Two pure-resolution functions (`resolve_target`, `find_deployment_for`) and
one async helper (`ensure_adapter_loaded`) that handles the
hot-load-when-needed case the proxy hits on a request for an adapter
that's not yet in any deployment.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from berth.backends.base import Backend
from berth.routing.scorer import (
    DeploymentCandidate,
    NodeSignals,
    RoutingRequest,
    default_scorer,
)
from berth.store import adapters as ad_store
from berth.store import deployment_adapters as da_store
from berth.store import deployments as dep_store

# vLLM and SGLang both default --max-lora-rank to 16 when the flag isn't
# passed. We use this for the pre-flight rank check so a too-large adapter
# is caught even when the operator forgot to set --max-lora-rank.
_DEFAULT_ENGINE_MAX_LORA_RANK = 16


@dataclass(frozen=True)
class ResolvedTarget:
    """The result of looking up an OpenAI `model` field.

    `adapter_name`:
        - None: bare base-model request, route as v1.
        - str: adapter request; the proxy must pick a deployment that has
          (or can have) this adapter loaded.
    `base_model_name`: the underlying base model's name (always set).
    """

    base_model_name: str
    adapter_name: str | None


class UnknownModel(Exception):
    pass


def resolve_target(conn: sqlite3.Connection, model_field: str) -> ResolvedTarget:
    """Resolve an OpenAI `model` field to (base, adapter|None).

    Three forms accepted:
    1. `base_name:adapter_name` - explicit composite. Base must match the
       adapter's registered base; otherwise raises UnknownModel.
    2. `adapter_name` - bare. Looked up in adapters first; resolves to
       (adapter.base_model.name, adapter.name).
    3. `base_name` - bare. Looked up in models. If neither matches, raises.
    """
    # Form 1: composite
    if ":" in model_field:
        base_name, adapter_name = model_field.split(":", 1)
        a = ad_store.get_by_name(conn, adapter_name)
        if a is None:
            raise UnknownModel(f"adapter {adapter_name!r} not registered")
        if a.base_model.name != base_name:
            raise UnknownModel(
                f"adapter {adapter_name!r} belongs to base "
                f"{a.base_model.name!r}, not {base_name!r}"
            )
        return ResolvedTarget(base_model_name=base_name, adapter_name=adapter_name)

    # Form 2: bare adapter name
    a = ad_store.get_by_name(conn, model_field)
    if a is not None:
        return ResolvedTarget(
            base_model_name=a.base_model.name, adapter_name=a.name,
        )

    # Form 3: bare base name (unchanged from v1)
    return ResolvedTarget(base_model_name=model_field, adapter_name=None)


def _filter_by_reachable_nodes(candidates, registry):
    """Drop deployment rows whose node_id has no live, ready AgentLink.

    Used by router-layer code to refuse routing to deployments on
    unreachable nodes (heartbeat stale, agent disconnected). When the
    registry is None, the filter is a no-op — preserves single-node
    behavior for tests that don't construct a cluster.
    """
    if registry is None:
        return list(candidates)
    out = []
    for dep in candidates:
        link = registry.get(dep.node_id)
        if link is not None and link.is_ready:
            out.append(dep)
    return out


def _find_deployment_without_signals(
    conn: sqlite3.Connection,
    base_model_name: str,
    adapter_name: str | None,
) -> dep_store.Deployment | None:
    """Pre-scorer body of find_deployment_for for callers without node signals."""
    if adapter_name is None:
        return dep_store.find_ready_by_model_name(conn, base_model_name)

    a = ad_store.get_by_name(conn, adapter_name)
    if a is None:
        return None

    candidates: list[tuple[int, dep_store.Deployment]] = []
    for d in dep_store.list_ready(conn):
        if d.model_id != a.base_model.id:
            continue
        if d.max_loras <= 0:
            continue
        loaded_into = da_store.find_deployments_with_adapter(conn, a.id)
        already_loaded = d.id in loaded_into
        if already_loaded:
            candidates.append((0, d))
        else:
            count = da_store.count_for_deployment(conn, d.id)
            if count < d.max_loras:
                candidates.append((1, d))
            else:
                candidates.append((2, d))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def rank_deployments_for(
    conn: sqlite3.Connection,
    base_model_name: str,
    adapter_name: str | None,
    *,
    signals_by_node: dict[int, NodeSignals],
    request: RoutingRequest,
) -> list[dep_store.Deployment]:
    """Return ready deployments for (base, adapter) ranked best-first.

    For adapter requests, the existing adapter-affinity tiers (already-
    loaded > free-slot > needs-evict) act as a prefilter — only the best
    tier is scored further. Within a tier, the node-level scorer chooses
    the order. We don't mix tiers because that would hide cold-load
    latency behind a fast idle node.
    """
    if adapter_name is None:
        base = model_store_get_by_name(conn, base_model_name)
        if base is None:
            return []
        ready = [
            d for d in dep_store.list_ready(conn)
            if d.model_id == base.id
        ]
    else:
        a = ad_store.get_by_name(conn, adapter_name)
        if a is None:
            return []
        tiered: list[tuple[int, dep_store.Deployment]] = []
        for d in dep_store.list_ready(conn):
            if d.model_id != a.base_model.id:
                continue
            if d.max_loras <= 0:
                continue
            loaded_into = da_store.find_deployments_with_adapter(conn, a.id)
            if d.id in loaded_into:
                tiered.append((0, d))
            else:
                count = da_store.count_for_deployment(conn, d.id)
                if count < d.max_loras:
                    tiered.append((1, d))
                else:
                    tiered.append((2, d))
        if not tiered:
            return []
        best_tier = min(t[0] for t in tiered)
        ready = [d for t, d in tiered if t == best_tier]

    if not ready:
        return []

    candidates = [
        DeploymentCandidate(
            deployment_id=d.id,
            node_id=d.node_id,
            # These are already-running deployments. Their reserved VRAM has
            # already been consumed by the engine, and vLLM often keeps most
            # of that allocation for KV cache. Treating it as a new memory
            # requirement here double-counts and can filter out the only
            # healthy deployment.
            model_required_mb=0,
        )
        for d in ready
    ]
    scored = default_scorer(
        candidates=candidates,
        signals_by_node=signals_by_node,
        request=request,
    )
    by_id = {d.id: d for d in ready}
    return [by_id[c.deployment_id] for c in scored]


def find_deployment_for(
    conn: sqlite3.Connection,
    base_model_name: str,
    adapter_name: str | None,
    *,
    signals_by_node: dict[int, NodeSignals] | None = None,
    request: RoutingRequest | None = None,
) -> dep_store.Deployment | None:
    """Return the best ready deployment, using node-aware scoring when available."""
    if signals_by_node is None:
        return _find_deployment_without_signals(conn, base_model_name, adapter_name)
    ranked = rank_deployments_for(
        conn, base_model_name, adapter_name,
        signals_by_node=signals_by_node,
        request=request or RoutingRequest(affinity_key=None),
    )
    return ranked[0] if ranked else None


def model_store_get_by_name(conn, name):
    """Lazy import to avoid a circular dep at module import time."""
    from berth.store import models as _model_store
    return _model_store.get_by_name(conn, name)


async def ensure_adapter_loaded(
    conn: sqlite3.Connection,
    backend: Backend,
    deployment: dep_store.Deployment,
    adapter_name: str,
    *,
    models_dir: Path,
) -> bool:
    """If `adapter_name` isn't already loaded into `deployment`, hot-load it
    (evicting the LRU adapter if slots are full). Engine is contacted via
    its dynamic load/unload HTTP endpoints. Junction-row state is updated
    on success. Touches last_used_at if already loaded.

    Returns True if a load was triggered (the request paid hot-load
    latency, ~100-500ms), False if the adapter was already loaded.
    The predictor uses this signal as `cold_loaded`."""
    a = ad_store.get_by_name(conn, adapter_name)
    if a is None:
        raise UnknownModel(f"adapter {adapter_name!r} not registered")
    if a.local_path is None:
        raise UnknownModel(
            f"adapter {adapter_name!r} not downloaded; "
            f"POST /admin/adapters/{adapter_name}/download first"
        )

    loaded_ids = da_store.find_deployments_with_adapter(conn, a.id)
    if deployment.id in loaded_ids:
        da_store.touch(conn, deployment.id, a.id)
        return False

    # Pre-flight: catch rank mismatches before the engine produces a
    # cryptic 500 on /v1/load_lora_adapter. When the deployment doesn't
    # set max_lora_rank explicitly, fall back to the engine's default
    # (16 for vLLM/SGLang).
    if a.lora_rank is not None:
        effective_max = deployment.max_lora_rank or _DEFAULT_ENGINE_MAX_LORA_RANK
        if a.lora_rank > effective_max:
            raise RuntimeError(
                f"adapter {a.name!r} has lora_rank={a.lora_rank} but "
                f"deployment #{deployment.id} was started with "
                f"max-lora-rank={effective_max}; "
                f"restart the deployment with "
                f"-x '--max-lora-rank={a.lora_rank}' (or higher)"
            )

    # Need to load. Evict LRU if slots full.
    if da_store.count_for_deployment(conn, deployment.id) >= deployment.max_loras:
        victim = da_store.lru_for_deployment(conn, deployment.id)
        if victim is not None and victim.id != a.id:
            await _engine_unload(backend, deployment, victim.name)
            da_store.detach(conn, deployment.id, victim.id)

    try:
        rel_path = Path(a.local_path).resolve().relative_to(models_dir.resolve())
    except ValueError as e:
        raise RuntimeError(
            f"adapter {a.name!r} local_path is outside the model cache; "
            "re-register the adapter"
        ) from e
    container_path = "/cache/" + str(rel_path)
    await _engine_load(backend, deployment, a.name, container_path)
    da_store.attach(conn, deployment.id, a.id)
    return True


async def _engine_load(
    backend: Backend,
    dep: dep_store.Deployment,
    adapter_name: str,
    container_path: str,
) -> None:
    if dep.container_address == "tunnel":
        # Remote deployment — adapter hot-load would need the adapter file
        # shipped to the agent first (the container_path above is the
        # leader's mount point) and the load call routed via proxy_request.
        # That isn't implemented; refuse loudly rather than blind-dialing the
        # agent-supplied address.
        raise RuntimeError(
            "adapter hot-load is not supported on remote deployments "
            f"(deployment {dep.id} runs on a remote node)"
        )
    url = (
        f"http://{dep.container_address}:{dep.container_port}"
        f"{backend.adapter_load_path}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            url, json={"lora_name": adapter_name, "lora_path": container_path},
        )
    if r.status_code >= 400:
        raise RuntimeError(
            f"engine adapter load returned {r.status_code}: {r.text[:200]}"
        )


async def _engine_unload(
    backend: Backend,
    dep: dep_store.Deployment,
    adapter_name: str,
) -> None:
    if dep.container_address == "tunnel":
        return  # remote — no direct-dial unload path
    url = (
        f"http://{dep.container_address}:{dep.container_port}"
        f"{backend.adapter_unload_path}"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json={"lora_name": adapter_name})
    except httpx.HTTPError:
        return  # engine gone; let caller proceed with detach
    if r.status_code >= 500:
        raise RuntimeError(
            f"engine adapter unload returned {r.status_code}: {r.text[:200]}"
        )
