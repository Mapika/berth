from __future__ import annotations

import asyncio
import json

import httpx
import typer
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table

from berth import config
from berth.cli import app


@app.command("top")
def top(refresh_s: float = typer.Option(1.0, "--refresh", "-r")):
    """Live view of deployments, GPUs, and recent events."""
    console = Console()
    asyncio.run(_run(console, refresh_s))


async def _run(console: Console, refresh_s: float) -> None:
    transport = httpx.AsyncHTTPTransport(uds=str(config.SOCK_PATH))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://daemon", timeout=None,
    ) as c:
        last_events: list[dict] = []

        async def consume_events():
            try:
                async with c.stream("GET", "/admin/events") as r:
                    async for line in r.aiter_lines():
                        if line.startswith("data:"):
                            try:
                                obj = json.loads(line[len("data:"):].strip())
                                last_events.append(obj)
                                if len(last_events) > 5:
                                    last_events.pop(0)
                            except json.JSONDecodeError:
                                pass
            except Exception:
                pass

        events_task = asyncio.create_task(consume_events())
        try:
            with Live(refresh_per_second=4, console=console, screen=False) as live:
                while True:
                    try:
                        deps_r = await c.get("/admin/deployments")
                        gpus_r = await c.get("/admin/gpus")
                        deps = deps_r.json() if deps_r.status_code == 200 else []
                        gpus = gpus_r.json() if gpus_r.status_code == 200 else []
                    except httpx.HTTPError as e:
                        console.print(f"daemon unreachable: {e}", style="red")
                        await asyncio.sleep(refresh_s)
                        continue

                    live.update(_render(deps, gpus, last_events))
                    await asyncio.sleep(refresh_s)
        finally:
            events_task.cancel()


def _render(deps: list[dict], gpus: list[dict], events: list[dict]):
    dep_table = Table(title="Deployments", show_lines=False)
    for col in ("ID", "STATUS", "PIN", "BACKEND", "GPUs", "VRAM(MB)", "CONTAINER"):
        dep_table.add_column(col)
    for d in deps:
        dep_table.add_row(
            str(d["id"]),
            d["status"],
            "*" if d.get("pinned") else "-",
            d["backend"],
            ",".join(str(g) for g in d.get("gpu_ids", [])),
            str(d.get("vram_reserved_mb", 0)),
            d.get("container_name") or "-",
        )
    gpu_table = Table(title="GPUs", show_lines=False)
    for col in ("INDEX", "MEM USED/TOTAL (MB)", "UTIL %", "POWER W"):
        gpu_table.add_column(col)
    for g in gpus:
        gpu_table.add_row(
            str(g["index"]),
            f"{g['memory_used_mb']}/{g['memory_total_mb']}",
            str(g["gpu_util_pct"]),
            str(g["power_w"]),
        )
    ev_table = Table(title="Recent events", show_lines=False)
    for col in ("TS", "KIND", "PAYLOAD"):
        ev_table.add_column(col)
    for e in events[-5:]:
        ts = e.get("ts", "")[-15:]
        ev_table.add_row(ts, e.get("kind", ""), json.dumps(e.get("payload", {})))
    return Group(dep_table, gpu_table, ev_table)
