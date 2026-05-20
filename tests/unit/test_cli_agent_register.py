from __future__ import annotations

import hashlib
import os
import stat

import httpx
import yaml
from typer.testing import CliRunner

from berth.cli import app


def test_register_writes_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o755)
    monkeypatch.setenv("BERTH_HOME", str(home))
    ca_pem = "-----BEGIN CERTIFICATE-----\nC\n-----END CERTIFICATE-----\n"
    ca_fp = "sha256:" + hashlib.sha256(ca_pem.encode("utf-8")).hexdigest()

    class _MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "node_id": 7,
                "agent_cert": "-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n",
                "agent_key": (
                    "-----BEGIN "
                    "PRIVATE KEY-----\nB\n-----END "
                    "PRIVATE KEY-----\n"
                ),
            }

    posted: dict = {}

    def _get(url, verify=None, timeout=None):
        class _CAResp:
            text = ca_pem

            def raise_for_status(self): pass

        return _CAResp()

    def _post(url, json, verify=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        posted["verify"] = verify
        return _MockResp()

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", _post)

    r = CliRunner().invoke(app, [
        "agent", "register",
        "--uri", (
            "berth://enroll?leader=https%3A%2F%2Fleader.example%3A11500"
            f"&token=tok-123&ca_fp={ca_fp}"
        ),
    ])
    assert r.exit_code == 0, r.output

    assert posted["url"].endswith("/admin/nodes/register")
    assert posted["json"]["token"] == "tok-123"
    assert posted["verify"] == str(home / "ca.crt")
    assert "host_info" in posted["json"]

    cfg = yaml.safe_load((home / "agent.yaml").read_text())
    assert cfg["leader_url"] == "https://leader.example:11500"
    assert cfg["node_id"] == 7
    assert (home / "agent.crt").exists()
    assert (home / "agent.key").exists()
    assert (home / "ca.crt").exists()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    # Key file should be 0o600.
    mode = (home / "agent.key").stat().st_mode & 0o777
    assert mode == 0o600


def test_register_rejects_legacy_leader_token_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    r = CliRunner().invoke(app, [
        "agent", "register",
        "--leader", "https://leader.example:11500",
        "--token", "tok-123",
    ])
    assert r.exit_code != 0
    assert "No such option" in r.output


def test_register_agent_key_owner_only_if_write_stops_before_chmod(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BERTH_HOME", str(home))
    key_path = home / "agent.key"

    class _MockResp:
        status_code = 200

        def raise_for_status(self): pass

        def json(self):
            return {
                "node_id": 7,
                "agent_cert": "-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n",
                "agent_key": (
                    "-----BEGIN "
                    "PRIVATE KEY-----\nB\n-----END "
                    "PRIVATE KEY-----\n"
                ),
            }

    def _post(url, json, verify=None, timeout=None):
        return _MockResp()

    real_chmod = os.chmod

    def fail_key_chmod(path, mode, *args, **kwargs):
        if path == key_path and mode == 0o600:
            raise RuntimeError("simulated interruption")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(os, "chmod", fail_key_chmod)
    old_umask = os.umask(0)
    try:
        from berth.cli.agent_cmd import _do_register

        try:
            _do_register(
                leader="https://leader.example:11500",
                token="tok-123",
                ca_pem="-----BEGIN CERTIFICATE-----\nC\n-----END CERTIFICATE-----\n",
                reachable_as=None,
            )
        except RuntimeError:
            pass
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_status_when_not_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    r = CliRunner().invoke(app, ["agent", "status"])
    assert r.exit_code == 1
    assert "not registered" in r.output


def test_status_when_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("BERTH_HOME", str(tmp_path))
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump({
        "leader_url": "https://x:1",
        "node_id": 3,
        "agent_cert_path": str(tmp_path / "agent.crt"),
        "agent_key_path": str(tmp_path / "agent.key"),
        "ca_cert_path": str(tmp_path / "ca.crt"),
        "reachable_as": None,
    }))
    r = CliRunner().invoke(app, ["agent", "status"])
    assert r.exit_code == 0
    assert "node_id  : 3" in r.output
    assert "https://x:1" in r.output
