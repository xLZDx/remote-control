"""
RemoteControl entry point. Picks host or client mode based on argv or a launcher
dialog if launched without args.

Usage:
    python -m app.main host           Start host (share this PC)
    python -m app.main client         Start client (connect to a PC)
    python -m app.main                Show role-picker dialog
"""
from __future__ import annotations

import argparse
import sys


def _run_host() -> int:
    from app.host import main as host_main
    return host_main.run()


def _run_client() -> int:
    from app.client import main as client_main
    return client_main.run()


def _run_picker() -> int:
    from app.shared.role_picker import pick_role
    role = pick_role()
    if role == "host":
        return _run_host()
    if role == "client":
        return _run_client()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="RemoteControl", add_help=True)
    parser.add_argument("role", nargs="?", choices=["host", "client"])
    args = parser.parse_args(argv)

    if args.role == "host":
        return _run_host()
    if args.role == "client":
        return _run_client()
    return _run_picker()


if __name__ == "__main__":
    sys.exit(main())
