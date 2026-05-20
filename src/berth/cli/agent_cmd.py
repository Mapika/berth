from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import typer
import yaml

from berth.cli import app
from berth.cluster.host_info import collect_host_info
from berth.config import _env_get, ensure_private_dir, write_private_file

agent_app = typer.Typer(help="Manage the local agent on this host.")
app.add_typer(agent_app, name="agent")


def _serve_home() -> Path:
    """Lazy ~/.berth resolution honouring BERTH_HOME (legacy: SERVE_HOME)
    at call time so tests that monkeypatch the env var see the override
    (config.BERTH_DIR is fixed at import time)."""
    return Path(_env_get(os.environ, "SERVE_HOME") or str(Path.home() / ".berth"))


def _agent_log_path(home: Path, override: Path | None = None) -> Path:
    return override or home / "logs" / "agent.log"


def _configure_agent_logging(log_path: Path, *, verbose: bool) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.captureWarnings(True)


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class _AgentStatusState:
    state: str = "starting"
    leader: str = "-"
    node_id: str = "-"
    operation: str = "initializing"
    last_error: str = "-"
    started_at: float = field(default_factory=time.monotonic)
    events: deque[str] = field(default_factory=lambda: deque(maxlen=7))

    def apply(self, event: str, payload: dict) -> None:
        if event == "agent.initializing":
            self.state = "initializing"
            self.leader = str(payload.get("leader") or self.leader)
            self.node_id = str(payload.get("node_id") or self.node_id)
            self.operation = "checking Docker and local state"
            self._add("initializing agent")
        elif event == "agent.connecting":
            self.state = "connecting"
            self.leader = str(payload.get("leader") or self.leader)
            self.node_id = str(payload.get("node_id") or self.node_id)
            self.operation = "opening leader websocket"
            self._add(f"connecting to {self.leader}")
        elif event == "agent.connected":
            self.state = "connected"
            self.leader = str(payload.get("leader") or self.leader)
            self.node_id = str(payload.get("node_id") or self.node_id)
            self.operation = "waiting for deployments"
            self.last_error = "-"
            self._add(f"connected as node {self.node_id}")
        elif event == "agent.reconnecting":
            delay = float(payload.get("delay_s") or 0.0)
            err = str(payload.get("error") or "connection lost")
            self.state = "reconnecting"
            self.last_error = err
            self.operation = f"reconnecting in {delay:.1f}s"
            self._add(f"connection lost: {err}")
        elif event == "agent.warning":
            msg = str(payload.get("message") or "warning")
            self.last_error = msg
            self._add(f"warning: {msg}")
        elif event == "agent.endpoint_reattached":
            cid = str(payload.get("container_id") or "")[:12]
            self._add(f"re-attached container {cid}")
        elif event == "deployment.download_started":
            repo = str(payload.get("repo") or "model")
            rev = str(payload.get("revision") or "main")
            self.operation = f"downloading {repo}@{rev}"
            self._add(self.operation)
        elif event == "deployment.download_finished":
            repo = str(payload.get("repo") or "model")
            self.operation = f"download ready: {repo}"
            self._add(self.operation)
        elif event == "deployment.container_starting":
            name = str(payload.get("name") or "container")
            image = str(payload.get("image") or "image")
            self.operation = f"starting {name}"
            self._add(f"starting {name} from {image}")
        elif event == "deployment.container_started":
            name = str(payload.get("name") or "container")
            cid = str(payload.get("container_id") or "")[:12]
            self.operation = f"container running: {name}"
            self._add(f"container {cid} running for {name}")
        elif event == "agent.fatal":
            self.state = "failed"
            self.last_error = str(payload.get("error") or "agent failed")
            self.operation = "stopped"
            self._add(f"fatal: {self.last_error}")

    def _add(self, message: str) -> None:
        self.events.appendleft(f"{_stamp()}  {message}")


def _render_agent_status(state: _AgentStatusState, log_path: Path):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    status_style = {
        "connected": "green",
        "connecting": "yellow",
        "reconnecting": "yellow",
        "failed": "red",
    }.get(state.state, "cyan")
    uptime = int(time.monotonic() - state.started_at)

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("status", Text(state.state, style=status_style))
    table.add_row("leader", state.leader)
    table.add_row("node", state.node_id)
    table.add_row("uptime", f"{uptime}s")
    table.add_row("operation", state.operation)
    table.add_row("last error", state.last_error)
    table.add_row("log file", str(log_path))

    events = Table(title="recent activity", show_header=False, box=None)
    events.add_column()
    for line in state.events:
        events.add_row(line)
    if not state.events:
        events.add_row("waiting for agent events")

    return Panel(
        Group(table, "", events),
        title="berth agent",
        subtitle="Ctrl-C to stop",
    )


async def _run_agent_interactive(home: Path, log_path: Path) -> None:
    from rich.console import Console
    from rich.live import Live

    from berth.cluster.agent_client import run_agent

    console = Console()
    if not console.is_terminal:
        typer.echo(f"agent running; logs: {log_path}")
        await run_agent(home, quiet_downloads=True)
        return

    state = _AgentStatusState()
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def status_cb(event: str, payload: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, payload))

    task = asyncio.create_task(
        run_agent(home, status_cb=status_cb, quiet_downloads=True)
    )
    with Live(
        _render_agent_status(state, log_path),
        refresh_per_second=4,
        console=console,
        screen=False,
    ) as live:
        while not task.done():
            with contextlib.suppress(asyncio.TimeoutError):
                event, payload = await asyncio.wait_for(queue.get(), timeout=0.25)
                state.apply(event, payload)
            live.update(_render_agent_status(state, log_path))
        exc = task.exception()
        if exc is not None:
            state.apply("agent.fatal", {"error": str(exc)})
            live.update(_render_agent_status(state, log_path))
            raise exc


def _tail_lines(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return list(deque(f, maxlen=lines))


def parse_enrollment_uri(uri: str) -> tuple[str, str, str]:
    """Parse `berth://enroll?leader=...&token=...&ca_fp=...` into a tuple.

    Returns `(leader, token, ca_fp)`. Raises `ValueError` on any
    structural problem so the caller can surface a clear error."""
    parsed = urlparse(uri)
    if parsed.scheme != "berth" or parsed.netloc != "enroll":
        raise ValueError(f"expected berth://enroll URI, got {uri!r}")
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
    r = httpx.get(url, verify=False, timeout=15.0)  # nosec B501
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
    ensure_private_dir(home)

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
    write_private_file(key_path, data["agent_key"].encode("utf-8"))
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
        help="Single-paste enrollment URI from `berth nodes enroll` "
             "(format: berth://enroll?leader=…&token=…&ca_fp=…). "
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
        if leader is None or token is None:
            raise typer.BadParameter(
                "provide --uri, or both --leader and --token"
            )
        _do_register(
            leader=leader, token=token,
            ca_pem=None, reachable_as=reachable_as,
        )


@agent_app.command("start")
def start(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Print raw logs/progress to stderr instead of the compact status UI.",
    ),
    log_file: Path | None = typer.Option(
        None,
        "--log-file",
        help="Write agent logs here (default: ~/.berth/logs/agent.log).",
    ),
):
    """Run the agent daemon in the foreground."""
    from berth.cluster.agent_client import run_agent

    home = _serve_home()
    path = _agent_log_path(home, log_file)
    _configure_agent_logging(path, verbose=verbose)

    try:
        if verbose:
            asyncio.run(run_agent(home, quiet_downloads=False))
        else:
            asyncio.run(_run_agent_interactive(home, path))
    except KeyboardInterrupt:
        typer.echo("agent stopped")


@agent_app.command("logs")
def logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow new log lines."),
    lines: int = typer.Option(100, "--lines", "-n", min=0, help="Initial lines to show."),
    log_file: Path | None = typer.Option(
        None,
        "--log-file",
        help="Read this log file (default: ~/.berth/logs/agent.log).",
    ),
):
    """Show the local agent log file."""
    path = _agent_log_path(_serve_home(), log_file)
    if not path.exists():
        typer.echo(f"agent log not found: {path}", err=True)
        raise typer.Exit(1)
    for line in _tail_lines(path, lines):
        typer.echo(line, nl=False)
    if not follow:
        return
    with path.open(encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                typer.echo(line, nl=False)
            else:
                time.sleep(0.5)


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
