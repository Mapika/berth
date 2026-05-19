from __future__ import annotations

import asyncio
import json

import typer

from berth import config
from berth.cli import app, ipc


@app.command("ps")
def ps(json_out: bool = typer.Option(False, "--json")):
    """List deployments and their status."""
    deps = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/deployments"))
    if json_out:
        typer.echo(json.dumps(deps, indent=2))
        return
    if not deps:
        typer.echo("no deployments")
        return
    typer.echo(
        f"{'ID':<4} {'STATUS':<10} {'PIN':<4} {'BACKEND':<8} {'GPUs':<10} "
        f"{'VRAM(MB)':<10} {'CONTAINER':<30}"
    )
    for d in deps:
        pin_mark = "*" if d.get("pinned") else "-"
        typer.echo(
            f"{d['id']:<4} {d['status']:<10} {pin_mark:<4} {d['backend']:<8} "
            f"{','.join(str(g) for g in d['gpu_ids']):<10} "
            f"{d.get('vram_reserved_mb', 0):<10} "
            f"{d.get('container_name') or '-':<30}"
        )
