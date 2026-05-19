"""Register, download, load, and unload LoRA adapters."""
from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from berth import config
from berth.cli import app, ipc

adapter_app = typer.Typer(help="LoRA adapter management")
app.add_typer(adapter_app, name="adapter")


@adapter_app.command("pull")
def adapter_pull(
    hf_repo: str = typer.Argument(
        ...,
        help="HuggingFace repo id of the adapter, e.g. user/llama3-finetune-lora",
    ),
    base: str = typer.Option(
        ..., "--base",
        help="Name of the base model this adapter applies to (must be registered)",
    ),
    name: str = typer.Option(
        None, "--name", "-n",
        help="Local adapter name (default: repo basename)",
    ),
    revision: str = typer.Option("main", "--revision"),
    skip_download: bool = typer.Option(
        False, "--skip-download",
        help="Only register; defer the blob download until first hot-load",
    ),
):
    """Register an adapter AND download its weights from HuggingFace."""
    local_name = name or hf_repo.split("/")[-1].lower()
    body = {
        "name": local_name, "base_model_name": base,
        "hf_repo": hf_repo, "revision": revision,
    }
    try:
        result = asyncio.run(ipc.post(config.SOCK_PATH, "/admin/adapters", json=body))
        typer.echo(
            f"registered: {result['name']} (base={result['base']}, "
            f"{result['hf_repo']}@{result['revision']})"
        )
    except RuntimeError as e:
        msg = str(e)
        if "409" in msg:
            typer.echo(f"already registered: {local_name}")
        elif "404" in msg:
            typer.echo(f"register failed: {e}", err=True)
            raise typer.Exit(1) from e
        else:
            typer.echo(f"register failed: {e}", err=True)
            raise typer.Exit(1) from e

    if skip_download:
        typer.echo("(skipping download)")
        return

    typer.echo(f"downloading {hf_repo}@{revision} ...")
    try:
        result = asyncio.run(
            ipc.post(config.SOCK_PATH, f"/admin/adapters/{local_name}/download")
        )
    except RuntimeError as e:
        typer.echo(f"download failed: {e}", err=True)
        raise typer.Exit(1) from e
    state = "cached" if result.get("already_present") else "downloaded"
    rank = result.get("lora_rank")
    rank_suffix = f", lora_rank={rank}" if rank is not None else ""
    typer.echo(f"{state}: {result['local_path']} ({result['size_mb']} MB{rank_suffix})")
    if rank is not None and rank > 16:
        typer.echo(
            f"note: adapter has lora_rank={rank}; the target deployment "
            f"must be started with -x '--max-lora-rank={rank}' (or higher) "
            f"because vLLM/SGLang default to 16."
        )


@adapter_app.command("add")
def adapter_add(
    local_path: str = typer.Argument(
        ...,
        help="Directory containing the adapter weights + adapter_config.json",
    ),
    base: str = typer.Option(
        ..., "--base",
        help="Name of the base model this adapter applies to (must be registered)",
    ),
    name: str = typer.Option(
        None, "--name", "-n",
        help="Local adapter name (default: basename of local_path)",
    ),
):
    """Register a pre-downloaded LoRA adapter from a local directory.

    Counterpart to `pull` for adapters that aren't on HuggingFace. The
    directory is copied into the managed cache so the engine container
    can see it; the source is left untouched.
    """
    src = Path(local_path).expanduser().resolve()
    local_name = name or src.name.lower()
    body = {
        "name": local_name,
        "base_model_name": base,
        "local_path": str(src),
    }
    try:
        result = asyncio.run(
            ipc.post(config.SOCK_PATH, "/admin/adapters/local", json=body)
        )
    except RuntimeError as e:
        typer.echo(f"add failed: {e}", err=True)
        raise typer.Exit(1) from e
    rank = result.get("lora_rank")
    rank_suffix = f", lora_rank={rank}" if rank is not None else ""
    typer.echo(
        f"registered: {result['name']} (base={result['base']}, "
        f"{result['size_mb']} MB{rank_suffix})"
    )
    typer.echo(f"cached at: {result['local_path']}")
    if rank is not None and rank > 16:
        typer.echo(
            f"note: adapter has lora_rank={rank}; the target deployment "
            f"must be started with -x '--max-lora-rank={rank}' (or higher) "
            f"because vLLM/SGLang default to 16."
        )


@adapter_app.command("ls")
def adapter_ls():
    """List registered adapters."""
    rows = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/adapters"))
    if not rows:
        typer.echo("no adapters registered")
        return
    typer.echo(
        f"{'NAME':<28}{'BASE':<24}{'SIZE_MB':<10}{'RANK':<6}"
        f"{'LOADED_INTO':<14}{'DOWNLOADED'}"
    )
    for r in rows:
        loaded = ",".join(str(x) for x in r["loaded_into"]) or "-"
        size = str(r["size_mb"]) if r["size_mb"] is not None else "-"
        rank = str(r.get("lora_rank")) if r.get("lora_rank") is not None else "-"
        downloaded = "yes" if r["downloaded"] else "no"
        typer.echo(
            f"{r['name']:<28}{r['base']:<24}{size:<10}{rank:<6}"
            f"{loaded:<14}{downloaded}"
        )


@adapter_app.command("rm")
def adapter_rm(
    name: str = typer.Argument(...),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Hot-unload from any deployments first if loaded.",
    ),
):
    """Remove an adapter from the registry. Refuses if loaded unless --force."""
    suffix = "?force=true" if force else ""
    try:
        asyncio.run(ipc.delete(config.SOCK_PATH, f"/admin/adapters/{name}{suffix}"))
        typer.echo(f"removed: {name}")
    except RuntimeError as e:
        typer.echo(f"remove failed: {e}", err=True)
        raise typer.Exit(1) from e


def _pick_default_deployment(base_name: str) -> int:
    """Choose a ready deployment of `base_name` for adapter ops when the
    user didn't pass --deployment. Prefers the most-recent ready row."""
    deps = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/deployments"))
    models = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/models"))
    base = next((m for m in models if m["name"] == base_name), None)
    if base is None:
        typer.echo(f"base model {base_name!r} not registered", err=True)
        raise typer.Exit(1)
    candidates = [
        d for d in deps
        if d["status"] == "ready" and d["model_id"] == base["id"]
    ]
    if not candidates:
        typer.echo(
            f"no ready deployment for base {base_name!r}; "
            f"`serve run {base_name} --max-loras N` first",
            err=True,
        )
        raise typer.Exit(1)
    candidates.sort(key=lambda d: d.get("started_at") or "", reverse=True)
    return candidates[0]["id"]


@adapter_app.command("load")
def adapter_load(
    name: str = typer.Argument(...),
    deployment: int = typer.Option(
        None, "--deployment", "-d",
        help="Deployment id to load into. Default: most recent ready "
             "deployment of the adapter's base.",
    ),
):
    """Hot-load an adapter into a running deployment of its base."""
    if deployment is None:
        adapters = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/adapters"))
        a = next((x for x in adapters if x["name"] == name), None)
        if a is None:
            typer.echo(f"adapter {name!r} not registered", err=True)
            raise typer.Exit(1)
        deployment = _pick_default_deployment(a["base"])
    try:
        result = asyncio.run(
            ipc.post(
                config.SOCK_PATH,
                f"/admin/deployments/{deployment}/adapters/{name}",
            )
        )
    except RuntimeError as e:
        typer.echo(f"load failed: {e}", err=True)
        raise typer.Exit(1) from e
    msg = f"loaded {name} into deployment {deployment}"
    if result.get("evicted"):
        msg += f" (evicted {result['evicted']})"
    typer.echo(msg)


@adapter_app.command("unload")
def adapter_unload(
    name: str = typer.Argument(...),
    deployment: int = typer.Option(
        None, "--deployment", "-d",
        help="Deployment id to unload from. Default: any deployment with "
             "this adapter loaded.",
    ),
):
    """Hot-unload an adapter from a deployment."""
    if deployment is None:
        adapters = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/adapters"))
        a = next((x for x in adapters if x["name"] == name), None)
        if a is None:
            typer.echo(f"adapter {name!r} not registered", err=True)
            raise typer.Exit(1)
        if not a["loaded_into"]:
            typer.echo(f"adapter {name!r} is not currently loaded anywhere")
            return
        deployment = a["loaded_into"][0]
    try:
        asyncio.run(
            ipc.delete(
                config.SOCK_PATH,
                f"/admin/deployments/{deployment}/adapters/{name}",
            )
        )
    except RuntimeError as e:
        typer.echo(f"unload failed: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"unloaded {name} from deployment {deployment}")
