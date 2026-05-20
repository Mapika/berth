from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import typer

from berth import config
from berth.cli import app, ipc

nodes_app = typer.Typer(help="Manage cluster nodes from the leader.")
app.add_typer(nodes_app, name="nodes")

ENROLLMENT_URI_SCHEME = "berth://enroll"


def _uds_post(path: str, json_body: dict) -> dict:
    """Call the daemon's admin API over the local UDS socket (no TLS, no auth)."""
    return asyncio.run(ipc.post(config.SOCK_PATH, path, json=json_body))


def _uds_get(path: str) -> dict:
    return asyncio.run(ipc.get(config.SOCK_PATH, path))


def _uds_delete(path: str) -> None:
    asyncio.run(ipc.delete(config.SOCK_PATH, path))


def build_enrollment_uri(*, leader: str, token: str, ca_fp: str) -> str:
    """Produce `berth://enroll?leader=...&token=...&ca_fp=...` with proper
    URL-encoding of every component."""
    q = urlencode({"leader": leader, "token": token, "ca_fp": ca_fp})
    return f"{ENROLLMENT_URI_SCHEME}?{q}"


@nodes_app.command("ls")
def list_nodes():
    """List nodes — status, GPU count, total VRAM, agent version."""
    data = _uds_get("/admin/nodes")
    rows = data["nodes"]
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
    data = _uds_get(f"/admin/nodes/{node_id}")
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
    """Mint a single-use enrollment URI for a new agent.

    Prints a `berth://enroll?leader=…&token=…&ca_fp=…` URI that bundles
    the leader URL, single-use token, and CA fingerprint. The agent
    pastes this into `berth agent register --uri '<uri>'` and the
    fingerprint pin prevents MITM during the CA bootstrap."""
    data = _uds_post("/admin/nodes/enroll", {"label": label})
    leader = data["leader_url"]
    token = data["token"]
    ca_fp = data["ca_fingerprint"]
    uri = build_enrollment_uri(leader=leader, token=token, ca_fp=ca_fp)
    typer.echo("Enrollment URI (single-use, expires in 10 min):")
    typer.echo("")
    typer.echo(f"  {uri}")
    typer.echo("")
    typer.echo("On the agent host, run:")
    typer.echo(f"  berth agent register --uri '{uri}'")


@nodes_app.command("remove")
def remove(node_id: int):
    """Decommission a node — revokes its cert fingerprint and deletes the row."""
    _uds_delete(f"/admin/nodes/{node_id}")
    typer.echo(f"removed node {node_id}")
