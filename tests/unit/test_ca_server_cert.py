from __future__ import annotations

import hashlib
import ssl

from cryptography import x509

from serve_engine.cluster.ca import (
    ensure_server_cert,
    fingerprint_ca_pem,
    generate_ca,
    generate_server_cert,
    load_ca,
    server_cert_san_hosts,
    write_cert_bundle,
)


def _make_ca(tmp_path):
    generate_ca(tmp_path / "ca", common_name="test-ca")
    return load_ca(tmp_path / "ca")


def test_generate_server_cert_san_dns_and_ip(tmp_path):
    ca = _make_ca(tmp_path)
    b = generate_server_cert(ca, hosts=["api.example.com", "192.168.1.10", "localhost"])
    sans = server_cert_san_hosts(b.cert_pem)
    assert "api.example.com" in sans
    assert "192.168.1.10" in sans
    assert "localhost" in sans


def test_generate_server_cert_has_server_auth_eku(tmp_path):
    ca = _make_ca(tmp_path)
    b = generate_server_cert(ca, hosts=["localhost"])
    cert = x509.load_pem_x509_certificate(b.cert_pem)
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.ExtendedKeyUsageOID.SERVER_AUTH in eku


def test_fingerprint_matches_openssl_dgst(tmp_path):
    ca = _make_ca(tmp_path)
    fp = fingerprint_ca_pem(ca.cert_pem)
    assert fp.startswith("sha256:")
    expected = "sha256:" + hashlib.sha256(ca.cert_pem).hexdigest()
    assert fp == expected


def test_ensure_server_cert_reuses_when_san_covers(tmp_path):
    ca = _make_ca(tmp_path)
    crt = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    first = ensure_server_cert(
        ca, crt_path=crt, key_path=key,
        required_hosts=["localhost", "127.0.0.1"],
    )
    second = ensure_server_cert(
        ca, crt_path=crt, key_path=key,
        required_hosts=["localhost"],  # subset of existing
    )
    assert first.cert_pem == second.cert_pem


def test_ensure_server_cert_regenerates_when_san_missing(tmp_path):
    ca = _make_ca(tmp_path)
    crt = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    first = ensure_server_cert(
        ca, crt_path=crt, key_path=key,
        required_hosts=["localhost"],
    )
    second = ensure_server_cert(
        ca, crt_path=crt, key_path=key,
        required_hosts=["10.99.0.1"],  # not in first cert's SAN
    )
    assert first.cert_pem != second.cert_pem
    assert "10.99.0.1" in server_cert_san_hosts(second.cert_pem)


def test_server_cert_loads_into_ssl_context(tmp_path):
    """End-to-end: cert+key file should load into a real SSLContext.
    Catches any encoding/PEM problems that the synthetic tests miss."""
    ca = _make_ca(tmp_path)
    b = generate_server_cert(ca, hosts=["localhost", "127.0.0.1"])
    crt = tmp_path / "s.crt"
    key = tmp_path / "s.key"
    write_cert_bundle(b, crt_path=crt, key_path=key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(crt), keyfile=str(key))
    # File mode check.
    assert oct(key.stat().st_mode & 0o777) == "0o600"
