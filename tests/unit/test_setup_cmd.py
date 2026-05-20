from __future__ import annotations

from typer.testing import CliRunner

from berth import cli
from berth.cli import setup_cmd
from berth.doctor.runner import CheckResult


def test_setup_passes_resolved_config_to_spawn_daemon(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_cmd.config, "BERTH_DIR", tmp_path)
    monkeypatch.setattr(setup_cmd.config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(setup_cmd.config, "SOCK_PATH", tmp_path / "sock")
    seen = {}

    def run_all(*, leader_only=False):
        seen["leader_only"] = leader_only
        return [CheckResult(name="python", status="ok", detail="ok")]

    monkeypatch.setattr(setup_cmd, "run_all", run_all)
    monkeypatch.setattr(setup_cmd, "summarise", lambda results: (1, 0, 0))

    async def failing_healthz(*args, **kwargs):
        raise RuntimeError("not running")

    async def create_key(*args, **kwargs):
        return {"id": 1, "secret": "sk-test"}  # pragma: allowlist secret

    def spawn_daemon(cfg, *, timeout_s, poll_s):
        seen["cfg"] = cfg
        seen["timeout_s"] = timeout_s
        seen["poll_s"] = poll_s
        return 1234

    monkeypatch.setattr(setup_cmd.ipc, "get", failing_healthz)
    monkeypatch.setattr(setup_cmd.ipc, "post", create_key)
    monkeypatch.setattr(setup_cmd, "spawn_daemon", spawn_daemon)
    monkeypatch.setattr(setup_cmd.config, "autodetect_outbound_ip", lambda: None)

    result = CliRunner().invoke(cli.app, ["setup"], input="admin\n")

    assert result.exit_code == 0, result.output
    assert seen["leader_only"] is False
    assert seen["cfg"].public_port == setup_cmd.config.DEFAULT_PUBLIC_PORT
    assert seen["timeout_s"] == 15.0
    assert seen["poll_s"] == 0.3
    assert "daemon started (pid 1234)" in result.output


def test_setup_runs_leader_only_doctor_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_cmd.config, "BERTH_DIR", tmp_path)
    monkeypatch.setattr(setup_cmd.config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(setup_cmd.config, "SOCK_PATH", tmp_path / "sock")
    setup_cmd.config.save_config_file({"server": {"leader_only": True}})

    seen = {}

    def run_all(*, leader_only=False):
        seen["leader_only"] = leader_only
        return [CheckResult(name="python", status="ok", detail="ok")]

    monkeypatch.setattr(setup_cmd, "run_all", run_all)
    monkeypatch.setattr(setup_cmd, "summarise", lambda results: (1, 0, 0))

    async def failing_healthz(*args, **kwargs):
        raise RuntimeError("not running")

    async def create_key(*args, **kwargs):
        return {"id": 1, "secret": "sk-test"}  # pragma: allowlist secret

    def spawn_daemon(cfg, *, timeout_s, poll_s):
        seen["cfg"] = cfg
        return 1234

    monkeypatch.setattr(setup_cmd.ipc, "get", failing_healthz)
    monkeypatch.setattr(setup_cmd.ipc, "post", create_key)
    monkeypatch.setattr(setup_cmd, "spawn_daemon", spawn_daemon)
    monkeypatch.setattr(setup_cmd.config, "autodetect_outbound_ip", lambda: None)

    result = CliRunner().invoke(cli.app, ["setup"], input="admin\n")

    assert result.exit_code == 0, result.output
    assert seen["leader_only"] is True
    assert seen["cfg"].leader_only is True
