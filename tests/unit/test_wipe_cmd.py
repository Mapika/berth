from __future__ import annotations

from typer.testing import CliRunner

from berth import cli
from berth.cli import wipe_cmd


def test_wipe_refuses_broad_paths():
    result = CliRunner().invoke(cli.app, ["wipe", "--home", "/", "--yes"])

    assert result.exit_code != 0
    assert "refusing to wipe broad path" in result.output


def test_wipe_clears_home_with_yes(monkeypatch, tmp_path):
    home = tmp_path / "berth-home"
    (home / "models").mkdir(parents=True)
    (home / "models" / "model.bin").write_text("x")
    (home / "db.sqlite").write_text("db")

    monkeypatch.setattr(wipe_cmd, "_stop_systemd_service", lambda: None)
    monkeypatch.setattr(wipe_cmd, "_stop_pid_daemon", lambda home: None)
    monkeypatch.setattr(wipe_cmd, "_remove_berth_docker", lambda: ["docker skipped"])

    result = CliRunner().invoke(
        cli.app,
        ["wipe", "--home", str(home), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert "docker skipped" in result.output
    assert "wiped 2 item(s)" in result.output
    assert list(home.iterdir()) == []
