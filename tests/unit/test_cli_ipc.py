import click
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


@pytest.mark.asyncio
async def test_ipc_connect_error_mentions_socket_and_berth_home(tmp_path, monkeypatch):
    sock = tmp_path / "missing-sock"

    class StubClient:
        def __init__(self, transport, base_url, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, path, json=None):
            raise httpx.ConnectError("no such file")

    monkeypatch.setattr(ipc.httpx, "AsyncClient", StubClient)

    with pytest.raises(click.ClickException) as excinfo:
        await ipc.post(sock, "/admin/keys", json={"name": "root"})

    msg = str(excinfo.value)
    assert str(sock) in msg
    assert "BERTH_HOME" in msg
