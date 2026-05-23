from __future__ import annotations

from berth.daemon import openai_proxy


def test_responses_route_registered():
    paths = {r.path for r in openai_proxy.router.routes}
    assert "/v1/responses" in paths

def test_responses_route_is_post():
    methods = set()
    for r in openai_proxy.router.routes:
        if getattr(r, "path", None) == "/v1/responses":
            methods |= set(r.methods or [])
    assert "POST" in methods
