from __future__ import annotations

from typer.testing import CliRunner

from berth import cli
from berth.cli import status_cmd


def test_status_uses_local_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(status_cmd.config, "SOCK_PATH", tmp_path / "sock")
    monkeypatch.setattr(status_cmd, "_systemd_state", lambda: "active")

    async def healthz(sock, path):
        assert sock == tmp_path / "sock"
        assert path == "/healthz"
        return {"ok": True}

    monkeypatch.setattr(status_cmd.ipc, "get", healthz)

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert "service: active" in result.output
    assert "daemon : running" in result.output
