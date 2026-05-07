"""
RemoteControl entry point.

CRITICAL: this file is the very first to execute. It sets up file-based
logging and an excepthook BEFORE importing any heavy dependency (PyQt6,
dxcam, av), so import-time crashes still leave a traceback on disk.

Usage:
    RemoteControl.exe              # role picker
    RemoteControl.exe host         # share this PC
    RemoteControl.exe client       # connect to a PC
"""
from __future__ import annotations

# ---------- step 1: stdlib-only setup, NO heavy imports yet ----------
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path


def _appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    p = Path(base) / "RemoteControl"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _bundled_log_dir() -> Path | None:
    """When running as a frozen exe, prefer <exe_dir>/logs so the log
    travels with the bundle. Returns None if not frozen or not writable."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        exe_dir = Path(sys.executable).resolve().parent
    except OSError:
        return None
    candidate = exe_dir / "logs"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        # writability probe
        probe = candidate / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        return None


def _bootstrap_logging() -> Path:
    """Set up a rotating file log + excepthook. Stdlib only - safe to call first."""
    log_dir = _bundled_log_dir() or _appdata_dir()
    log_path = log_dir / "app.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname).1s] %(name)s: %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(
            str(log_path), maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    try:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    except Exception:
        pass

    crash = logging.getLogger("crash")

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        try:
            tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            crash.error("UNCAUGHT EXCEPTION:\n%s", tb)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook

    def _thread_hook(args) -> None:
        try:
            tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            crash.error("THREAD EXCEPTION in %s:\n%s", getattr(args.thread, "name", "?"), tb)
        except Exception:
            pass
    threading.excepthook = _thread_hook

    return log_path


_LOG_PATH = _bootstrap_logging()
logging.getLogger(__name__).info("=== RemoteControl bootstrap (log: %s) ===", _LOG_PATH)


# ---------- step 2: import-failure-safe role dispatch ----------

def _show_fatal_dialog(text: str) -> None:
    """Best-effort GUI error dialog when something fails before our normal UI."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "RemoteControl - fatal error", 0x10)
    except Exception:
        pass


def _show_warning_dialog(text: str, title: str = "RemoteControl") -> bool:
    """Yes/No dialog. Returns True if Yes."""
    try:
        import ctypes
        # MB_YESNO=4, MB_ICONQUESTION=0x20; IDYES=6
        result = ctypes.windll.user32.MessageBoxW(0, text, title, 4 | 0x20)
        return result == 6
    except Exception:
        return False


def _enforce_single_instance(role: str) -> bool:
    """Return True if we should proceed (we're the only instance), False if
    another RemoteControl is already running and the user said 'cancel'.

    Uses a Windows named mutex so the check is process-fast and doesn't
    require port-scanning. The mutex is held for the lifetime of the process
    (the OS releases it on exit)."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return True
    name = f"Global\\RemoteControl_{role}"
    ERROR_ALREADY_EXISTS = 183

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    last_err = kernel32.GetLastError()

    if last_err == ERROR_ALREADY_EXISTS:
        # Stash a *new* handle - the old one is owned by the original instance.
        # We hold this so a later instance also gets ALREADY_EXISTS.
        proceed = _show_warning_dialog(
            f"RemoteControl is already running ({role} mode).\n\n"
            "If you want to launch a fresh copy, find the existing\n"
            "RemoteControl.exe in Task Manager (Ctrl+Shift+Esc) and End Task on\n"
            "every one of them, then re-launch.\n\n"
            "Click 'Yes' to attempt to terminate any existing RemoteControl.exe\n"
            "processes and continue, or 'No' to cancel this launch.",
            title="RemoteControl - already running",
        )
        if not proceed:
            return False
        _kill_other_remotecontrol_processes()
        # Give the OS a moment to release the mutex/sockets
        import time
        time.sleep(2.0)
        # Re-attempt
        handle = kernel32.CreateMutexW(None, False, name)
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            _show_fatal_dialog(
                "Could not start: a RemoteControl process is still running and\n"
                "could not be terminated automatically. End it via Task Manager\n"
                "and try again."
            )
            return False
    # Keep the handle alive for the process lifetime by stuffing it on a module.
    globals()["_singleton_mutex_handle"] = handle
    return True


def _kill_other_remotecontrol_processes() -> None:
    """Kill any RemoteControl.exe other than ourselves. Best-effort, no crash."""
    try:
        import ctypes
        import os
        from ctypes import wintypes
        import subprocess
    except Exception:
        return
    my_pid = os.getpid()
    try:
        # /F = force; /FI excludes our own PID
        subprocess.run(
            ["taskkill", "/F", "/IM", "RemoteControl.exe", "/FI", f"PID ne {my_pid}"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _run_host() -> int:
    try:
        from app.host import main as host_main
    except Exception:
        logging.getLogger(__name__).exception("host module import failed")
        _show_fatal_dialog(
            "RemoteControl could not start (host module failed to load).\n\n"
            f"Full traceback in:\n  {_LOG_PATH}\n\n"
            "Send the log file to the developer for diagnosis."
        )
        return 2
    try:
        return host_main.run()
    except SystemExit:
        raise
    except Exception:
        logging.getLogger(__name__).exception("host run() crashed")
        _show_fatal_dialog(
            "RemoteControl host crashed during startup.\n\n"
            f"Full traceback in:\n  {_LOG_PATH}"
        )
        return 3


def _run_client() -> int:
    try:
        from app.client import main as client_main
    except Exception:
        logging.getLogger(__name__).exception("client module import failed")
        _show_fatal_dialog(
            "RemoteControl could not start (client module failed to load).\n\n"
            f"Full traceback in:\n  {_LOG_PATH}"
        )
        return 2
    try:
        return client_main.run()
    except SystemExit:
        raise
    except Exception:
        logging.getLogger(__name__).exception("client run() crashed")
        _show_fatal_dialog(
            "RemoteControl client crashed during startup.\n\n"
            f"Full traceback in:\n  {_LOG_PATH}"
        )
        return 3


def _run_picker() -> int:
    try:
        from app.shared.role_picker import pick_role
    except Exception:
        logging.getLogger(__name__).exception("role picker import failed")
        _show_fatal_dialog(
            "RemoteControl could not start (UI module failed to load).\n\n"
            f"Full traceback in:\n  {_LOG_PATH}"
        )
        return 2
    role = pick_role()
    if role == "host":
        return _run_host()
    if role == "client":
        return _run_client()
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="RemoteControl", add_help=True)
    parser.add_argument("role", nargs="?", choices=["host", "client"])
    args = parser.parse_args(argv)
    if args.role == "host":
        if not _enforce_single_instance("host"):
            return 0
        return _run_host()
    if args.role == "client":
        # Multiple clients are fine (different connections); skip mutex.
        return _run_client()
    # Picker mode: also single-instance to avoid duplicate role pickers
    if not _enforce_single_instance("picker"):
        return 0
    return _run_picker()


if __name__ == "__main__":
    sys.exit(main())
