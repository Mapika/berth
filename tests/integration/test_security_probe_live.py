from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from scripts import security_probe


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


def test_security_probe_passes_against_live_tls_listeners(tmp_path: Path):
    public_port = _free_port()
    cluster_port = _free_port()
    sock_path = tmp_path / "sock"
    env = os.environ.copy()
    env["BERTH_HOME"] = str(tmp_path)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "berth.daemon",
            "--public-host", "127.0.0.1",
            "--public-port", str(public_port),
            "--public-bind", "127.0.0.1",
            "--cluster-host", "127.0.0.1",
            "--cluster-port", str(cluster_port),
            "--cluster-bind", "127.0.0.1",
            "--sock", str(sock_path),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_port(public_port)
        _wait_for_port(cluster_port)
        results = security_probe.run_probe(
            public_url=f"https://127.0.0.1:{public_port}",
            cluster_url=f"https://127.0.0.1:{cluster_port}",
            bearer_token=None,
            verify=False,
        )
        failures = [r for r in results if not r.ok]
        assert failures == []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
