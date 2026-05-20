from __future__ import annotations

import tomllib

import pytest

from berth import config


@pytest.fixture(autouse=True)
def _isolated_berth_home(tmp_path, monkeypatch):
    """Point BERTH_HOME at a tmp dir for every test in this module."""
    monkeypatch.setattr(config, "BERTH_DIR", tmp_path, raising=True)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.toml", raising=True)
    return tmp_path


def test_resolve_defaults_when_nothing_set(monkeypatch):
    # Force autodetect to fail so we land on the literal default.
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(env={})
    assert r.public_host == config.DEFAULT_PUBLIC_HOST
    assert r.public_port == config.DEFAULT_PUBLIC_PORT
    assert r.public_bind == "127.0.0.1"
    assert r.cluster_port == config.DEFAULT_CLUSTER_PORT
    assert r.cluster_bind == "127.0.0.1"
    assert r.source["public_host"] == "default"
    assert r.source["public_bind"] == "default"
    assert r.source["cluster_port"] == "default"
    assert r.source["cluster_bind"] == "default"


def test_flag_beats_env_beats_file_beats_autodetect(_isolated_berth_home, monkeypatch):
    # File says one thing.
    config.save_config_file({"public": {"host": "file.example.com"}})
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: "10.9.9.9")

    # File only.
    r = config.resolve_config(env={})
    assert r.public_host == "file.example.com"
    assert r.source["public_host"] == "file"

    # Env beats file.
    r = config.resolve_config(env={"BERTH_PUBLIC_HOST": "env.example.com"})
    assert r.public_host == "env.example.com"
    assert r.source["public_host"] == "env"

    # Flag beats env.
    r = config.resolve_config(
        env={"BERTH_PUBLIC_HOST": "env.example.com"},
        cli_public_host="flag.example.com",
    )
    assert r.public_host == "flag.example.com"
    assert r.source["public_host"] == "flag"


def test_removed_serve_env_names_are_ignored(monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(env={
        "SERVE_PUBLIC_HOST": "old.example.com",
        "SERVE_LEADER_URL": "https://old:99",
    })
    assert r.public_host == config.DEFAULT_PUBLIC_HOST
    assert r.cluster_url == f"https://{config.DEFAULT_PUBLIC_HOST}:11501"
    assert "leader_url" not in r.source


def test_cluster_inherits_public_when_unset(monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(
        env={}, cli_public_host="x.example.com",
    )
    assert r.cluster_host == "x.example.com"
    assert r.source["cluster_host"].startswith("inherit:")


def test_leader_url_override(monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(env={"BERTH_LEADER_URL": "https://forced:99"})
    assert r.cluster_url == "https://forced:99"
    assert r.source["leader_url"] == "env:BERTH_LEADER_URL"


def test_reverse_proxy_mode_defaults(monkeypatch):
    """Default config keeps TLS-direct semantics — no proxy mode."""
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(env={})
    assert r.public_scheme == "https"
    assert r.trust_proxy_headers is False
    assert r.forwarded_allow_ips == "127.0.0.1"


def test_reverse_proxy_mode_from_env(monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    r = config.resolve_config(env={
        "BERTH_PUBLIC_SCHEME": "http",
        "BERTH_TRUST_PROXY_HEADERS": "true",
        "BERTH_FORWARDED_ALLOW_IPS": "10.0.0.5,10.0.0.6",
    })
    assert r.public_scheme == "http"
    assert r.trust_proxy_headers is True
    assert r.forwarded_allow_ips == "10.0.0.5,10.0.0.6"
    assert r.public_url == f"http://{r.public_host}:{r.public_port}"


def test_leader_only_resolution_flag_env_file(_isolated_berth_home, monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)
    config.save_config_file({"server": {"leader_only": True}})

    r = config.resolve_config(env={})
    assert r.leader_only is True
    assert r.source["leader_only"] == "file"

    r = config.resolve_config(env={"BERTH_LEADER_ONLY": "false"})
    assert r.leader_only is False
    assert r.source["leader_only"] == "env"

    r = config.resolve_config(
        env={"BERTH_LEADER_ONLY": "false"},
        cli_leader_only=True,
    )
    assert r.leader_only is True
    assert r.source["leader_only"] == "flag"


def test_unsafe_deploy_options_default_off_and_opt_in(_isolated_berth_home, monkeypatch):
    monkeypatch.setattr(config, "autodetect_outbound_ip", lambda: None)

    r = config.resolve_config(env={})
    assert r.allow_unsafe_deploy_options is False
    assert r.source["allow_unsafe_deploy_options"] == "default"

    config.save_config_file({"server": {"allow_unsafe_deploy_options": True}})
    r = config.resolve_config(env={})
    assert r.allow_unsafe_deploy_options is True
    assert r.source["allow_unsafe_deploy_options"] == "file"

    r = config.resolve_config(env={"BERTH_ALLOW_UNSAFE_DEPLOY_OPTIONS": "false"})
    assert r.allow_unsafe_deploy_options is False
    assert r.source["allow_unsafe_deploy_options"] == "env"


def test_save_config_file_round_trip(_isolated_berth_home):
    config.save_config_file({
        "public": {"host": "a.com", "port": 8443},
        "cluster": {"bind": "10.0.0.1"},
    })
    assert _isolated_berth_home.joinpath("config.toml").exists()
    loaded = tomllib.loads(_isolated_berth_home.joinpath("config.toml").read_text())
    assert loaded["public"]["host"] == "a.com"
    assert loaded["public"]["port"] == 8443
    assert loaded["cluster"]["bind"] == "10.0.0.1"

    # Setting None removes a key.
    config.save_config_file({"public": {"port": None}})
    loaded = tomllib.loads(_isolated_berth_home.joinpath("config.toml").read_text())
    assert "port" not in loaded["public"]
    assert loaded["public"]["host"] == "a.com"  # unrelated keys preserved
