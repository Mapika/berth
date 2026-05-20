from berth.store import api_keys, db


def _fresh(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_create_returns_full_key_once(tmp_path):
    conn = _fresh(tmp_path)
    secret, key = api_keys.create(conn, name="alice", tier="standard")
    assert secret.startswith("sk-")
    assert len(secret) >= 32
    assert key.prefix == secret[:12]
    assert key.name == "alice"
    assert key.tier == "standard"


def test_verify_matches_hashed(tmp_path):
    conn = _fresh(tmp_path)
    secret, _ = api_keys.create(conn, name="alice", tier="standard")
    found = api_keys.verify(conn, secret)
    assert found is not None
    assert found.name == "alice"


def test_verify_rejects_wrong_secret(tmp_path):
    conn = _fresh(tmp_path)
    api_keys.create(conn, name="alice", tier="standard")
    assert api_keys.verify(conn, "sk-wrong") is None


def test_verify_rejects_revoked(tmp_path):
    conn = _fresh(tmp_path)
    secret, key = api_keys.create(conn, name="alice", tier="standard")
    api_keys.revoke(conn, key.id)
    assert api_keys.verify(conn, secret) is None


def test_list_all_excludes_secret(tmp_path):
    conn = _fresh(tmp_path)
    api_keys.create(conn, name="a", tier="standard")
    api_keys.create(conn, name="b", tier="standard")
    rows = api_keys.list_all(conn)
    assert len(rows) == 2
    assert all(not hasattr(r, "secret") for r in rows)


def test_per_key_overrides(tmp_path):
    conn = _fresh(tmp_path)
    _, key = api_keys.create(
        conn, name="alice", tier="standard", rpm_override=120, tpm_override=50_000,
    )
    fetched = api_keys.get_by_id(conn, key.id)
    assert fetched.rpm_override == 120
    assert fetched.tpm_override == 50_000
    assert fetched.rpd_override is None


def test_count_active(tmp_path):
    conn = _fresh(tmp_path)
    assert api_keys.count_active(conn) == 0
    _, k1 = api_keys.create(conn, name="a", tier="standard")
    api_keys.create(conn, name="b", tier="standard")
    assert api_keys.count_active(conn) == 2
    api_keys.revoke(conn, k1.id)
    assert api_keys.count_active(conn) == 1


def test_allowed_models_roundtrip(tmp_path):
    """Round-trip the optional per-key model allowlist.

    Covers all three states:
    - None (default) means unrestricted - persisted as NULL, decoded as None.
    - [] means deny-all - persisted as the literal JSON "[]", decoded as [].
    - [<names>] persisted as JSON, decoded back as a list of strings.

    set_allowed_models toggles between states. Empty string in the DB
    (legacy/edge) decodes to None like NULL.
    """
    conn = _fresh(tmp_path)

    # Default: unrestricted.
    _, key = api_keys.create(conn, name="default", tier="standard")
    assert key.allowed_models is None
    assert api_keys.get_by_id(conn, key.id).allowed_models is None

    # Set an allowlist.
    api_keys.set_allowed_models(conn, key.id, ["llama-1b", "qwen-3"])
    fetched = api_keys.get_by_id(conn, key.id)
    assert fetched.allowed_models == ["llama-1b", "qwen-3"]

    # Empty list = deny-all (must survive the round-trip, NOT collapse to None).
    api_keys.set_allowed_models(conn, key.id, [])
    fetched = api_keys.get_by_id(conn, key.id)
    assert fetched.allowed_models == []

    # Back to None = unrestricted.
    api_keys.set_allowed_models(conn, key.id, None)
    fetched = api_keys.get_by_id(conn, key.id)
    assert fetched.allowed_models is None

    # create() also accepts the param up front.
    _, k2 = api_keys.create(
        conn, name="restricted", tier="standard",
        allowed_models=["only-this"],
    )
    assert k2.allowed_models == ["only-this"]

    # verify() returns the same shape.
    secret, _k3 = api_keys.create(
        conn, name="for-verify", tier="standard", allowed_models=["a", "b"],
    )
    verified = api_keys.verify(conn, secret)
    assert verified is not None
    assert verified.allowed_models == ["a", "b"]


def test_malformed_allowed_models_fails_closed(tmp_path):
    """Corrupt allowlist metadata must not turn a scoped key unrestricted."""
    conn = _fresh(tmp_path)
    secret, key = api_keys.create(
        conn, name="scoped", tier="standard", allowed_models=["only-this"],
    )

    conn.execute(
        "UPDATE api_keys SET allowed_models=? WHERE id=?",
        ("not-json", key.id),
    )
    assert api_keys.get_by_id(conn, key.id).allowed_models == []
    verified = api_keys.verify(conn, secret)
    assert verified is not None
    assert verified.allowed_models == []

    conn.execute(
        "UPDATE api_keys SET allowed_models=? WHERE id=?",
        ('{"not": "a list"}', key.id),
    )
    assert api_keys.get_by_id(conn, key.id).allowed_models == []


def test_concurrent_verify_and_count_does_not_corrupt_cursor(tmp_path):
    """Regression: dashboard 500s on stop/restart were caused by concurrent
    sqlite3 access from FastAPI's worker-thread pool. With the LockedConnection
    wrapper, hammering verify+count_active from many threads must never raise
    InterfaceError or return None for COUNT(*)."""
    import threading

    conn = _fresh(tmp_path)
    secret, _ = api_keys.create(conn, name="hot", tier="standard")
    errors: list[BaseException] = []

    def hammer():
        try:
            for _ in range(200):
                k = api_keys.verify(conn, secret)
                assert k is not None and k.name == "hot"
                n = api_keys.count_active(conn)
                assert n == 1, f"got {n!r}"
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
