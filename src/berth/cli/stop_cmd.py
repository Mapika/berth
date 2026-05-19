from __future__ import annotations

import asyncio

import typer

from berth import config
from berth.cli import app, ipc


@app.command("stop")
def stop(
    dep_id: int = typer.Argument(None, help="Deployment id (default: stop all)"),
):
    """Stop a deployment by id, or all if no id is given."""
    if dep_id is None:
        asyncio.run(ipc.delete(config.SOCK_PATH, "/admin/deployments"))
        typer.echo("all deployments stopped")
    else:
        asyncio.run(ipc.delete(config.SOCK_PATH, f"/admin/deployments/{dep_id}"))
        typer.echo(f"stopped deployment #{dep_id}")
