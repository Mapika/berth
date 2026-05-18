from __future__ import annotations

import os

import httpx
import typer

from serve_engine.cli import app

nodes_app = typer.Typer(help="Manage cluster nodes from the leader.")
app.add_typer(nodes_app, name="nodes")


def _auth_headers() -> dict[str, str]:
    tok = os.environ.get("SERVE_TOKEN")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _base() -> str:
    return os.environ.get("SERVE_URL", "http://127.0.0.1:11500").rstrip("/")


@nodes_app.command("ls")
def list_nodes():
    """List nodes — status, GPU count, total VRAM, agent version."""
    r = httpx.get(f"{_base()}/admin/nodes",
                  headers=_auth_headers(), timeout=10.0)
    r.raise_for_status()
    rows = r.json()["nodes"]
    typer.echo(
        f"{'ID':<4} {'LABEL':<20} {'STATUS':<14} "
        f"{'GPUs':<5} {'VRAM MB':>10}  VERSION"
    )
    for n in rows:
        typer.echo(
            f"{n['id']:<4} {n['label']:<20} {n['status']:<14} "
            f"{n['gpu_count']:<5} {n['total_vram_mb']:>10}  "
            f"{n.get('agent_version') or '-'}"
        )


@nodes_app.command("show")
def show(node_id: int):
    """Detailed view of a single node, including its GPU inventory."""
    r = httpx.get(f"{_base()}/admin/nodes/{node_id}",
                  headers=_auth_headers(), timeout=10.0)
    r.raise_for_status()
    data = r.json()
    n = data["node"]
    typer.echo(f"label    : {n['label']}")
    typer.echo(f"status   : {n['status']}")
    typer.echo(f"version  : {n.get('agent_version') or '-'}")
    typer.echo(f"cpus     : {n['cpu_count']}, ram_mb: {n['total_ram_mb']}")
    typer.echo("gpus:")
    for g in data["gpus"]:
        typer.echo(f"  [{g['gpu_index']}] {g['name']} {g['total_vram_mb']} MB")


@nodes_app.command("enroll")
def enroll(label: str):
    """Mint a single-use enrollment token for a new agent."""
    r = httpx.post(
        f"{_base()}/admin/nodes/enroll",
        json={"label": label},
        headers=_auth_headers(),
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json()
    typer.echo("Enrollment token (single-use, expires in 10 min):")
    typer.echo(f"  token      : {data['token']}")
    typer.echo(f"  leader_url : {data['leader_url']}")
    typer.echo("")
    typer.echo("On the agent host run:")
    typer.echo(
        f"  serve agent register --leader {data['leader_url']} "
        f"--token {data['token']}"
    )


@nodes_app.command("remove")
def remove(node_id: int):
    """Decommission a node — revokes its cert fingerprint and deletes the row."""
    r = httpx.delete(
        f"{_base()}/admin/nodes/{node_id}",
        headers=_auth_headers(),
        timeout=10.0,
    )
    r.raise_for_status()
    typer.echo(f"removed node {node_id}")
