from __future__ import annotations

import asyncio

import httpx
import typer

from berth import config
from berth.cli import app


@app.command("logs")
def logs(follow: bool = typer.Option(True, "--follow/--no-follow", "-f")):
    """Stream logs from the currently active deployment's engine container."""
    async def run():
        transport = httpx.AsyncHTTPTransport(uds=str(config.SOCK_PATH))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://daemon", timeout=None
        ) as c:
            try:
                async with c.stream("GET", "/admin/deployments/current/logs") as r:
                    if r.status_code != 200:
                        typer.echo(await r.aread(), err=True)
                        raise typer.Exit(1)
                    async for chunk in r.aiter_raw():
                        typer.echo(chunk.decode(errors="replace"), nl=False)
            except KeyboardInterrupt:
                pass
    asyncio.run(run())
