import httpx
import pytest

from berth.cli import ipc


@pytest.mark.asyncio
async def test_ipc_get_uses_uds_transport(tmp_path, monkeypatch):
    sock = tmp_path / "sock"
    captured = {}

    class StubClient:
        def __init__(self, transport, base_url, timeout):
            captured["transport"] = transport
            captured["base_url"] = base_url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, path):
            captured["path"] = path
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(ipc.httpx, "AsyncClient", StubClient)
    result = await ipc.get(sock, "/admin/models")
    assert result == {"ok": True}
    assert isinstance(captured["transport"], httpx.AsyncHTTPTransport)
    assert captured["base_url"] == "http://daemon"
    assert captured["path"] == "/admin/models"
