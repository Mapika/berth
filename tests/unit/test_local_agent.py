from __future__ import annotations

import httpx
import pytest

from berth.cluster.local_agent import LocalAgentLink
from berth.lifecycle.docker_client import DockerClient


class _FakeContainer:
    def __init__(self, cid: str, port: int):
        self.id = cid
        self.name = "x"
        self.attrs = {
            "NetworkSettings": {
                "Ports": {f"{port}/tcp": [{"HostPort": str(port)}]}
            }
        }
    def reload(self): pass
    def stop(self, timeout): pass
    def remove(self): pass


class _FakeContainers:
    def __init__(self):
        self._c: _FakeContainer | None = None
    def run(self, **kw):
        self._c = _FakeContainer("cid-1", 9000)
        return self._c
    def get(self, cid):
        return self._c


class _FakeNetworks:
    def get(self, name): pass
    def create(self, *a, **k): pass


class _FakeDocker:
    def __init__(self):
        self.containers = _FakeContainers()
        self.networks = _FakeNetworks()


@pytest.mark.asyncio
async def test_start_returns_address_and_port():
    dc = DockerClient(client=_FakeDocker(), network_name="berth")
    link = LocalAgentLink(node_id=0, docker_client=dc)
    started = await link.start_deployment({
        "image": "x", "name": "d-1", "command": [], "environment": {},
        "kwargs": {}, "volumes": {}, "internal_port": 9000,
    })
    assert started.container_id == "cid-1"
    assert started.address == "127.0.0.1"
    assert started.port == 9000


@pytest.mark.asyncio
async def test_proxy_request_streams_response():
    dc = DockerClient(client=_FakeDocker(), network_name="berth")

    async def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield b"hello "
            yield b"world"
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=gen(),
        )

    transport = httpx.MockTransport(handler)
    link = LocalAgentLink(node_id=0, docker_client=dc,
                          transport_for_test=transport)
    link.register_endpoint(container_id="cid-1", address="127.0.0.1", port=9000)

    chunks = []
    async for c in link.proxy_request(
        container_id="cid-1", method="GET", path="/",
        headers={}, body=b"",
    ):
        chunks.append(c)
    assert chunks[0].status == 200
    assert b"".join(c.body for c in chunks) == b"hello world"
    assert chunks[-1].eof is True


@pytest.mark.asyncio
async def test_stop_clears_endpoint():
    dc = DockerClient(client=_FakeDocker(), network_name="berth")
    link = LocalAgentLink(node_id=0, docker_client=dc)
    started = await link.start_deployment({
        "image": "x", "name": "d-1", "command": [], "environment": {},
        "kwargs": {}, "volumes": {}, "internal_port": 9000,
    })
    assert started.container_id in link._endpoints
    await link.stop_deployment(started.container_id)
    assert started.container_id not in link._endpoints


@pytest.mark.asyncio
async def test_proxy_unknown_container_raises():
    dc = DockerClient(client=_FakeDocker(), network_name="berth")
    link = LocalAgentLink(node_id=0, docker_client=dc)
    with pytest.raises(KeyError):
        async for _ in link.proxy_request(
            container_id="missing", method="GET", path="/",
            headers={}, body=b"",
        ):
            pass


def test_link_properties():
    dc = DockerClient(client=_FakeDocker(), network_name="berth")
    link = LocalAgentLink(node_id=7, docker_client=dc)
    assert link.node_id == 7
    assert link.is_ready is True
