"""
Client entry point. Phase-1: CLI smoke test that connects, authenticates,
and prints a status line. Phase-4 replaces this with a Qt UI.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from app.client.tcp_client import HostClient, AuthError, FingerprintMismatchError
from app.shared import config

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
    )


async def _confirm_new_fingerprint(host_key: str, fp: str) -> bool:
    print(f"\nFirst connect to {host_key}.")
    print(f"Cert fingerprint (sha256): {fp}")
    print("If this matches what the host shows, type YES to trust and pin: ", end="")
    try:
        return input().strip().upper() == "YES"
    except EOFError:
        return False


async def _async_connect(address: str, port: int, pin: str) -> int:
    client = HostClient(address, port)
    try:
        info = await client.connect(pin=pin, confirm_new_fingerprint=_confirm_new_fingerprint)
    except FingerprintMismatchError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except AuthError as exc:
        print(f"AUTH FAILED: {exc}", file=sys.stderr)
        return 3
    except (ConnectionError, OSError) as exc:
        print(f"CONNECTION FAILED: {exc}", file=sys.stderr)
        return 4

    print(f"Connected to {info.name} (fingerprint {info.fingerprint[:23]}...)")
    print(f"Monitors: {len(info.monitors)}")
    print("Phase-1 smoke test: connection established. Phase-4 wires the UI.")
    print("Press Ctrl+C to disconnect.")
    try:
        await client._closed.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()
    return 0


def run(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="RemoteControl client")
    parser.add_argument("--address", help="host address (ip or hostname)")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument("--pin", help="connect PIN; prompted if omitted")
    args = parser.parse_args(argv)

    address = args.address or input("Host address: ").strip()
    if not address:
        print("address required", file=sys.stderr)
        return 1
    pin = args.pin or getpass.getpass("PIN: ").strip()
    if not pin:
        print("PIN required", file=sys.stderr)
        return 1

    try:
        return asyncio.run(_async_connect(address, args.port, pin))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(run())
