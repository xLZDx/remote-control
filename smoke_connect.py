"""Headless smoke test: TCP/TLS + cert-pin auto-accept + HELLO/AUTH handshake."""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.client.tcp_client import HostClient

HOST = "188.244.21.9"
PORT = 7777
PIN = "385649"


async def auto_confirm(host_key: str, fp: str) -> bool:
    print(f"[smoke] auto-accepting fingerprint for {host_key}: {fp}")
    return True


async def main() -> int:
    client = HostClient(HOST, PORT, client_name="smoke-test")
    try:
        info = await client.connect(pin=PIN, confirm_new_fingerprint=auto_confirm)
    except Exception as exc:
        print(f"[smoke] FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"[smoke] CONNECTED to host '{info.name}', monitors={len(info.monitors)}")
    print(f"[smoke] fingerprint={info.fingerprint}")
    await asyncio.sleep(2.0)
    await client.close()
    print("[smoke] OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
