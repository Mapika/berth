from __future__ import annotations

import httpx
import yaml
from typer.testing import CliRunner

from berth.cli import app


def test_register_writes_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVE_HOME", str(tmp_path))

    class _MockResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "node_id": 7,
                "agent_cert": "-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n",
                "agent_key":  "-----BEGIN PRIVATE KEY-----\nB\n-----END PRIVATE KEY-----\n",
                "ca_cert":    "-----BEGIN CERTIFICATE-----\nC\n-----END CERTIFICATE-----\n",
            }

    posted: dict = {}

    def _post(url, json, timeout=None):
        posted["url"] = url
        posted["json"] = json
        return _MockResp()

    monkeypatch.setattr(httpx, "post", _post)

    r = CliRunner().invoke(app, [
        "agent", "register",
        "--leader", "https://leader.example:11500",
        "--token", "tok-123",
    ])
    assert r.exit_code == 0, r.output

    assert posted["url"].endswith("/admin/nodes/register")
    assert posted["json"]["token"] == "tok-123"
    assert "host_info" in posted["json"]

    cfg = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    assert cfg["leader_url"] == "https://leader.example:11500"
    assert cfg["node_id"] == 7
    assert (tmp_path / "agent.crt").exists()
    assert (tmp_path / "agent.key").exists()
    assert (tmp_path / "ca.crt").exists()
    # Key file should be 0o600.
    mode = (tmp_path / "agent.key").stat().st_mode & 0o777
    assert mode == 0o600


def test_status_when_not_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVE_HOME", str(tmp_path))
    r = CliRunner().invoke(app, ["agent", "status"])
    assert r.exit_code == 1
    assert "not registered" in r.output


def test_status_when_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVE_HOME", str(tmp_path))
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
