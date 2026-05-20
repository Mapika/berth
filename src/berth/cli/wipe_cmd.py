from __future__ import annotations

import os
import pwd
import shutil
import signal
import subprocess  # nosec
import time
from pathlib import Path
from typing import Any, cast

import typer

from berth import config
from berth.cli import app

_DANGEROUS_HOMES = {
    Path("/"),
    Path("/etc"),
    Path("/home"),
    Path("/opt"),
    Path("/tmp"),
    Path("/usr"),
    Path("/var"),
    Path("/var/lib"),
}


def _validated_home(home: Path) -> Path:
    resolved = home.expanduser().resolve(strict=False)
    if resolved in _DANGEROUS_HOMES or len(resolved.parts) < 3:
        raise typer.BadParameter(f"refusing to wipe broad path: {resolved}")
    if resolved.exists() and resolved.is_symlink():
        raise typer.BadParameter(f"refusing to wipe symlink: {resolved}")
    return resolved


def _stop_systemd_service() -> str | None:
    if os.geteuid() != 0 or shutil.which("systemctl") is None:
        return None
    result = subprocess.run(  # nosec
        ["systemctl", "stop", "berth"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return "stopped systemd service berth"
    detail = (result.stderr or result.stdout).strip()
    if (
        "not been booted with systemd" in detail
        or "Failed to connect to bus" in detail
        or "Unit berth.service not loaded" in detail
    ):
        return None
    return f"systemd stop skipped: {detail or 'service not available'}"


def _stop_pid_daemon(home: Path) -> str | None:
    pid_file = home / "daemon.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        pid_file.unlink(missing_ok=True)
        return "removed invalid daemon pid file"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return "removed stale daemon pid file"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    pid_file.unlink(missing_ok=True)
    return f"stopped daemon pid {pid}"


def _remove_berth_docker() -> list[str]:
    messages: list[str] = []
    try:
        from docker.errors import NotFound  # type: ignore[import-untyped]

        import docker  # type: ignore[import-untyped]
    except Exception as e:
        return [f"docker cleanup skipped: {e}"]
    try:
        client = cast(Any, docker).from_env()
        removed = 0
        for container in client.containers.list(all=True):
            if getattr(container, "name", "").startswith("berth-"):
                container.remove(force=True)
                removed += 1
        messages.append(f"removed {removed} berth docker container(s)")
        try:
            network = client.networks.get(config.DOCKER_NETWORK_NAME)
            network.remove()
            messages.append(f"removed docker network {config.DOCKER_NETWORK_NAME}")
        except NotFound:
            messages.append(f"docker network {config.DOCKER_NETWORK_NAME} not present")
    except Exception as e:
        messages.append(f"docker cleanup skipped: {e}")
    return messages


def _wipe_home(home: Path) -> int:
    if not home.exists():
        return 0
    count = 0
    for child in home.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
        count += 1
    return count


def _restore_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(config.PRIVATE_DIR_MODE)
    if os.geteuid() != 0:
        return
    try:
        user = pwd.getpwnam("berth")
    except KeyError:
        return
    os.chown(home, user.pw_uid, user.pw_gid)


@app.command("wipe")
def wipe(
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
    home: Path | None = typer.Option(
        None, "--home",
        help="State directory to wipe. Defaults to BERTH_HOME or ~/.berth.",
    ),
    keep_docker: bool = typer.Option(
        False, "--keep-docker",
        help="Do not remove berth-owned Docker containers or network.",
    ),
):
    """Stop berth and delete local state, models, keys, certs, logs, and DB."""
    target = _validated_home(home or config.BERTH_DIR)
    if not yes:
        typer.confirm(
            "This deletes all berth state under "
            f"{target} (DB, models, keys, certs, logs, configs). Continue?",
            abort=True,
        )
    for message in (_stop_systemd_service(), _stop_pid_daemon(target)):
        if message:
            typer.echo(message)
    if not keep_docker:
        for message in _remove_berth_docker():
            typer.echo(message)
    removed = _wipe_home(target)
    _restore_home(target)
    typer.echo(f"wiped {removed} item(s) from {target}")
