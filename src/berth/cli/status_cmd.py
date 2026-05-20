from __future__ import annotations

import asyncio
import shutil
import subprocess  # nosec

import typer

from berth import config
from berth.cli import app, ipc


def _systemd_state() -> str | None:
    if shutil.which("systemctl") is None:
        return None
    result = subprocess.run(  # nosec
        ["systemctl", "is-active", "berth"],
        check=False,
        capture_output=True,
        text=True,
    )
    state = result.stdout.strip()
    if state in {"", "unknown"}:
        return None
    return state


@app.command("status")
def status():
    """Show daemon health for service and foreground installs."""
    service_state = _systemd_state()
    try:
        body = asyncio.run(ipc.get(config.SOCK_PATH, "/healthz"))
    except Exception as e:
        if service_state:
            typer.echo(f"service: {service_state}")
        typer.echo(f"daemon : unreachable ({e})", err=True)
        raise typer.Exit(1) from e
    if service_state:
        typer.echo(f"service: {service_state}")
    typer.echo(f"daemon : running ({body})")
