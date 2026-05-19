"""API-key hashing with the HMAC pepper.

Tests run a fresh module-level pepper config per test (using monkeypatch
to reset module state) so they don't leak peppers into the rest of the
suite — the wider suite expects legacy SHA-256 mode.
"""
from __future__ import annotations

import hashlib
import hmac

import pytest

from serve_engine.store import api_keys


@pytest.fixture
def reset_pepper(monkeypatch):
    """Save and restore the module's pepper state around each test."""
    saved_path = api_keys._PEPPER_PATH
    saved_cache = api_keys._PEPPER_CACHED
    yield
    monkeypatch.setattr(api_keys, "_PEPPER_PATH", saved_path)
    monkeypatch.setattr(api_keys, "_PEPPER_CACHED", saved_cache)


def test_hash_uses_sha256_when_no_pepper_configured(reset_pepper, monkeypatch):
    monkeypatch.setattr(api_keys, "_PEPPER_PATH", None)
    monkeypatch.setattr(api_keys, "_PEPPER_CACHED", None)
    out = api_keys._hash("sk-test-abc")
    assert out == hashlib.sha256(b"sk-test-abc").hexdigest()


def test_hash_uses_hmac_when_pepper_configured(reset_pepper, tmp_path):
    pepper_path = tmp_path / "pepper"
    api_keys.configure_pepper(pepper_path)
    out = api_keys._hash("sk-test-abc")
    # File must exist and be readable.
    assert pepper_path.exists()
    pepper_bytes = pepper_path.read_bytes()
    expected = hmac.new(pepper_bytes, b"sk-test-abc", hashlib.sha256).hexdigest()
    assert out == expected
    assert out != hashlib.sha256(b"sk-test-abc").hexdigest()


def test_pepper_file_has_mode_0600(reset_pepper, tmp_path):
    pepper_path = tmp_path / "pepper"
    api_keys.configure_pepper(pepper_path)
    _ = api_keys._hash("anything")  # triggers pepper file creation
    mode = pepper_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_hash_differs_under_different_peppers(reset_pepper, tmp_path):
    """The same secret hashes to different digests under two peppers."""
    pa = tmp_path / "pa"
    pa.write_bytes(b"A" * 32)
    pb = tmp_path / "pb"
    pb.write_bytes(b"B" * 32)

    api_keys.configure_pepper(pa)
    h1 = api_keys._hash("sk-same")
    api_keys.configure_pepper(pb)
    h2 = api_keys._hash("sk-same")
    assert h1 != h2


def test_pepper_is_idempotent_across_reloads(reset_pepper, tmp_path):
    """If the pepper file already exists, configure_pepper reuses it."""
    pepper_path = tmp_path / "pepper"
    api_keys.configure_pepper(pepper_path)
    h1 = api_keys._hash("sk-x")
    # Simulate process restart by re-configuring.
    api_keys.configure_pepper(pepper_path)
    h2 = api_keys._hash("sk-x")
    assert h1 == h2
