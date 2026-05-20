from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from berth.config import write_private_file

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
    # 0o700 on the dir is belt-and-braces — the CA private key inside
    # is mode 0o600, but if backups or other tools list the directory
    # we want the listing itself locked down.
    try:
        ca_dir.chmod(0o700)
    except OSError:
        pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.UTC)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _TEN_YEARS)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    (ca_dir / "ca.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path = ca_dir / "ca.key"
    write_private_file(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_cert.public_key()  # type: ignore[arg-type]
            ),
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


def fingerprint_ca_pem(ca_cert_pem: bytes) -> str:
    """sha256 of the CA's PEM bytes — what enrollment URIs pin.

    We hash the PEM directly (not DER) so operators can verify by hand
    with `openssl dgst -sha256 ca.pem` on the file served by
    /admin/ca.pem."""
    return "sha256:" + hashlib.sha256(ca_cert_pem).hexdigest()


def _san_entries(hosts: list[str]) -> list[x509.GeneralName]:
    entries: list[x509.GeneralName] = []
    for h in hosts:
        h = h.strip()
        if not h:
            continue
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            entries.append(x509.DNSName(h))
    return entries


def generate_server_cert(ca: CA, *, hosts: list[str]) -> CertBundle:
    """Mint a server cert signed by `ca` with SAN entries for `hosts`.

    Each host is classified as DNS or IP and added to the SAN. Adds
    SERVER_AUTH extended key usage. Validity = 5 years."""
    ca_cert = x509.load_pem_x509_certificate(ca.cert_pem)
    ca_key = serialization.load_pem_private_key(ca.key_pem, password=None)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(_dt.UTC)
    san = _san_entries(hosts)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(hosts[0] if hosts else "serve-leader"))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _ONE_YEAR * 5)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_cert.public_key()  # type: ignore[arg-type]
            ),
            critical=False,
        )
    )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(san), critical=False,
        )
    leaf = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())  # type: ignore[arg-type]
    return CertBundle(
        cert_pem=leaf.public_bytes(serialization.Encoding.PEM),
        key_pem=leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def write_cert_bundle(
    bundle: CertBundle, *, crt_path: Path, key_path: Path,
) -> None:
    """Write cert + key to disk; key gets mode 0o600."""
    crt_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    crt_path.write_bytes(bundle.cert_pem)
    write_private_file(key_path, bundle.key_pem)


def server_cert_san_hosts(cert_pem: bytes) -> set[str]:
    """Return the set of SAN entries (DNS names + IP strings) in a cert."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return set()
    out: set[str] = set()
    for entry in san:
        if isinstance(entry, x509.DNSName):
            out.add(entry.value)
        elif isinstance(entry, x509.IPAddress):
            out.add(str(entry.value))
    return out


def ensure_server_cert(
    ca: CA, *,
    crt_path: Path, key_path: Path, required_hosts: list[str],
) -> CertBundle:
    """Read or (re)generate the server cert.

    If the cert exists and its SAN covers every host in `required_hosts`,
    it is reused. Otherwise it is regenerated. Returns the loaded bundle.
    """
    required = {h for h in required_hosts if h}
    if crt_path.exists() and key_path.exists():
        existing_pem = crt_path.read_bytes()
        if required.issubset(server_cert_san_hosts(existing_pem)):
            return CertBundle(
                cert_pem=existing_pem,
                key_pem=key_path.read_bytes(),
            )
    bundle = generate_server_cert(ca, hosts=sorted(required) or ["localhost"])
    write_cert_bundle(bundle, crt_path=crt_path, key_path=key_path)
    return bundle
