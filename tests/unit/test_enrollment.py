from __future__ import annotations

from berth.cluster.enrollment import EnrollmentTokens


def test_mint_and_consume_once():
    store = EnrollmentTokens(ttl_seconds=60, now=lambda: 1000.0)
    tok = store.mint(label="agent-a")
    assert store.consume(tok) == "agent-a"
    # Single-use: second consume must return None
    assert store.consume(tok) is None


def test_token_expires():
    t = {"now": 1000.0}
    store = EnrollmentTokens(ttl_seconds=60, now=lambda: t["now"])
    tok = store.mint(label="agent-a")
    t["now"] = 1061.0
    assert store.consume(tok) is None


def test_consume_unknown_token_returns_none():
    store = EnrollmentTokens(ttl_seconds=60, now=lambda: 1.0)
    assert store.consume("garbage") is None


def test_minted_tokens_are_unguessable():
    store = EnrollmentTokens(ttl_seconds=60, now=lambda: 1.0)
    tokens = {store.mint(label=f"a{i}") for i in range(50)}
    # All distinct, all at least 32 chars
    assert len(tokens) == 50
    assert all(len(t) >= 32 for t in tokens)


def test_mint_multiple_for_same_label_each_usable_once():
    store = EnrollmentTokens(ttl_seconds=60, now=lambda: 1.0)
    a = store.mint(label="agent-a")
    b = store.mint(label="agent-a")
    assert a != b
    assert store.consume(a) == "agent-a"
    assert store.consume(b) == "agent-a"
    assert store.consume(a) is None
