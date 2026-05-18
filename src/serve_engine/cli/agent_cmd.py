from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import typer
import yaml

from serve_engine.cli import app
from serve_engine.cluster.host_info import collect_host_info

agent_app = typer.Typer(help="Manage the local agent on this host.")
app.add_typer(agent_app, name="agent")


def _serve_home() -> Path:
    """Lazy ~/.serve resolution honouring SERVE_HOME at call time so tests
    that monkeypatch the env var see the override (config.SERVE_DIR is
    fixed at import time)."""
    return Path(os.environ.get("SERVE_HOME", str(Path.home() / ".serve")))


def parse_enrollment_uri(uri: str) -> tuple[str, str, str]:
    """Parse `serve://enroll?leader=...&token=...&ca_fp=...` into a tuple.

    Returns `(leader, token, ca_fp)`. Raises `ValueError` on any
    structural problem so the caller can surface a clear error."""
    parsed = urlparse(uri)
    if parsed.scheme != "serve" or parsed.netloc != "enroll":
        raise ValueError(f"expected serve://enroll URI, got {uri!r}")
    q = parse_qs(parsed.query)
    try:
        leader = q["leader"][0]
        token = q["token"][0]
        ca_fp = q["ca_fp"][0]
    except (KeyError, IndexError) as e:
        raise ValueError(
            "enrollment URI missing required param "
            "(need leader, token, ca_fp)"
        ) from e
    if not leader.startswith(("http://", "https://")):
        raise ValueError(f"leader must be http(s) URL, got {leader!r}")
    return leader, token, ca_fp


def _fetch_ca_pinned(leader: str, expected_fp: str) -> str:
    """Fetch CA from <leader>/admin/ca.pem with TLS verification disabled
    *for this one request*, then verify the response body hashes to the
    pinned fingerprint. Returns the verified PEM string.

    TLS verification is off because the agent doesn't yet trust this CA
    — we use the operator-supplied fingerprint instead (trust-on-first-
    use-with-pin). Once written, all subsequent calls verify normally."""
    url = leader.rstrip("/") + "/admin/ca.pem"
    r = httpx.get(url, verify=False, timeout=15.0)
    r.raise_for_status()
    pem = r.text
    actual = "sha256:" + hashlib.sha256(pem.encode("utf-8")).hexdigest()
    if actual.lower() != expected_fp.lower():
        raise typer.BadParameter(
            f"CA fingerprint mismatch.\n"
            f"  expected (from URI): {expected_fp}\n"
            f"  actual   (from {url}): {actual}\n"
            "Refusing to install CA — possible MITM, or stale URI."
        )
    return pem


def _do_register(
    *, leader: str, token: str, ca_pem: str | None, reachable_as: str | None,
) -> None:
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
    register_url = f"{leader.rstrip('/')}/admin/nodes/register"
    if ca_pem is not None:
        # CA already verified by fingerprint; write it now so the post
        # below verifies properly.
        (home / "ca.crt").write_text(ca_pem)
        r = httpx.post(
            register_url, json=payload,
            verify=str(home / "ca.crt"), timeout=30.0,
        )
    else:
        # Legacy --leader/--token path with no pinned fingerprint:
        # do the original behavior (server cert verification with system
        # trust store).
        r = httpx.post(register_url, json=payload, timeout=30.0)
    r.raise_for_status()
    data = r.json()

    (home / "agent.crt").write_text(data["agent_cert"])
    key_path = home / "agent.key"
    key_path.write_text(data["agent_key"])
    os.chmod(key_path, 0o600)
    # If the server returned a CA too (legacy), persist it.
    if "ca_cert" in data and ca_pem is None:
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


@agent_app.command("register")
def register(
    uri: str = typer.Option(
        None, "--uri",
        help="Single-paste enrollment URI from `serve nodes enroll` "
             "(format: serve://enroll?leader=…&token=…&ca_fp=…). "
             "Mutually exclusive with --leader/--token.",
    ),
    leader: str = typer.Option(
        None, "--leader",
        help="https://<leader-host>:<port> (legacy; prefer --uri).",
    ),
    token: str = typer.Option(
        None, "--token",
        help="Single-use enrollment token (legacy; prefer --uri).",
    ),
    reachable_as: str | None = typer.Option(
        None, "--reachable-as",
        help="(future) LAN address for direct routing; unused in tunneled mode",
    ),
):
    """Exchange a one-time enrollment token for a durable agent certificate.

    Prefer the `--uri` form: it bundles the leader URL, token, and CA
    fingerprint so the agent can detect a swapped CA during the bootstrap
    download. The legacy `--leader/--token` form remains for scripted
    setups but cannot pin the CA — use it only when the network path to
    the leader is trusted."""
    if uri is not None and (leader is not None or token is not None):
        raise typer.BadParameter(
            "--uri is mutually exclusive with --leader/--token"
        )
    if uri is None and (leader is None or token is None):
        raise typer.BadParameter(
            "provide --uri, or both --leader and --token"
        )

    if uri is not None:
        leader_url, token_val, ca_fp = parse_enrollment_uri(uri)
        ca_pem = _fetch_ca_pinned(leader_url, ca_fp) if ca_fp else None
        _do_register(
            leader=leader_url, token=token_val,
            ca_pem=ca_pem, reachable_as=reachable_as,
        )
    else:
        assert leader is not None and token is not None  # narrow for type
        _do_register(
            leader=leader, token=token,
            ca_pem=None, reachable_as=reachable_as,
        )


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
