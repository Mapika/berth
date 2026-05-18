from __future__ import annotations

import httpx
from typer.testing import CliRunner

from serve_engine.cli import app


class _MockResp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


def test_nodes_ls(monkeypatch):
    monkeypatch.setenv("SERVE_URL", "http://x")
    monkeypatch.setenv("SERVE_TOKEN", "sk-abc")

    captured = {}

    def _get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _MockResp({"nodes": [
            {"id": 0, "label": "local", "status": "ready",
             "gpu_count": 1, "total_vram_mb": 80000,
             "agent_version": "0.0.1"},
            {"id": 1, "label": "agent-a", "status": "ready",
             "gpu_count": 2, "total_vram_mb": 160000,
             "agent_version": "0.0.1"},
        ]})

    monkeypatch.setattr(httpx, "get", _get)
    r = CliRunner().invoke(app, ["nodes", "ls"])
    assert r.exit_code == 0, r.output
    assert "local" in r.output and "agent-a" in r.output
    assert captured["headers"]["Authorization"] == "Bearer sk-abc"
    assert captured["url"] == "http://x/admin/nodes"


def test_nodes_enroll_prints_followup_command(monkeypatch):
    monkeypatch.setenv("SERVE_URL", "http://x")
    monkeypatch.delenv("SERVE_TOKEN", raising=False)

    def _post(url, json=None, headers=None, timeout=None):
        return _MockResp({
            "token": "tok-xyz",
            "leader_url": "https://leader:11500",
            "ca_cert": "ca-pem",
        })

    monkeypatch.setattr(httpx, "post", _post)
    r = CliRunner().invoke(app, ["nodes", "enroll", "agent-a"])
    assert r.exit_code == 0
    assert "tok-xyz" in r.output
    assert "serve agent register" in r.output


def test_nodes_show(monkeypatch):
    monkeypatch.setenv("SERVE_URL", "http://x")
    monkeypatch.delenv("SERVE_TOKEN", raising=False)

    def _get(url, headers=None, timeout=None):
        return _MockResp({
            "node": {"label": "agent-a", "status": "ready",
                     "agent_version": "0.0.1",
                     "cpu_count": 8, "total_ram_mb": 32000},
            "gpus": [
                {"gpu_index": 0, "name": "H100", "total_vram_mb": 81920},
            ],
        })

    monkeypatch.setattr(httpx, "get", _get)
    r = CliRunner().invoke(app, ["nodes", "show", "1"])
    assert r.exit_code == 0
    assert "agent-a" in r.output
    assert "H100" in r.output


def test_nodes_remove(monkeypatch):
    monkeypatch.setenv("SERVE_URL", "http://x")
    monkeypatch.delenv("SERVE_TOKEN", raising=False)

    captured = {}

    def _delete(url, headers=None, timeout=None):
        captured["url"] = url
        return _MockResp({"ok": True})

    monkeypatch.setattr(httpx, "delete", _delete)
    r = CliRunner().invoke(app, ["nodes", "remove", "5"])
    assert r.exit_code == 0
    assert "removed node 5" in r.output
    assert captured["url"] == "http://x/admin/nodes/5"
