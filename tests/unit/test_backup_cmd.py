"""`serve backup create` produces a tarball of the DR set."""
from __future__ import annotations

import tarfile

from typer.testing import CliRunner

from berth import cli, config
from berth.store import db


def test_backup_create_writes_tarball_with_dr_set(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SERVE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml")

    # Lay out a minimal "live state" tree.
    (tmp_path / "ca").mkdir()
    (tmp_path / "ca" / "ca.crt").write_text("FAKE CERT")
    (tmp_path / "ca" / "ca.key").write_text("FAKE KEY")
    (tmp_path / "key_pepper").write_bytes(b"\x00" * 32)
    (tmp_path / "config.toml").write_text('[public]\nhost = "x"\n')
    conn = db.connect(tmp_path / "db.sqlite")
    db.init_schema(conn)

    dest = tmp_path / "snap.tar.gz"
    runner = CliRunner()
    res = runner.invoke(cli.app, ["backup", "create", str(dest)])
    assert res.exit_code == 0, res.output
    assert dest.exists()

    with tarfile.open(dest, "r:gz") as tar:
        names = set(tar.getnames())
    assert "db.sqlite" in names
    assert "ca/ca.key" in names
    assert "ca/ca.crt" in names
    assert "key_pepper" in names
    assert "config.toml" in names
