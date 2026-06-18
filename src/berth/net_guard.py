"""Guards for dialing engine endpoints whose address may be untrusted.

Managed deployments get their ``container_address`` from berth's own Docker
launch, so it is trusted. *Adopted* deployments (and, transitively, handles
reported by enrolled agents) carry an operator/agent-supplied address that
berth then dials on the inference hot path and for adapter load/unload. That
is an SSRF primitive: a malicious agent or an attacker-influenced adopt
definition could point the leader at an internal service.

The highest-value SSRF target is the cloud metadata endpoint
(169.254.169.254, inside the link-local range). We block that class of
address for adopted endpoints while deliberately *allowing* loopback and
ordinary private/public addresses — adopting an engine running on localhost
or a LAN host is a legitimate workflow. Non-IP hostnames pass through; the
operator owns their DNS.
"""
from __future__ import annotations

from ipaddress import ip_address

# The "tunnel" sentinel means a remote deployment reachable only via the
# agent WS tunnel; it is never direct-dialed (callers special-case it).
TUNNEL_SENTINEL = "tunnel"


def is_blocked_adopted_address(address: str | None) -> bool:
    """True when an adopted deployment's address is an unsafe SSRF target.

    Only IP-literal addresses are inspected. We block link-local
    (169.254.0.0/16 — includes the 169.254.169.254 cloud metadata
    endpoint), multicast, reserved, and the unspecified address. Loopback
    and ordinary private/public addresses are allowed (legitimate
    adopt-localhost / adopt-LAN). Non-IP hostnames return False — the
    operator is trusted for their own DNS.
    """
    if not address or address == TUNNEL_SENTINEL:
        return False
    try:
        ip = ip_address(address)
    except ValueError:
        return False
    return (
        ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_dialable_engine(deployment) -> None:
    """Raise ValueError if an adopted deployment points at an unsafe address.

    No-op for managed deployments (trusted address) and for the tunnel
    sentinel (never direct-dialed).
    """
    if getattr(deployment, "source", "managed") != "adopted":
        return
    address = getattr(deployment, "container_address", None)
    if is_blocked_adopted_address(address):
        raise ValueError(
            f"refusing to dial adopted engine at unsafe address {address!r} "
            "(link-local/metadata/multicast addresses are blocked)"
        )
