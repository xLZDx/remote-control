"""Tests for network_info: local IP enumeration + public IP fetch (mocked)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.shared import network_info


def test_local_ipv4_addresses_returns_strings() -> None:
    addrs = network_info.local_ipv4_addresses()
    for a in addrs:
        assert isinstance(a, str)
        parts = a.split(".")
        assert len(parts) == 4
        for p in parts:
            int(p)  # raises if not numeric


def test_local_ipv4_excludes_loopback() -> None:
    addrs = network_info.local_ipv4_addresses()
    for a in addrs:
        assert not a.startswith("127.")


def test_local_ipv4_excludes_link_local_by_default() -> None:
    addrs = network_info.local_ipv4_addresses(include_link_local=False)
    for a in addrs:
        assert not a.startswith("169.254.")


def test_looks_like_ipv4_accepts_valid() -> None:
    assert network_info._looks_like_ipv4("203.0.113.5")
    assert network_info._looks_like_ipv4("0.0.0.0")
    assert network_info._looks_like_ipv4("255.255.255.255")


def test_looks_like_ipv4_rejects_invalid() -> None:
    assert not network_info._looks_like_ipv4("")
    assert not network_info._looks_like_ipv4("hello")
    assert not network_info._looks_like_ipv4("1.2.3")
    assert not network_info._looks_like_ipv4("256.1.1.1")
    assert not network_info._looks_like_ipv4("a.b.c.d")
    assert not network_info._looks_like_ipv4("1.2.3.4.5")


def _mock_response(payload: bytes):
    """Return a context-manager-like mock for urlopen."""
    cm = MagicMock()
    cm.read.return_value = payload
    cm.__enter__ = lambda self: cm
    cm.__exit__ = lambda *a: False
    return cm


def test_public_ipv4_first_provider_returns_ip() -> None:
    with patch("app.shared.network_info.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _mock_response(b"203.0.113.5\n")
        ip = network_info.public_ipv4()
    assert ip == "203.0.113.5"


def test_public_ipv4_falls_back_on_first_provider_failure() -> None:
    calls: list[str] = []

    def fake_urlopen(req, timeout, context):  # noqa: ARG001
        calls.append(req.full_url)
        if len(calls) == 1:
            raise OSError("simulated outage")
        return _mock_response(b"198.51.100.42")

    with patch("app.shared.network_info.urllib.request.urlopen", side_effect=fake_urlopen):
        ip = network_info.public_ipv4()
    assert ip == "198.51.100.42"
    assert len(calls) == 2


def test_public_ipv4_returns_none_when_all_providers_fail() -> None:
    with patch("app.shared.network_info.urllib.request.urlopen", side_effect=OSError("offline")):
        ip = network_info.public_ipv4()
    assert ip is None


def test_public_ipv4_rejects_garbage_response() -> None:
    with patch("app.shared.network_info.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _mock_response(b"not-an-ip\n")
        # All providers will return garbage -> returns None
        ip = network_info.public_ipv4()
    assert ip is None


def test_public_ipv4_async_invokes_callback() -> None:
    received: list[str | None] = []
    with patch("app.shared.network_info.public_ipv4", return_value="203.0.113.99"):
        t = network_info.public_ipv4_async(received.append)
        t.join(timeout=2.0)
    assert received == ["203.0.113.99"]
