from __future__ import annotations

import tomllib

import pytest

from serve_engine import config


@pytest.fixture(autouse=True)
def _isolated_serve_home(tmp_path, monkeypatch):
    """Point SERVE_HOME at a tmp dir for every test in this module."""
    monkeypatch.setattr(config, "SERVE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml", raising=True)
    return tmp_path


def test_resolve_defaults_when_nothing_set(monkeypatch):
    # Force autodetect to fail so we land on the literal default.
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(env={})
    assert r.public_host == config.DEFAULT_PUBLIC_HOST
    assert r.public_port == config.DEFAULT_PUBLIC_PORT
    assert r.public_bind == config.DEFAULT_BIND
    assert r.cluster_port == config.DEFAULT_CLUSTER_PORT
    assert r.source["public_host"] == "default"
    assert r.source["cluster_port"] == "default"


def test_flag_beats_env_beats_file_beats_autodetect(_isolated_serve_home, monkeypatch):
    # File says one thing.
    config.save_config_file({"public": {"host": "file.example.com"}})
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: "10.9.9.9")

    # File only.
    r = config.resolve_config(env={})
    assert r.public_host == "file.example.com"
    assert r.source["public_host"] == "file"

    # Env beats file.
    r = config.resolve_config(env={"SERVE_PUBLIC_HOST": "env.example.com"})
    assert r.public_host == "env.example.com"
    assert r.source["public_host"] == "env"

    # Flag beats env.
    r = config.resolve_config(
        env={"SERVE_PUBLIC_HOST": "env.example.com"},
        cli_public_host="flag.example.com",
    )
    assert r.public_host == "flag.example.com"
    assert r.source["public_host"] == "flag"


def test_cluster_inherits_public_when_unset(monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(
        env={}, cli_public_host="x.example.com",
    )
    assert r.cluster_host == "x.example.com"
    assert r.source["cluster_host"].startswith("inherit:")


def test_leader_url_override(monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(env={"SERVE_LEADER_URL": "https://forced:99"})
    assert r.cluster_url == "https://forced:99"
    assert r.source["leader_url"] == "env:SERVE_LEADER_URL"


def test_save_config_file_round_trip(_isolated_serve_home):
    config.save_config_file({
        "public": {"host": "a.com", "port": 8443},
        "cluster": {"bind": "10.0.0.1"},
    })
    assert _isolated_serve_home.joinpath("config.toml").exists()
    loaded = tomllib.loads(_isolated_serve_home.joinpath("config.toml").read_text())
    assert loaded["public"]["host"] == "a.com"
    assert loaded["public"]["port"] == 8443
    assert loaded["cluster"]["bind"] == "10.0.0.1"

    # Setting None removes a key.
    config.save_config_file({"public": {"port": None}})
    loaded = tomllib.loads(_isolated_serve_home.joinpath("config.toml").read_text())
    assert "port" not in loaded["public"]
    assert loaded["public"]["host"] == "a.com"  # unrelated keys preserved
