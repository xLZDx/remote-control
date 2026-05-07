r"""
Logging setup used by both host and client mains.

- Console output (best-effort - no console in PyInstaller windowed mode).
- Rotating file at %APPDATA%\RemoteControl\app.log (last 5 x 1 MB).
- Global excepthook so silent crashes leave a traceback on disk.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path

from app.shared import config

_DEFAULT_FORMAT = "%(asctime)s [%(levelname).1s] %(name)s: %(message)s"
_log_initialized = False
_log_lock = threading.Lock()


def init_logging(level: int = logging.INFO) -> Path:
    """Idempotent logging setup. Returns the path of the active log file.

    If the root logger already has handlers (likely because app/main.py's
    bootstrap ran first), this is a no-op and we just return the path."""
    global _log_initialized
    with _log_lock:
        log_path = config.appdata_dir() / "app.log"
        root = logging.getLogger()
        if _log_initialized or root.handlers:
            _log_initialized = True
            return log_path

        root.setLevel(level)

        # File handler (rotating)
        try:
            fh = logging.handlers.RotatingFileHandler(
                str(log_path), maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            fh.setLevel(level)
            fh.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
            root.addHandler(fh)
        except OSError:
            pass

        # Console (only meaningful when launched from a terminal)
        try:
            ch = logging.StreamHandler(sys.stderr)
            ch.setLevel(level)
            ch.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
            root.addHandler(ch)
        except Exception:
            pass

        _install_excepthook()
        _log_initialized = True
        logging.getLogger(__name__).info("logging initialized at %s", log_path)
        return log_path


def _install_excepthook() -> None:
    """Capture unhandled exceptions to the log file with a full traceback."""
    log = logging.getLogger("crash")

    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        try:
            tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            log.error("UNCAUGHT EXCEPTION:\n%s", tb)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    # Also hook unraisable exceptions (asyncio internals, dtors, etc.)
    def _unraisable(unraisable) -> None:
        try:
            tb = "".join(traceback.format_exception(
                type(unraisable.exc_value), unraisable.exc_value,
                unraisable.exc_traceback,
            ))
            log.error("UNRAISABLE EXCEPTION (%s):\n%s",
                      getattr(unraisable, "object", "?"), tb)
        except Exception:
            pass

    sys.unraisablehook = _unraisable

    # Threading exceptions
    def _thread_hook(args) -> None:
        try:
            tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            log.error("THREAD EXCEPTION in %s:\n%s",
                      getattr(args.thread, "name", "?"), tb)
        except Exception:
            pass

    threading.excepthook = _thread_hook


def humanize_socket_error(exc: BaseException) -> str:
    """Turn an OSError/ConnectionError into something a user can act on."""
    import errno
    err = getattr(exc, "errno", None)
    name = getattr(exc, "strerror", None) or str(exc)

    if err in (errno.ECONNREFUSED, 10061):
        return ("Connection refused. The host PC is reachable, but no RemoteControl "
                "host is listening on that port. Make sure 'Share this PC' is running, "
                "and that the port matches.")
    if err in (errno.ETIMEDOUT, 10060):
        return ("Timed out trying to reach the host. Check that:\n"
                "  - the address is correct,\n"
                "  - the host PC is online,\n"
                "  - if you're on a different network, the host's router forwards "
                "TCP/<port> to the host PC,\n"
                "  - Windows Firewall on the host allows inbound on the port.")
    if err in (errno.EHOSTUNREACH, errno.ENETUNREACH, 10065, 10051):
        return ("The host is unreachable from this PC. Likely a network or routing "
                "problem.")
    if isinstance(exc, OSError) and exc.errno in (11001, 11004):
        return ("The address could not be resolved. Check spelling, or use the host's "
                "IP instead of a hostname.")
    if not name or name == "":
        cls = exc.__class__.__name__
        return f"Network error ({cls})"
    return name

