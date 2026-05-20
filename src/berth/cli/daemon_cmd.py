from __future__ import annotations

import asyncio
import os
import signal
import subprocess  # nosec
import sys
import time

import typer

from berth import config
from berth.cli import app, ipc

daemon_app = typer.Typer(help="Daemon control")
app.add_typer(daemon_app, name="daemon")

PID_FILE = config.BERTH_DIR / "daemon.pid"


def spawn_daemon(
    cfg: config.ResolvedConfig,
    *,
    timeout_s: float = 30.0,
    poll_s: float = 0.5,
) -> int:
    """Spawn the daemon process, write its PID, poll the UDS /healthz
    until it answers. Returns the spawned PID."""
    config.ensure_private_dir(config.BERTH_DIR)
    log_path = config.LOGS_DIR / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "berth.daemon",
        "--public-host", cfg.public_host,
        "--public-port", str(cfg.public_port),
        "--public-bind", cfg.public_bind,
        "--cluster-host", cfg.cluster_host,
        "--cluster-port", str(cfg.cluster_port),
        "--cluster-bind", cfg.cluster_bind,
    ]
    if cfg.public_cert_path:
        cmd.extend(["--public-cert", str(cfg.public_cert_path)])
    if cfg.public_key_path:
        cmd.extend(["--public-key", str(cfg.public_key_path)])
    proc = subprocess.Popen(  # nosec
        cmd,
        stdout=open(log_path, "ab"),  # file must outlive this Popen call
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            asyncio.run(ipc.get(config.SOCK_PATH, "/healthz"))
            return proc.pid
        except Exception:
            time.sleep(poll_s)
    raise TimeoutError(f"daemon failed to become ready within {timeout_s:.0f}s")


def _is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _print_startup_banner(cfg: config.ResolvedConfig, pid: int) -> None:
    """Print the resolved addresses, certs, and fingerprint."""
    from berth.cluster.ca import fingerprint_ca_pem, load_ca

    ca_dir = config.BERTH_DIR / "ca"
    ca = load_ca(ca_dir)
    ca_fp = fingerprint_ca_pem(ca.cert_pem)
    using_public_cert = bool(cfg.public_cert_path and cfg.public_key_path)

    typer.echo(f"daemon started (pid {pid})")
    typer.echo("")
    if using_public_cert:
        typer.echo(
            f"public  : {cfg.public_url}    (cert: {cfg.public_cert_path})"
        )
    else:
        typer.echo(
            f"public  : {cfg.public_url}    "
            "⚠  using cluster-CA cert"
        )
        typer.echo(
            "            external clients must trust "
            f"{ca_fp} or set [public_tls]"
        )
    typer.echo(
        f"cluster : {cfg.cluster_url}  (cert: serve cluster CA)"
    )
    typer.echo(f"            ca fingerprint: {ca_fp}")
    if cfg.cluster_bind == "0.0.0.0":  # nosec
        typer.echo("")
        typer.echo(
            f"cluster listener on {cfg.cluster_bind}:{cfg.cluster_port} "
            "— internet-reachable"
        )
        typer.echo(
            "  consider setting [cluster] bind in ~/.berth/config.toml "
            "to restrict it to a private/VPN interface"
        )


@daemon_app.command("start")
def daemon_start(
    public_host: str = typer.Option(None, "--public-host"),
    public_port: int = typer.Option(None, "--public-port"),
    public_bind: str = typer.Option(None, "--public-bind"),
    public_cert: str = typer.Option(None, "--public-cert"),
    public_key: str = typer.Option(None, "--public-key"),
    cluster_host: str = typer.Option(None, "--cluster-host"),
    cluster_port: int = typer.Option(None, "--cluster-port"),
    cluster_bind: str = typer.Option(None, "--cluster-bind"),
    foreground: bool = typer.Option(
        False, "--foreground",
        help="Run in the current process (don't spawn a background "
        "daemon, don't write a PID file). Use under systemd Type=exec, "
        "or for development debugging.",
    ),
    # Back-compat aliases.
    host: str = typer.Option(None, "--host", hidden=True),
    port: int = typer.Option(None, "--port", hidden=True),
):
    """Start the daemon. Defaults to background mode with a PID file
    and detached process; `--foreground` runs it in this terminal."""
    if not foreground and _is_running():
        typer.echo("daemon already running")
        raise typer.Exit(0)
    cfg = config.resolve_config(
        cli_public_host=public_host or host,
        cli_public_port=public_port or port,
        cli_public_bind=public_bind,
        cli_cluster_host=cluster_host,
        cli_cluster_port=cluster_port,
        cli_cluster_bind=cluster_bind,
        cli_public_cert=public_cert,
        cli_public_key=public_key,
    )
    if foreground:
        # Run the daemon in-process. Stdout/stderr go to the parent
        # (systemd captures them into journald; humans see them in
        # their terminal). No PID file — the supervising process
        # tracks us already.
        from berth.daemon.__main__ import (
            configure_logging,
        )
        from berth.daemon.__main__ import (
            serve as _serve_inline,
        )
        configure_logging()
        try:
            asyncio.run(_serve_inline(cfg, config.SOCK_PATH))
        except KeyboardInterrupt:
            pass
        return
    try:
        pid = spawn_daemon(cfg)
    except TimeoutError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    _print_startup_banner(cfg, pid)


@daemon_app.command("stop")
def daemon_stop():
    """Stop the daemon."""
    if not _is_running():
        typer.echo("daemon not running")
        raise typer.Exit(0)
    pid = int(PID_FILE.read_text().strip())
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            break
    if PID_FILE.exists():
        PID_FILE.unlink()
    typer.echo("daemon stopped")


@daemon_app.command("status")
def daemon_status():
    """Show daemon status."""
    if not _is_running():
        typer.echo("daemon: not running")
        raise typer.Exit(1)
    pid = int(PID_FILE.read_text().strip())
    try:
        body = asyncio.run(ipc.get(config.SOCK_PATH, "/healthz"))
        typer.echo(f"daemon: running (pid {pid}), healthz: {body}")
    except Exception as e:
        typer.echo(f"daemon: pid file present (pid {pid}) but unhealthy: {e}", err=True)
        raise typer.Exit(2) from e
