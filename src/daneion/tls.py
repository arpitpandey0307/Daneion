"""Pinned self-signed TLS (ADR-0011).

The controller creates one certificate on first start. A worker learns the certificate's SHA-256
fingerprint from the join command shown on the controller's screen, checks it on first contact,
then trusts exactly that certificate from then on. No CA, no hostname checks: the pin is the trust.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import socket
import ssl
from pathlib import Path

FINGERPRINT_CHARS = 16  # hex chars a person compares/types; the full hash is pinned on disk


def fingerprint(cert_pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert_pem)
    return hashlib.sha256(der).hexdigest()


def ensure_controller_cert(directory: Path) -> tuple[Path, Path, str]:
    """Return (cert_path, key_path, full_fingerprint), creating the pair once."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "controller.crt", directory / "controller.key"
    if not (cert_path.exists() and key_path.exists()):
        _generate(cert_path, key_path)
    return cert_path, key_path, fingerprint(cert_path.read_text(encoding="ascii"))


def _generate(cert_path: Path, key_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         f"Daneion controller {socket.gethostname()}")])
    now = dt.datetime.now(dt.UTC)
    san = x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=False,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    key_path.write_bytes(key_pem)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def fetch_server_cert(host: str, port: int, timeout: float = 5.0) -> str:
    """Read the server's certificate without trusting it (first contact, before the pin check)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if der is None:
        raise ssl.SSLError("server sent no certificate")
    return ssl.DER_cert_to_PEM_cert(der)


def pinned_context(cert_pem_path: Path) -> ssl.SSLContext:
    """Trust only the pinned certificate. Hostnames are not checked because laptops on a hotspot
    change IP addresses; the exact-certificate pin is what authenticates the controller."""
    ctx = ssl.create_default_context(cafile=str(cert_pem_path))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    # Python 3.13 turns on strict X.509 checks by default; certificates created by older
    # Daneion builds lack some extensions. The exact-certificate pin is the trust anchor here.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx
