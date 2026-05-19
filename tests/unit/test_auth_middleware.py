import httpx
import pytest
from fastapi import Depends, FastAPI

from berth.auth import tiers
from berth.auth.middleware import require_auth_dep
from berth.store import api_keys, db, key_usage


@pytest.fixture
def app_factory(tmp_path):
    def make(create_admin_key: bool, **key_kwargs):
        conn = db.connect(tmp_path / "t.db")
        db.init_schema(conn)
        secret = None
        if create_admin_key:
            secret, _ = api_keys.create(conn, name="root", tier="admin", **key_kwargs)
        a = FastAPI()
        a.state.conn = conn
        a.state.tier_cfg = tiers.load_tiers()

        @a.post("/v1/test")
        async def _test(key=Depends(require_auth_dep)):
            return {
                "ok": True,
                "usage_event_id": None if key is None else key.usage_event_id,
            }

        return a, secret
    return make


@pytest.mark.asyncio
async def test_no_keys_table_empty_means_bypass(app_factory):
    """When the api_keys table is empty, auth is bypassed entirely."""
    app, _ = app_factory(create_admin_key=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/test")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_missing_bearer_when_keys_exist_401(app_factory):
    app, _ = app_factory(create_admin_key=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/test")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_bad_bearer_401(app_factory):
    app, _ = app_factory(create_admin_key=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/test", headers={"Authorization": "Bearer sk-bogus"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_good_bearer_passes(app_factory):
    app, secret = app_factory(create_admin_key=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/test", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_query_param_token_is_not_accepted(app_factory):
    app, secret = app_factory(create_admin_key=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(f"/v1/test?token={secret}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_query_param_invalid_token(app_factory):
    app, _ = app_factory(create_admin_key=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/test?token=sk-bogus")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_v1_request_is_reserved_before_next_rate_check(app_factory):
    app, secret = app_factory(create_admin_key=True, rpm_override=1)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r1 = await c.post(
            "/v1/test",
            headers={"Authorization": f"Bearer {secret}"},
        )
        r2 = await c.post(
            "/v1/test",
            headers={"Authorization": f"Bearer {secret}"},
        )

    assert r1.status_code == 200
    event_id = r1.json()["usage_event_id"]
    assert isinstance(event_id, int)
    assert r2.status_code == 429

    row = app.state.conn.execute(
        """
        SELECT key_id, tokens_in, tokens_out
        FROM key_usage_events
        WHERE id=?
        """,
        (event_id,),
    ).fetchone()
    assert row is not None
    assert (row["tokens_in"], row["tokens_out"]) == (0, 0)
    requests, _ = key_usage.totals_in_window(
        app.state.conn,
        key_id=row["key_id"],
        window_s=60,
    )
    assert requests == 1
