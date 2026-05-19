from __future__ import annotations

import asyncio

import typer

from berth import config
from berth.cli import app, ipc


@app.command("pull")
def pull(
    hf_repo: str = typer.Argument(
        ...,
        help="HuggingFace repo id, e.g. meta-llama/Llama-3.2-1B-Instruct",
    ),
    name: str = typer.Option(
        None, "--name", "-n", help="Local name (default: repo basename)",
    ),
    revision: str = typer.Option("main", "--revision"),
    skip_download: bool = typer.Option(
        False, "--skip-download",
        help="Only register; don't fetch weights now (download happens at first run)",
    ),
):
    """Register a model AND download its weights (use --skip-download to defer)."""
    local_name = name or hf_repo.split("/")[-1].lower()

    # Register (idempotent - 409 from daemon means already there, which is fine).
    body = {"name": local_name, "hf_repo": hf_repo, "revision": revision}
    try:
        result = asyncio.run(ipc.post(config.SOCK_PATH, "/admin/models", json=body))
        typer.echo(f"registered: {result['name']} ({result['hf_repo']}@{result['revision']})")
    except RuntimeError as e:
        msg = str(e)
        if "409" in msg:
            typer.echo(f"already registered: {local_name}")
        else:
            typer.echo(f"register failed: {e}", err=True)
            raise typer.Exit(1) from e

    if skip_download:
        typer.echo("(skipping download - weights fetched at first `serve run`)")
        return

    typer.echo(f"downloading {hf_repo}@{revision} ... (this may take a few minutes)")
    try:
        result = asyncio.run(
            ipc.post(config.SOCK_PATH, f"/admin/models/{local_name}/download")
        )
    except RuntimeError as e:
        typer.echo(f"download failed: {e}", err=True)
        raise typer.Exit(1) from e
    if result.get("already_present"):
        typer.echo(f"cached: {result['local_path']}")
    else:
        typer.echo(f"downloaded: {result['local_path']}")
