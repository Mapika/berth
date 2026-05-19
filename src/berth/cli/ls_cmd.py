from __future__ import annotations

import asyncio
import json

import typer

from berth import config
from berth.cli import app, ipc


@app.command("ls")
def ls(json_out: bool = typer.Option(False, "--json")):
    """List registered models."""
    models = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/models"))
    if json_out:
        typer.echo(json.dumps(models, indent=2))
        return
    if not models:
        typer.echo("no models registered. Use `berth pull <hf-repo>` to add one.")
        return
    typer.echo(f"{'NAME':<30} {'REPO':<50} {'REVISION':<10}")
    for m in models:
        typer.echo(f"{m['name']:<30} {m['hf_repo']:<50} {m['revision']:<10}")
