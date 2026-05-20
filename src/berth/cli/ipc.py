from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import click
import httpx

BASE_URL = "http://daemon"


def _client(sock: Path) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=str(sock))
    return httpx.AsyncClient(transport=transport, base_url=BASE_URL, timeout=600.0)


def _raise_for_status(r: httpx.Response) -> None:
    try:
        detail = r.json().get("detail", r.text)
    except Exception:
        detail = r.text
    raise RuntimeError(f"daemon error {r.status_code}: {detail}")


def _raise_connect_error(sock: Path) -> NoReturn:
    raise click.ClickException(
        f"could not connect to berth daemon control socket at {sock}. "
        "Start the berth service and make sure this command uses the same "
        "BERTH_HOME as the daemon."
    )


async def get(sock: Path, path: str) -> Any:
    try:
        async with _client(sock) as c:
            r = await c.get(path)
    except httpx.ConnectError:
        _raise_connect_error(sock)
    else:
        if r.status_code >= 400:
            _raise_for_status(r)
        return r.json()


async def post(sock: Path, path: str, *, json: dict[str, Any] | None = None) -> Any:
    try:
        async with _client(sock) as c:
            r = await c.post(path, json=json)
    except httpx.ConnectError:
        _raise_connect_error(sock)
    else:
        if r.status_code >= 400:
            _raise_for_status(r)
        if r.status_code == 204:
            return None
        return r.json()


async def delete(sock: Path, path: str) -> None:
    try:
        async with _client(sock) as c:
            r = await c.delete(path)
    except httpx.ConnectError:
        _raise_connect_error(sock)
    else:
        if r.status_code >= 400 and r.status_code != 404:
            _raise_for_status(r)
