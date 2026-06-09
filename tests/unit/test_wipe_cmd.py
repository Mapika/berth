from __future__ import annotations

from typer.testing import CliRunner

from berth import cli
from berth.cli import wipe_cmd


def test_wipe_refuses_broad_paths():
    result = CliRunner().invoke(cli.app, ["wipe", "--home", "/", "--yes"])

    assert result.exit_code != 0
    assert "refusing to wipe broad path" in result.output


def test_wipe_refuses_plain_home_without_marker(monkeypatch, tmp_path):
    # A real user home: deep enough to clear the denylist, but with no berth
    # marker. It must be refused so `--home /home/alice` can't rm -rf a home.
    home = tmp_path / "alice"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "thesis.txt").write_text("important")
    (home / ".bashrc").write_text("export PATH=...")

    # Ensure the configured BERTH_DIR doesn't happen to match this path.
    monkeypatch.setattr(wipe_cmd.config, "BERTH_DIR", tmp_path / "real-berth")

    # Wide terminal so Rich doesn't wrap/truncate the error panel mid-phrase.
    result = CliRunner().invoke(
        cli.app, ["wipe", "--home", str(home), "--yes"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code != 0
    assert "does not look like a berth home" in result.output
    # Nothing was deleted.
    assert (home / "Documents" / "thesis.txt").exists()
    assert (home / ".bashrc").exists()


def test_wipe_allows_dotberth_named_dir(monkeypatch, tmp_path):
    # A directory named .berth is accepted even without a marker file.
    home = tmp_path / ".berth"
    home.mkdir()
    (home / "logs").mkdir()

    monkeypatch.setattr(wipe_cmd, "_stop_systemd_service", lambda: None)
    monkeypatch.setattr(wipe_cmd, "_stop_pid_daemon", lambda home: None)
    monkeypatch.setattr(wipe_cmd, "_remove_berth_docker", lambda: [])
    monkeypatch.setattr(wipe_cmd.config, "BERTH_DIR", tmp_path / "real-berth")

    result = CliRunner().invoke(cli.app, ["wipe", "--home", str(home), "--yes"])

    assert result.exit_code == 0, result.output


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
