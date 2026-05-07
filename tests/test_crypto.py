"""Tests for app.shared.crypto — PIN, PBKDF2, HMAC challenge, cert generation."""
from __future__ import annotations

import os
import re

import pytest

from app.shared import crypto


def test_generate_pin_length_and_charset() -> None:
    for length in (4, 6, 8, 10, 12):
        pin = crypto.generate_pin(length)
        assert len(pin) == length
        assert re.fullmatch(r"\d+", pin)


def test_generate_pin_rejects_bad_length() -> None:
    with pytest.raises(ValueError):
        crypto.generate_pin(3)
    with pytest.raises(ValueError):
        crypto.generate_pin(13)


def test_generate_pin_is_random() -> None:
    pins = {crypto.generate_pin(8) for _ in range(50)}
    assert len(pins) > 30


def test_derive_auth_key_deterministic() -> None:
    salt = b"\x01" * 16
    a = crypto.derive_auth_key("123456", salt)
    b = crypto.derive_auth_key("123456", salt)
    c = crypto.derive_auth_key("654321", salt)
    assert a == b
    assert a != c
    assert len(a) == 32


def test_derive_auth_key_rejects_empty_pin() -> None:
    with pytest.raises(ValueError):
        crypto.derive_auth_key("", b"\x00" * 16)


def test_make_challenge_unique_and_sized() -> None:
    salts, nonces = set(), set()
    for _ in range(20):
        salt, nonce = crypto.make_challenge()
        assert len(salt) == 16
        assert len(nonce) == 16
        salts.add(salt)
        nonces.add(nonce)
    assert len(salts) == 20
    assert len(nonces) == 20


def test_proof_roundtrip() -> None:
    pin = "482817"
    salt, nonce = crypto.make_challenge()
    proof = crypto.compute_proof(pin, salt, nonce)
    assert crypto.verify_proof(pin, salt, nonce, proof) is True


def test_proof_rejects_wrong_pin() -> None:
    salt, nonce = crypto.make_challenge()
    proof = crypto.compute_proof("482817", salt, nonce)
    assert crypto.verify_proof("482818", salt, nonce, proof) is False


def test_proof_rejects_tampered_nonce() -> None:
    salt, nonce = crypto.make_challenge()
    proof = crypto.compute_proof("482817", salt, nonce)
    bad_nonce = bytes(b ^ 1 for b in nonce)
    assert crypto.verify_proof("482817", salt, bad_nonce, proof) is False


def test_proof_constant_time_check_returns_bool() -> None:
    salt, nonce = crypto.make_challenge()
    result = crypto.verify_proof("0000", salt, nonce, b"\x00" * 32)
    assert isinstance(result, bool)


def test_ensure_host_cert_creates_files(tmp_path) -> None:
    cert = tmp_path / "host.pem"
    key = tmp_path / "host.key"
    out_cert, out_key = crypto.ensure_host_cert(cert, key)
    assert out_cert == cert
    assert out_key == key
    assert cert.exists() and key.exists()
    pem = cert.read_bytes()
    assert pem.startswith(b"-----BEGIN CERTIFICATE-----")


def test_ensure_host_cert_idempotent(tmp_path) -> None:
    cert = tmp_path / "host.pem"
    key = tmp_path / "host.key"
    crypto.ensure_host_cert(cert, key)
    first_pem = cert.read_bytes()
    crypto.ensure_host_cert(cert, key)
    assert cert.read_bytes() == first_pem


def test_fingerprint_format(tmp_path) -> None:
    cert = tmp_path / "host.pem"
    key = tmp_path / "host.key"
    crypto.ensure_host_cert(cert, key)
    fp = crypto.cert_fingerprint(cert.read_bytes())
    parts = fp.split(":")
    assert len(parts) == 32
    for p in parts:
        assert len(p) == 2
        int(p, 16)


def test_fingerprint_short(tmp_path) -> None:
    cert = tmp_path / "host.pem"
    key = tmp_path / "host.key"
    crypto.ensure_host_cert(cert, key)
    full = crypto.cert_fingerprint(cert.read_bytes())
    short = crypto.fingerprint_short(full)
    assert short == ":".join(full.split(":")[:8])
