from __future__ import annotations

import re

import httpx

# Strict semver: vX.Y.Z, optionally .postN. No nightly, no -cu12, no -dev.
_STABLE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(\.post\d+)?$")


def latest_stable_tag(image: str, *, timeout_s: float = 10.0) -> str | None:
    """Query Docker Hub for the most-recently-pushed stable tag for `image`.

    `image` is "namespace/repo" (no tag). Returns None if no stable tags
    are found or if the network call fails.
    """
    url = (
        f"https://hub.docker.com/v2/repositories/{image}/tags/"
        f"?page_size=100&ordering=last_updated"
    )
    try:
        r = httpx.get(url, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    for t in data.get("results", []):
        name = t.get("name", "")
        if _STABLE_TAG_RE.match(name):
            return name
    return None
