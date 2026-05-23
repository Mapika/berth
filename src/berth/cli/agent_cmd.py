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
from berth.cluster import adopted as adopted_mod
from berth.cluster.host_info import collect_host_info
from berth.config import _env_get, ensure_private_dir, write_private_file

agent_app = typer.Typer(help="Manage the local agent on this host.")
app.add_typer(agent_app, name="agent")


def _berth_home() -> Path:
    """Resolve BERTH_HOME at call time for tests and one-shot CLI calls."""
    return Path(_env_get(os.environ, "BERTH_HOME") or str(Path.home() / ".berth"))


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
    *, leader: str, token: str, ca_pem: str, reachable_as: str | None,
) -> None:
    home = _berth_home()
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
    # CA already verified by fingerprint; write it now so the post below
    # verifies properly.
    (home / "ca.crt").write_text(ca_pem)
    r = httpx.post(
        register_url, json=payload,
        verify=str(home / "ca.crt"), timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()

    (home / "agent.crt").write_text(data["agent_cert"])
    key_path = home / "agent.key"
    write_private_file(key_path, data["agent_key"].encode("utf-8"))
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


def _render_agent_unit(*, berth_bin: str, berth_home: str, system: bool,
                       run_user: str | None) -> str:
    user_line = f"User={run_user}\n" if (system and run_user) else ""
    return f"""[Unit]
Description=berth agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=exec
{user_line}Environment=BERTH_HOME={berth_home}
ExecStart={berth_bin} agent start
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy={"multi-user.target" if system else "default.target"}
"""


@agent_app.command("install-service")
def install_service(
    system: bool = typer.Option(False, "--system",
        help="Install a system unit (needs root); default is a --user unit."),
    user: bool = typer.Option(False, "--user", help="Install a user unit (default)."),
    berth_home: Path = typer.Option(None, "--berth-home",
        help="BERTH_HOME for the service (default: resolved agent home)."),
):
    """Install + enable a systemd unit that runs `berth agent start`."""
    import shutil
    import subprocess  # nosec

    home = str(berth_home or _berth_home())
    berth_bin = shutil.which("berth") or sys.argv[0]
    is_system = system and not user

    run_user = None
    if is_system:
        run_user = os.environ.get("SUDO_USER") or (
            None if os.geteuid() == 0 else os.environ.get("USER"))
        if not run_user:
            typer.echo(
                "--system needs a non-root account for the unit's User= "
                "(run via sudo, or use --user).", err=True)
            raise typer.Exit(1)
        unit_dir = Path("/etc/systemd/system")
    else:
        unit_dir = Path.home() / ".config" / "systemd" / "user"

    unit = _render_agent_unit(berth_bin=berth_bin, berth_home=home,
                              system=is_system, run_user=run_user)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "berth-agent.service"
    unit_path.write_text(unit)
    typer.echo(f"wrote {unit_path}")

    sysctl = shutil.which("systemctl")
    if not sysctl:
        typer.echo("systemctl not found; enable the unit manually.", err=True)
        return
    scope = [] if is_system else ["--user"]
    subprocess.run([sysctl, *scope, "daemon-reload"], check=False)  # nosec
    subprocess.run([sysctl, *scope, "enable", "--now", "berth-agent"], check=False)  # nosec
    typer.echo("berth-agent enabled and started.")
    if not is_system:
        typer.echo("tip: `loginctl enable-linger $USER` keeps it running after logout.")


@agent_app.command("register")
def register(
    uri: str = typer.Option(
        ..., "--uri",
        help="Single-paste enrollment URI from `berth nodes enroll` "
             "(format: berth://enroll?leader=...&token=...&ca_fp=...).",
    ),
    reachable_as: str | None = typer.Option(
        None, "--reachable-as",
        help="(future) LAN address for direct routing; unused in tunneled mode",
    ),
):
    """Exchange a one-time enrollment token for a durable agent certificate.

    The URI bundles the leader URL, token, and CA fingerprint so the
    agent can detect a swapped CA during bootstrap."""
    leader_url, token_val, ca_fp = parse_enrollment_uri(uri)
    ca_pem = _fetch_ca_pinned(leader_url, ca_fp)
    _do_register(
        leader=leader_url, token=token_val,
        ca_pem=ca_pem, reachable_as=reachable_as,
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

    home = _berth_home()
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
    path = _agent_log_path(_berth_home(), log_file)
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
    home = _berth_home()
    cfg_path = home / "agent.yaml"
    if not cfg_path.exists():
        typer.echo("not registered")
        raise typer.Exit(1)
    cfg = yaml.safe_load(cfg_path.read_text())
    typer.echo(f"node_id  : {cfg['node_id']}")
    typer.echo(f"leader   : {cfg['leader_url']}")
    typer.echo(f"cert     : {cfg['agent_cert_path']}")


@agent_app.command("adopt")
def adopt(
    container: str = typer.Option(None, "--container",
        help="Adopt a running docker container by name (introspect port+GPUs)."),
    port: int = typer.Option(None, "--port",
        help="Adopt a raw OpenAI-compatible server on this host:port."),
    model: str = typer.Option(None, "--model",
        help="Model name (required with --port; used as the registry name)."),
    name: str = typer.Option(None, "--name", help="Local label (default: model)."),
    host: str = typer.Option("127.0.0.1", "--host"),
    gpus: str = typer.Option("", "--gpus", help="Comma-separated GPU ids, e.g. '7'."),
    served_model_name: str = typer.Option(None, "--served-model-name"),
    vram_mb: int = typer.Option(0, "--vram-mb",
        help="VRAM to reserve for these GPUs (0 = leave scheduler to treat as full)."),
):
    """Register an already-running OpenAI-compatible server as a deployment."""
    home = _berth_home()
    try:
        gpu_ids = [int(g) for g in gpus.split(",") if g.strip()]
    except ValueError:
        typer.echo("--gpus must be comma-separated integers, e.g. '0,7'", err=True)
        raise typer.Exit(1) from None
    if container:
        from berth.config import DOCKER_NETWORK_NAME
        from berth.lifecycle.docker_client import DockerClient
        try:
            cid, addr, prt, c_gpus, image_tag = adopted_mod.introspect_container(
                DockerClient(network_name=DOCKER_NETWORK_NAME), container)
        except Exception as e:
            typer.echo(f"adopt failed: {e}", err=True)
            raise typer.Exit(1) from e
        gpu_ids = gpu_ids or c_gpus
        addr_eff, port_eff = addr, prt
    elif port:
        if not model:
            typer.echo("--model is required with --port", err=True)
            raise typer.Exit(1)
        cid = f"adopted-{model.replace('/', '-')}-{port}"
        addr_eff, port_eff, image_tag = host, port, "external"
    else:
        typer.echo("provide --container OR both --port and --model", err=True)
        raise typer.Exit(1)

    try:
        served = served_model_name or adopted_mod.probe_served_model(addr_eff, port_eff)
    except adopted_mod.AdoptError as e:
        typer.echo(f"adopt failed: {e}", err=True)
        raise typer.Exit(1) from e

    model_name = model or served
    entry = adopted_mod.AdoptedEndpoint(
        name=name or model_name, model_name=model_name,
        served_model_name=served, address=addr_eff, port=port_eff,
        container_id=cid, gpu_ids=gpu_ids, vram_reserved_mb=vram_mb,
        image_tag=image_tag,
    )
    try:
        adopted_mod.add_entry(home, entry)
    except adopted_mod.AdoptError as e:
        typer.echo(f"adopt failed: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"adopted {entry.name} -> {addr_eff}:{port_eff} "
        f"(model {served}, gpu {gpu_ids}). Takes effect on the running agent."
    )


@agent_app.command("unadopt")
def unadopt(name: str = typer.Argument(...)):
    """Remove an adopted endpoint; the route drops on the next agent report."""
    home = _berth_home()
    before = {e.name for e in adopted_mod.load(home)}
    if name not in before:
        typer.echo(f"no adopted endpoint named {name!r}", err=True)
        raise typer.Exit(1)
    adopted_mod.remove_entry(home, name)
    typer.echo(f"unadopted {name}")


@agent_app.command("adopted")
def adopted_ls():
    """List adopted endpoints recorded on this host."""
    for e in adopted_mod.load(_berth_home()):
        typer.echo(f"{e.name}\t{e.address}:{e.port}\t{e.served_model_name}\tgpu={e.gpu_ids}")
