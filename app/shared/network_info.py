"""
Network info helpers shared by host and client UIs.

- local_ipv4_addresses(): non-loopback IPv4 addresses currently bound on
  this machine. Filters out link-local (169.254/16) by default.
- public_ipv4(): single best-effort lookup of this machine's public IP via
  HTTPS to api.ipify.org. Returns None on any failure (offline, blocked,
  service down). Synchronous, with a hard 5-second timeout.
"""
from __future__ import annotations

import logging
import socket
import ssl
import threading
import urllib.request

logger = logging.getLogger(__name__)

# A few public IP-echo services. We try them in order so a single outage
# doesn't break detection. All return plaintext; we strip whitespace.
_PUBLIC_IP_PROVIDERS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

_PUBLIC_IP_TIMEOUT_S = 5.0


def local_ipv4_addresses(include_link_local: bool = False) -> list[str]:
    """Return non-loopback IPv4 addresses bound on this machine."""
    addrs: list[str] = []
    # Try a UDP-connect trick first - this finds the route-out IP,
    # which is usually what the user wants to share for LAN access.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip not in addrs:
                addrs.append(ip)
        finally:
            s.close()
    except OSError:
        pass

    # Then enumerate all bound interfaces via getaddrinfo.
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ip = info[4][0]
            if not ip:
                continue
            if ip.startswith("127."):
                continue
            if not include_link_local and ip.startswith("169.254."):
                continue
            if ip not in addrs:
                addrs.append(ip)
    except OSError:
        pass

    return addrs


def public_ipv4(timeout: float = _PUBLIC_IP_TIMEOUT_S) -> str | None:
    """Best-effort public-IPv4 lookup. Returns the IP or None."""
    ctx = ssl.create_default_context()
    for url in _PUBLIC_IP_PROVIDERS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "RemoteControl/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = resp.read(64).decode("utf-8", errors="ignore").strip()
            if _looks_like_ipv4(data):
                return data
        except Exception as exc:
            logger.debug("public_ipv4 via %s failed: %s", url, exc)
            continue
    return None


def public_ipv4_async(callback) -> threading.Thread:
    """
    Fire-and-forget public-IP lookup. `callback(ip_or_none)` is invoked from
    the worker thread when the lookup finishes. The caller is responsible
    for marshalling back to its UI thread.

    Returns the started thread (mainly for tests; daemon=True so it never
    blocks shutdown).
    """
    def _run() -> None:
        ip = public_ipv4()
        try:
            callback(ip)
        except Exception:
            logger.exception("public_ipv4_async callback raised")

    t = threading.Thread(target=_run, name="rc-public-ip", daemon=True)
    t.start()
    return t


def _looks_like_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False
