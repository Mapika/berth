"""Boot the daemon for real and verify both TLS listeners respond.

This test spawns `python -m berth.daemon` as a subprocess pointed
at a tmp_path BERTH_HOME, then connects over HTTPS to both the public
and cluster listeners. The cluster CA is loaded directly from the tmp
home to validate the server cert chain — proving the certs the daemon
generates at startup load into a real ssl.SSLContext.
"""
from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, *, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} did not open within {timeout_s}s")


def _start_daemon(
    tmp_path: Path,
    public_port: int,
    cluster_port: int,
    sock_path: Path,
    *,
    umask: int | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["BERTH_HOME"] = str(tmp_path)
    args = [
        sys.executable, "-m", "berth.daemon",
        "--public-host", "127.0.0.1",
        "--public-port", str(public_port),
        "--public-bind", "127.0.0.1",
        "--cluster-host", "127.0.0.1",
        "--cluster-port", str(cluster_port),
        "--cluster-bind", "127.0.0.1",
        "--sock", str(sock_path),
    ]
    old_umask = os.umask(umask) if umask is not None else None
    try:
        return subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        if old_umask is not None:
            os.umask(old_umask)


def test_daemon_tls_both_listeners(tmp_path: Path):
    public_port = _free_port()
    cluster_port = _free_port()
    sock_path = tmp_path / "sock"
    proc = _start_daemon(tmp_path, public_port, cluster_port, sock_path)
    try:
        _wait_for_port(public_port)
        _wait_for_port(cluster_port)
        ca_pem = (tmp_path / "ca" / "ca.crt").read_bytes().decode("ascii")
        # Public listener: serves /healthz over TLS using the cluster CA
        # fallback cert (no [public_tls] configured).
        with httpx.Client(verify=str(tmp_path / "ca" / "ca.crt")) as c:
            r = c.get(f"https://127.0.0.1:{public_port}/healthz")
        assert r.status_code == 200
        # Cluster listener: serves /admin/ca.pem, the fingerprint must
        # match the on-disk CA bytes.
        with httpx.Client(verify=str(tmp_path / "ca" / "ca.crt")) as c:
            r = c.get(f"https://127.0.0.1:{cluster_port}/admin/ca.pem")
        assert r.status_code == 200
        assert r.text.strip() == ca_pem.strip()
        import hashlib
        fp = "sha256:" + hashlib.sha256(r.text.encode("utf-8")).hexdigest()
        assert r.headers["x-berth-ca-fingerprint"] == fp
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_daemon_control_socket_is_owner_only(tmp_path: Path):
    public_port = _free_port()
    cluster_port = _free_port()
    sock_path = tmp_path / "sock"
    proc = _start_daemon(
        tmp_path,
        public_port,
        cluster_port,
        sock_path,
        umask=0o022,
    )
    try:
        _wait_for_port(public_port)
        mode = stat.S_IMODE(sock_path.stat().st_mode)
        assert mode == 0o600
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_daemon_berth_home_is_owner_only(tmp_path: Path):
    berth_home = tmp_path / "home"
    berth_home.mkdir()
    berth_home.chmod(0o755)
    public_port = _free_port()
    cluster_port = _free_port()
    sock_path = berth_home / "sock"
    proc = _start_daemon(berth_home, public_port, cluster_port, sock_path)
    try:
        _wait_for_port(public_port)
        mode = stat.S_IMODE(berth_home.stat().st_mode)
        assert mode == 0o700
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
