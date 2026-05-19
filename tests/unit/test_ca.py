from __future__ import annotations

from cryptography import x509

from berth.cluster.ca import (
    fingerprint_sha256,
    generate_ca,
    issue_agent_cert,
    load_ca,
)


def test_generate_and_load_ca(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="berth-ca")
    assert (ca_dir / "ca.crt").exists()
    assert (ca_dir / "ca.key").exists()
    ca = load_ca(ca_dir)
    cert = x509.load_pem_x509_certificate(ca.cert_pem)
    assert "berth-ca" in cert.subject.rfc4514_string()
    # Self-signed: issuer == subject
    assert cert.issuer == cert.subject


def test_issue_agent_cert_signed_by_ca(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="berth-ca")
    ca = load_ca(ca_dir)
    bundle = issue_agent_cert(ca, label="agent-a")
    leaf = x509.load_pem_x509_certificate(bundle.cert_pem)
    assert "agent-a" in leaf.subject.rfc4514_string()
    ca_cert = x509.load_pem_x509_certificate(ca.cert_pem)
    # Verify issuer matches CA subject (chain anchor)
    assert leaf.issuer == ca_cert.subject
    # Verify the signature with the CA public key
    ca_cert.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        # RSA PKCS1v15 padding is what the builder uses by default with SHA256:
        __import__("cryptography.hazmat.primitives.asymmetric.padding",
                   fromlist=["PKCS1v15"]).PKCS1v15(),
        leaf.signature_hash_algorithm,
    )


def test_fingerprint_is_stable_sha256(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="x")
    ca = load_ca(ca_dir)
    b = issue_agent_cert(ca, label="agent-a")
    fp1 = fingerprint_sha256(b.cert_pem)
    fp2 = fingerprint_sha256(b.cert_pem)
    assert fp1 == fp2
    assert fp1.startswith("sha256:")
    assert len(fp1) == len("sha256:") + 64  # hex SHA-256


def test_different_agents_get_different_fingerprints(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="x")
    ca = load_ca(ca_dir)
    a = issue_agent_cert(ca, label="agent-a")
    b = issue_agent_cert(ca, label="agent-b")
    assert fingerprint_sha256(a.cert_pem) != fingerprint_sha256(b.cert_pem)


def test_ca_key_is_mode_600(tmp_path):
    ca_dir = tmp_path / "ca"
    generate_ca(ca_dir, common_name="x")
    mode = (ca_dir / "ca.key").stat().st_mode & 0o777
    assert mode == 0o600
