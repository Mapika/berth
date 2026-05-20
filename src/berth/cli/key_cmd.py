from __future__ import annotations

import asyncio
import json

import typer

from berth import config
from berth.cli import app, ipc

key_app = typer.Typer(help="API key management")
app.add_typer(key_app, name="key")


@key_app.command("create")
def create(
    name: str = typer.Argument(..., help="Human-readable label"),
    tier: str = typer.Option("standard", "--tier"),
):
    """Create a new API key. The secret is only printed once."""
    body = {"name": name, "tier": tier}
    result = asyncio.run(ipc.post(config.SOCK_PATH, "/admin/keys", json=body))
    typer.echo(f"id:     {result['id']}")
    typer.echo(f"name:   {result['name']}")
    typer.echo(f"tier:   {result['tier']}")
    typer.echo(f"secret: {result['secret']}")
    typer.echo("(save this secret now; it won't be shown again)")


@key_app.command("list")
def list_keys(json_out: bool = typer.Option(False, "--json")):
    """List API keys. Secrets are never shown."""
    keys = asyncio.run(ipc.get(config.SOCK_PATH, "/admin/keys"))
    if json_out:
        typer.echo(json.dumps(keys, indent=2))
        return
    if not keys:
        typer.echo("no keys (create the first key over the local control socket)")
        return
    typer.echo(f"{'ID':<4} {'NAME':<20} {'TIER':<10} {'PREFIX':<14} {'REVOKED':<8}")
    for k in keys:
        revoked = "yes" if k.get("revoked") else "-"
        typer.echo(
            f"{k['id']:<4} {k['name']:<20} {k['tier']:<10} "
            f"{k['prefix']:<14} {revoked:<8}"
        )


@key_app.command("revoke")
def revoke(key_id: int = typer.Argument(...)):
    """Revoke an API key by id."""
    asyncio.run(ipc.delete(config.SOCK_PATH, f"/admin/keys/{key_id}"))
    typer.echo(f"revoked key #{key_id}")
