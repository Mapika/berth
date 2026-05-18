from __future__ import annotations

import os
from pathlib import Path

import httpx
import typer
import yaml

from serve_engine.cli import app
from serve_engine.cluster.host_info import collect_host_info

agent_app = typer.Typer(help="Manage the local agent on this host.")
app.add_typer(agent_app, name="agent")


def _serve_home() -> Path:
    return Path(os.environ.get("SERVE_HOME", str(Path.home() / ".serve")))


@agent_app.command("register")
def register(
    leader: str = typer.Option(..., "--leader",
                               help="https://<leader-host>:<port>"),
    token: str = typer.Option(..., "--token",
                              help="single-use enrollment token from "
                                   "`serve nodes enroll`"),
    reachable_as: str | None = typer.Option(
        None, "--reachable-as",
        help="(future) LAN address for direct routing; unused in tunneled mode",
    ),
):
    """Exchange a one-time enrollment token for a durable agent certificate.

    Writes the cert, key, CA, and `agent.yaml` to $SERVE_HOME (default ~/.serve).
    """
    home = _serve_home()
    home.mkdir(parents=True, exist_ok=True)

    info = collect_host_info()
    payload = {
        "token": token,
        "host_info": {
            "cpu_count": info.cpu_count,
            "total_ram_mb": info.total_ram_mb,
            "gpu_count": info.gpu_count,
            "total_vram_mb": info.total_vram_mb,
            "gpus": [
                {
                    "index": g.index, "name": g.name,
                    "total_vram_mb": g.total_vram_mb,
                    "driver_version": g.driver_version,
                }
                for g in info.gpus
            ],
        },
    }
    r = httpx.post(
        f"{leader.rstrip('/')}/admin/nodes/register",
        json=payload, timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()

    (home / "agent.crt").write_text(data["agent_cert"])
    key_path = home / "agent.key"
    key_path.write_text(data["agent_key"])
    os.chmod(key_path, 0o600)
    (home / "ca.crt").write_text(data["ca_cert"])
    cfg = {
        "leader_url": leader,
        "node_id": data["node_id"],
        "agent_cert_path": str(home / "agent.crt"),
        "agent_key_path": str(key_path),
        "ca_cert_path": str(home / "ca.crt"),
        "reachable_as": reachable_as,
    }
    (home / "agent.yaml").write_text(yaml.safe_dump(cfg))
    typer.echo(f"registered as node_id={data['node_id']}")


@agent_app.command("start")
def start():
    """Run the agent daemon in the foreground."""
    import asyncio

    from serve_engine.cluster.agent_client import run_agent
    asyncio.run(run_agent(_serve_home()))


@agent_app.command("status")
def status():
    """Show this host's agent registration status."""
    home = _serve_home()
    cfg_path = home / "agent.yaml"
    if not cfg_path.exists():
        typer.echo("not registered")
        raise typer.Exit(1)
    cfg = yaml.safe_load(cfg_path.read_text())
    typer.echo(f"node_id  : {cfg['node_id']}")
    typer.echo(f"leader   : {cfg['leader_url']}")
    typer.echo(f"cert     : {cfg['agent_cert_path']}")
