from __future__ import annotations

import asyncio

import typer

from serve_engine import config
from serve_engine.cli import app, ipc
from serve_engine.cli.daemon_cmd import spawn_daemon
from serve_engine.doctor.runner import run_all, summarise


@app.command("setup")
def setup():
    """First-run wizard: doctor, start daemon, create admin key, print URL."""
    typer.echo("=== serve-engine setup ===")
    typer.echo()
    typer.echo("Step 1: environment diagnostic")
    results = run_all()
    _, _, fail = summarise(results)
    labels = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    for r in results:
        typer.echo(f"  {labels[r.status]:<4} {r.name}: {r.detail}")
    if fail:
        typer.secho(
            "\nFAIL doctor reports failures; fix and re-run `serve setup`.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)

    typer.echo()
    typer.echo("Step 2: starting daemon")
    cfg = config.resolve_config()
    try:
        asyncio.run(ipc.get(config.SOCK_PATH, "/healthz"))
        typer.echo("  daemon already running")
    except Exception:
        try:
            pid = spawn_daemon(cfg, timeout_s=15.0, poll_s=0.3)
        except TimeoutError as e:
            typer.secho(f"  {e}; check logs", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from e
        typer.echo(f"  daemon started (pid {pid})")

    typer.echo()
    typer.echo("Step 3: create admin key")
    label = typer.prompt("Key label", default="admin")
    body = {"name": label, "tier": "admin"}
    result = asyncio.run(ipc.post(config.SOCK_PATH, "/admin/keys", json=body))
    typer.echo(f"  id:     {result['id']}")
    typer.echo(f"  secret: {result['secret']}")
    typer.echo()
    typer.echo("Save this secret. It won't be shown again.")
    typer.echo()
    typer.secho(
        f"Done. Open {cfg.public_url}/ and paste the secret.",
        fg=typer.colors.GREEN,
    )
