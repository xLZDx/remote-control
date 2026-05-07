"""
Cryptography helpers:
- PIN generation / verification
- PBKDF2-derived auth keys + HMAC challenge-response (PIN never crosses the wire)
- Self-signed TLS cert generation for the host
- Cert fingerprint helpers for client-side pinning
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from . import config

_PBKDF2_ITERATIONS = 200_000
_AUTH_KEY_BYTES = 32
_NONCE_BYTES = 16
_SALT_BYTES = 16


# ---------- PIN ----------

def generate_pin(length: int = config.DEFAULT_PIN_LENGTH) -> str:
    """Numeric PIN of `length` digits, cryptographically random, leading zeros allowed."""
    if length < 4 or length > 12:
        raise ValueError("pin length must be 4..12")
    upper = 10 ** length
    return f"{secrets.randbelow(upper):0{length}d}"


def derive_auth_key(pin: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 derived key from PIN + salt."""
    if not pin:
        raise ValueError("pin must be non-empty")
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=_AUTH_KEY_BYTES
    )


def make_challenge() -> tuple[bytes, bytes]:
    """Returns (salt, nonce) for a fresh challenge."""
    return os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)


def compute_proof(pin: str, salt: bytes, nonce: bytes) -> bytes:
    """HMAC(auth_key, nonce). Constant-time-comparable proof of PIN knowledge."""
    key = derive_auth_key(pin, salt)
    return hmac.new(key, nonce, hashlib.sha256).digest()


def verify_proof(pin: str, salt: bytes, nonce: bytes, proof: bytes) -> bool:
    expected = compute_proof(pin, salt, nonce)
    return hmac.compare_digest(expected, proof)


# ---------- Self-signed cert ----------

def ensure_host_cert(
    cert_path: Path = config.HOST_CERT_PATH,
    key_path: Path = config.HOST_KEY_PATH,
) -> tuple[Path, Path]:
    """Generate a self-signed RSA-2048 cert for the host if one isn't present."""
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname() or "remotecontrol-host"
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, config.APP_NAME),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass  # best-effort on Windows
    return cert_path, key_path


def cert_fingerprint(cert_pem: bytes) -> str:
    """SHA-256 fingerprint of a DER-encoded cert, formatted `aa:bb:cc:...`."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).digest()
    return ":".join(f"{b:02x}" for b in digest)


def fingerprint_short(fp: str) -> str:
    """First 8 byte-pairs (16 hex chars) - readable for the user to confirm."""
    return ":".join(fp.split(":")[:8])
