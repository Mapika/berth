from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_ONE_YEAR = _dt.timedelta(days=365)
_TEN_YEARS = _dt.timedelta(days=365 * 10)


@dataclass(frozen=True)
class CA:
    cert_pem: bytes
    key_pem: bytes


@dataclass(frozen=True)
class CertBundle:
    cert_pem: bytes
    key_pem: bytes


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def generate_ca(ca_dir: Path, *, common_name: str) -> None:
    ca_dir = Path(ca_dir)
    ca_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _TEN_YEARS)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    (ca_dir / "ca.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = ca_dir / "ca.key"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)


def load_ca(ca_dir: Path) -> CA:
    ca_dir = Path(ca_dir)
    return CA(
        cert_pem=(ca_dir / "ca.crt").read_bytes(),
        key_pem=(ca_dir / "ca.key").read_bytes(),
    )


def issue_agent_cert(ca: CA, *, label: str) -> CertBundle:
    ca_cert = x509.load_pem_x509_certificate(ca.cert_pem)
    ca_key = serialization.load_pem_private_key(ca.key_pem, password=None)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.UTC)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(label))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _ONE_YEAR * 5)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())  # type: ignore[arg-type]
    )
    return CertBundle(
        cert_pem=leaf.public_bytes(serialization.Encoding.PEM),
        key_pem=leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def fingerprint_sha256(cert_pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return "sha256:" + hashlib.sha256(der).hexdigest()
