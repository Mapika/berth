from __future__ import annotations

from typer.testing import CliRunner

from berth.cli import app, nodes_cmd


def test_nodes_ls(monkeypatch):
    captured = {}

    def _uds_get(path):
        captured["path"] = path
        return {"nodes": [
            {"id": 0, "label": "local", "status": "ready",
             "gpu_count": 1, "total_vram_mb": 80000,
             "agent_version": "0.0.1"},
            {"id": 1, "label": "agent-a", "status": "ready",
             "gpu_count": 2, "total_vram_mb": 160000,
             "agent_version": "0.0.1"},
        ]}

    monkeypatch.setattr(nodes_cmd, "_uds_get", _uds_get)
    r = CliRunner().invoke(app, ["nodes", "ls"])
    assert r.exit_code == 0, r.output
    assert "local" in r.output and "agent-a" in r.output
    assert captured["path"] == "/admin/nodes"


def test_nodes_enroll_prints_uri(monkeypatch):
    def _uds_post(path, body):
        return {
            "token": "tok-xyz",
            "leader_url": "https://leader:11501",
            "ca_cert": "ca-pem",
            "ca_fingerprint": "sha256:deadbeef",
        }

    monkeypatch.setattr(nodes_cmd, "_uds_post", _uds_post)
    r = CliRunner().invoke(app, ["nodes", "enroll", "agent-a"])
    assert r.exit_code == 0
    assert "serve://enroll?" in r.output
    assert "leader=https" in r.output
    assert "token=tok-xyz" in r.output
    assert "ca_fp=sha256" in r.output
    assert "serve agent register --uri" in r.output


def test_nodes_show(monkeypatch):
    def _uds_get(path):
        return {
            "node": {"label": "agent-a", "status": "ready",
                     "agent_version": "0.0.1",
                     "cpu_count": 8, "total_ram_mb": 32000},
            "gpus": [
                {"gpu_index": 0, "name": "H100", "total_vram_mb": 81920},
            ],
        }

    monkeypatch.setattr(nodes_cmd, "_uds_get", _uds_get)
    r = CliRunner().invoke(app, ["nodes", "show", "1"])
    assert r.exit_code == 0
    assert "agent-a" in r.output
    assert "H100" in r.output


def test_nodes_remove(monkeypatch):
    captured = {}

    def _uds_delete(path):
        captured["path"] = path

    monkeypatch.setattr(nodes_cmd, "_uds_delete", _uds_delete)
    r = CliRunner().invoke(app, ["nodes", "remove", "5"])
    assert r.exit_code == 0
    assert "removed node 5" in r.output
    assert captured["path"] == "/admin/nodes/5"


def test_build_enrollment_uri_round_trip():
    uri = nodes_cmd.build_enrollment_uri(
        leader="https://example.com:11501",
        token="abc 123",
        ca_fp="sha256:7f3a",
    )
    assert uri.startswith("serve://enroll?")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(uri).query)
    assert q["leader"] == ["https://example.com:11501"]
    assert q["token"] == ["abc 123"]
    assert q["ca_fp"] == ["sha256:7f3a"]
