"""End-to-end test of the secure-by-default URI enrollment flow.

Uses an in-process ASGI transport (no real TLS) for the registration
HTTP path. Verifies:
  - `nodes enroll` returns leader_url + token + ca_fingerprint
  - `berth://enroll?...` round-trips through parse/build
  - `/admin/ca.pem` returns the CA with the matching fingerprint header
  - a fingerprint-mismatch CA is rejected by the pin check
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import httpx
import pytest

from berth.backends.vllm import VLLMBackend
from berth.cli.agent_cmd import parse_enrollment_uri
from berth.cli.nodes_cmd import build_enrollment_uri
from berth.daemon.app import build_app
from berth.store import db


@pytest.mark.asyncio
async def test_enrollment_uri_flow_end_to_end(tmp_path):
    from berth.lifecycle.topology import GPUInfo, Topology

    docker_client = MagicMock()
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    app = build_app(
        conn=conn,
        docker_client=docker_client,
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
        berth_home=tmp_path,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # 1) Operator mints an enrollment token. The response now includes
        # the CA fingerprint so the CLI can build a URI.
        enroll = (await c.post(
            "/admin/nodes/enroll", json={"label": "agent-x"},
        )).json()
        assert "token" in enroll
        assert "leader_url" in enroll
        assert "ca_fingerprint" in enroll
        assert enroll["ca_fingerprint"].startswith("sha256:")

        # 2) The CLI bundles it into a berth://enroll URI.
        uri = build_enrollment_uri(
            leader=enroll["leader_url"],
            token=enroll["token"],
            ca_fp=enroll["ca_fingerprint"],
        )
        leader, token, ca_fp = parse_enrollment_uri(uri)
        assert leader == enroll["leader_url"]
        assert token == enroll["token"]
        assert ca_fp == enroll["ca_fingerprint"]

        # 3) The agent fetches /admin/ca.pem and verifies the fingerprint
        # before trusting the CA. The header MUST match the URI's pin.
        r = await c.get("/admin/ca.pem")
        assert r.status_code == 200
        assert r.headers["x-berth-ca-fingerprint"] == ca_fp
        actual_fp = "sha256:" + hashlib.sha256(
            r.text.encode("utf-8")
        ).hexdigest()
        assert actual_fp == ca_fp  # the file's hash matches the pin

        # 4) Once the CA is trusted, registration proceeds normally.
        reg = (await c.post("/admin/nodes/register", json={
            "token": token,
            "host_info": {"cpu_count": 1, "total_ram_mb": 1024,
                          "gpu_count": 1, "total_vram_mb": 81920,
                          "gpus": [{"index": 0, "name": "H100",
                                    "total_vram_mb": 81920}]},
        })).json()
        assert "agent_cert" in reg


@pytest.mark.asyncio
async def test_ca_pem_endpoint_returns_pem_and_fingerprint(tmp_path):
    """The cluster CA endpoint must be reachable without auth and must
    expose the same fingerprint the enrollment URI pins."""
    from berth.lifecycle.topology import GPUInfo, Topology

    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    topology = Topology(
        gpus=[GPUInfo(index=0, name="H100", total_mb=80 * 1024)],
        _islands={0: frozenset({0})},
    )
    app = build_app(
        conn=conn,
        docker_client=MagicMock(),
        backends={"vllm": VLLMBackend()},
        models_dir=tmp_path,
        topology=topology,
        berth_home=tmp_path,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/admin/ca.pem")
    assert r.status_code == 200
    assert r.text.startswith("-----BEGIN CERTIFICATE-----")
    fp = r.headers["x-berth-ca-fingerprint"]
    assert fp == "sha256:" + hashlib.sha256(r.text.encode("utf-8")).hexdigest()
