from __future__ import annotations

import asyncio

import typer

from berth import config
from berth.cli import app, ipc


@app.command("run")
def run(
    name_or_repo: str = typer.Argument(
        ...,
        help="Model name (if registered) or HF repo",
    ),
    gpu: str = typer.Option("0", "--gpu", help="Comma-separated GPU ids, e.g. '0' or '0,1'"),
    max_model_len: int = typer.Option(8192, "--ctx"),
    dtype: str = typer.Option("auto"),
    image_tag: str = typer.Option(None, "--image", help="Override engine image tag"),
    pin: bool = typer.Option(False, "--pin", help="Make this deployment unevictable"),
    idle_timeout_s: int = typer.Option(
        None, "--idle-timeout",
        help="Seconds idle before auto-eviction (default: server config)",
    ),
    engine: str = typer.Option(
        None, "--engine",
        help="Force a specific engine (vllm | sglang | trtllm). Default: auto-select.",
    ),
    kv_cache_dtype: str = typer.Option(
        None, "--kv-cache",
        help="KV cache dtype passed to engine (e.g. 'fp8_e4m3'). "
             "Shorthand for --extra '--kv-cache-dtype=...'.",
    ),
    max_seqs: int = typer.Option(
        None, "--max-seqs",
        help="Max concurrent decode sequences. Drives vLLM --max-num-seqs "
             "and SGLang --max-running-requests, and feeds the KV-pool "
             "sizing. Lower this for hybrid/Mamba architectures where each "
             "decode sequence consumes a state-cache block.",
    ),
    extra: list[str] = typer.Option(
        [], "--extra", "-x",
        help="Raw engine flag, e.g. -x '--reasoning-parser=qwen3' "
             "or -x '--enable-expert-parallel'. Repeatable.",
    ),
    max_loras: int = typer.Option(
        0, "--max-loras",
        help="Number of LoRA adapter slots to reserve in this deployment "
             "(enables --enable-lora on vLLM, equivalent on SGLang). 0 = "
             "adapters disabled. Adapters loaded later via `serve adapter load`.",
    ),
    node: str = typer.Option(
        None, "--node",
        help="Target node label. Default: leader host. Other values must "
             "match an enrolled, currently-ready agent (see `serve nodes ls`). "
             "When set, the engine container runs on the remote agent's host.",
    ),
):
    """Load a model and make it active. Replaces any existing deployment
    of the same name (errors if that deployment is pinned)."""
    gpu_ids = [int(g) for g in gpu.split(",") if g.strip()]
    extra_args: dict[str, str] = {}
    for raw in extra:
        s = raw.strip()
        if not s.startswith("--"):
            typer.echo(
                f"--extra value {raw!r} must start with '--' "
                "(e.g. --extra '--kv-cache-dtype=fp8_e4m3')",
                err=True,
            )
            raise typer.Exit(1)
        if "=" in s:
            k, v = s.split("=", 1)
            extra_args[k] = v
        else:
            extra_args[s] = ""
    if kv_cache_dtype is not None:
        extra_args["--kv-cache-dtype"] = kv_cache_dtype
    if "/" in name_or_repo:
        hf_repo = name_or_repo
        local_name = hf_repo.split("/")[-1].lower()
    else:
        local_name = name_or_repo
        models = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/models"))
        if not any(m["name"] == local_name for m in models):
            typer.echo(
                f"model {local_name!r} not registered. "
                "Use `serve pull <hf-repo>` first.",
                err=True,
            )
            raise typer.Exit(1)
        match = next(m for m in models if m["name"] == local_name)
        hf_repo = match["hf_repo"]

    body = {
        "model_name": local_name,
        "hf_repo": hf_repo,
        "gpu_ids": gpu_ids,
        "max_model_len": max_model_len,
        "dtype": dtype,
    }
    body["pinned"] = pin
    if idle_timeout_s is not None:
        body["idle_timeout_s"] = idle_timeout_s
    if image_tag is not None:
        body["image_tag"] = image_tag
    if engine is not None:
        body["backend"] = engine
    if max_seqs is not None:
        body["target_concurrency"] = max_seqs
    if max_loras > 0:
        body["max_loras"] = max_loras
    if extra_args:
        body["extra_args"] = extra_args
    if node:
        body["node_label"] = node

    target = f"node={node!r}" if node and node != "local" else "leader"
    typer.echo(f"loading {local_name} on {target} GPU(s) {gpu_ids} ...")
    try:
        result = asyncio.run(ipc.post(config.SOCK_PATH, "/admin/deployments", json=body))
    except RuntimeError as e:
        typer.echo(f"load failed: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"ready: deployment #{result['id']} ({result['container_name']})")
