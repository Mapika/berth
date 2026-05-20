"""`berth deploy bootstrap` provisions a fresh VPS to a ready-to-start
configuration: writes config.toml, initialises the DB + CA + pepper,
mints the first admin key, prints next-step instructions."""
from __future__ import annotations

import re
import stat
import tomllib

from typer.testing import CliRunner

from berth import cli, config
from berth.cli.deploy_cmd import _bootstrap, _ok_hostname
from berth.store import api_keys, db

# GitHub Actions runners set FORCE_COLOR=1, which makes Typer's Click error
# formatter highlight option names by wrapping each hyphen-separated segment
# in its own ANSI colour escape. A naive substring search for "--cluster-domain"
# then fails because the captured bytes are ``--\e[...m-cluster\e[0m\e[...m-domain\e[0m``.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _isolate(monkeypatch, home):
    """Reset module-level state between tests so we don't poison the
    rest of the suite (api_keys has a module-level pepper cache)."""
    monkeypatch.setattr(config, "BERTH_DIR", home)
    monkeypatch.setattr(config, "DB_PATH", home / "db.sqlite")
    monkeypatch.setattr(config, "CONFIG_FILE", home / "config.toml")
    monkeypatch.setattr(api_keys, "_PEPPER_PATH", None)
    monkeypatch.setattr(api_keys, "_PEPPER_CACHED", None)


def test_ok_hostname_accepts_real_names():
    assert _ok_hostname("berth.example.com")
    assert _ok_hostname("a-host.io")
    assert _ok_hostname("localhost")


def test_ok_hostname_rejects_garbage():
    assert not _ok_hostname("")
    assert not _ok_hostname("has spaces")
    assert not _ok_hostname("trailing-")
    assert not _ok_hostname("-leading")


def test_bootstrap_writes_config_with_behind_proxy_defaults(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    out = _bootstrap(
        domain="berth.example.com",
        public_port=11500,
        cluster_port=11501,
        behind_proxy=True,
        berth_home=tmp_path,
        force=False,
    )
    assert "wrote" in out["config_status"]
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["public"]["host"] == "berth.example.com"
    assert cfg["public"]["scheme"] == "http"
    assert cfg["public"]["bind"] == "127.0.0.1"
    assert cfg["public"]["trust_proxy_headers"] is True
    assert cfg["cluster"]["bind"] == "0.0.0.0"


def test_bootstrap_writes_sni_443_leader_only_config(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    out = _bootstrap(
        domain="leader.example.com",
        cluster_domain="cluster.example.com",
        public_port=11500,
        cluster_port=11501,
        public_tls_port=8443,
        behind_proxy=True,
        sni_443=True,
        leader_only=True,
        berth_home=tmp_path,
        force=False,
    )

    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["server"]["leader_only"] is True
    assert cfg["public"]["host"] == "leader.example.com"
    assert cfg["public"]["bind"] == "127.0.0.1"
    assert cfg["cluster"]["host"] == "cluster.example.com"
    assert cfg["cluster"]["port"] == 11501
    assert cfg["cluster"]["bind"] == "127.0.0.1"
    assert out["leader_url"] == "https://cluster.example.com"


def test_bootstrap_writes_direct_tls_config(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    out = _bootstrap(
        domain="berth.example.com",
        public_port=11500,
        cluster_port=11501,
        behind_proxy=False,
        berth_home=tmp_path,
        force=False,
    )
    assert "wrote" in out["config_status"]
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["public"]["bind"] == "0.0.0.0"
    assert "scheme" not in cfg["public"]  # default https


def test_bootstrap_is_idempotent_and_preserves_existing_config(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text(
        '[public]\nhost = "preexisting"\nport = 9999\n'
    )
    out = _bootstrap(
        domain="berth.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=tmp_path, force=False,
    )
    assert "not overwritten" in out["config_status"]
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["public"]["host"] == "preexisting"


def test_bootstrap_force_overwrites_existing_config(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text(
        '[public]\nhost = "preexisting"\n'
    )
    _bootstrap(
        domain="new.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=tmp_path, force=True,
    )
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["public"]["host"] == "new.example.com"


def test_bootstrap_initialises_db_ca_and_pepper(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    out = _bootstrap(
        domain="berth.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=tmp_path, force=False,
    )
    assert (tmp_path / "db.sqlite").exists()
    assert (tmp_path / "ca" / "ca.crt").exists()
    assert (tmp_path / "ca" / "ca.key").exists()
    assert (tmp_path / "key_pepper").exists()
    assert "migrations applied" in out["db_status"]


def test_bootstrap_makes_existing_home_owner_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    _isolate(monkeypatch, home)

    _bootstrap(
        domain="berth.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=home, force=False,
    )

    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_bootstrap_mints_first_admin_key_when_table_empty(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    out = _bootstrap(
        domain="berth.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=tmp_path, force=False,
    )
    assert out["first_key"].startswith("sk-")
    # And the key actually verifies against the DB.
    conn = db.connect(tmp_path / "db.sqlite")
    api_keys.configure_pepper(tmp_path / "key_pepper")
    found = api_keys.verify(conn, out["first_key"])
    assert found is not None
    assert found.tier == "admin"


def test_bootstrap_skips_key_mint_when_keys_already_exist(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    # First run mints a key.
    _bootstrap(
        domain="berth.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=tmp_path, force=False,
    )
    # Second run on the same home must not mint another.
    out2 = _bootstrap(
        domain="berth.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=tmp_path, force=False,
    )
    assert out2["first_key"] == ""


def test_bootstrap_renders_caddyfile_for_domain(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    out = _bootstrap(
        domain="berth.example.com",
        public_port=11500, cluster_port=11501,
        behind_proxy=True, berth_home=tmp_path, force=False,
    )
    cf = out["caddyfile"]
    assert "berth.example.com" in cf
    assert "127.0.0.1:11500" in cf
    assert "X-Forwarded-Proto" in cf
    assert "Strict-Transport-Security" in cf


def test_bootstrap_renders_sni_443_proxy_configs(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    out = _bootstrap(
        domain="leader.example.com",
        cluster_domain="cluster.example.com",
        public_port=11500,
        cluster_port=11501,
        public_tls_port=8443,
        behind_proxy=True,
        sni_443=True,
        leader_only=True,
        berth_home=tmp_path,
        force=False,
    )

    assert "https://leader.example.com:8443" in out["caddyfile"]
    assert "bind 127.0.0.1" in out["caddyfile"]
    assert "http://cluster.example.com" in out["caddyfile"]
    assert "respond 404" in out["caddyfile"]
    assert "-Alt-Svc" in out["caddyfile"]
    assert "Strict-Transport-Security" in out["caddyfile"]
    assert "127.0.0.1:11500" in out["caddyfile"]
    assert "req.ssl_sni -i cluster.example.com" in out["haproxy"]
    assert "req.ssl_sni -i leader.example.com" in out["haproxy"]
    assert "127.0.0.1:11501" in out["haproxy"]
    assert "BERTH_LEADER_URL=https://cluster.example.com" in out["systemd_unit"]


def test_cli_rejects_bad_domain(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli.app, [
        "deploy", "bootstrap",
        "--domain", "has spaces",
        "--berth-home", str(tmp_path),
    ])
    assert res.exit_code != 0
    assert "hostname" in res.output.lower()


def test_cli_sni_443_requires_cluster_domain(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli.app, [
        "deploy", "bootstrap",
        "--domain", "leader.example.com",
        "--sni-443",
        "--berth-home", str(tmp_path),
    ])
    assert res.exit_code != 0
    assert "cluster-domain" in _ANSI.sub("", res.output)


def test_cli_writes_files_and_prints_summary(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli.app, [
        "deploy", "bootstrap",
        "--domain", "berth.example.com",
        "--berth-home", str(tmp_path),
    ])
    assert res.exit_code == 0, res.output
    assert "sk-" in res.output  # first key printed
    assert "berth.example.com" in res.output
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "db.sqlite").exists()
