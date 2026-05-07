"""
Host PIN window: shows the address, PIN, cert fingerprint, and a list of
currently connected viewers with per-viewer view-only toggles.

The window is non-modal and stays open while the host runs. Closing it does
NOT stop the server (use the tray icon for that).
"""
from __future__ import annotations

import socket
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QClipboard, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)


class HostPinWindow(QWidget):
    """
    Signals:
        regenerate_pin_requested: user clicked "Regenerate PIN"
        toggle_view_only_requested(session_id, view_only): user toggled per-viewer perm
        kick_session_requested(session_id): user clicked "Disconnect" on a viewer
        quit_requested: user clicked "Quit" (closes server)
    """
    regenerate_pin_requested = pyqtSignal()
    toggle_view_only_requested = pyqtSignal(str, bool)
    kick_session_requested = pyqtSignal(str)
    quit_requested = pyqtSignal()

    def __init__(
        self,
        get_pin: Callable[[], str],
        get_port: Callable[[], int],
        get_fingerprint: Callable[[], str],
        get_sessions: Callable[[], list],
    ) -> None:
        super().__init__()
        self.get_pin = get_pin
        self.get_port = get_port
        self.get_fingerprint = get_fingerprint
        self.get_sessions = get_sessions

        self.setWindowTitle("RemoteControl - Host")
        self.setMinimumSize(520, 420)

        outer = QVBoxLayout(self)

        addr_box = QGroupBox("Connection details")
        grid = QGridLayout(addr_box)

        # Address
        grid.addWidget(QLabel("<b>Address</b>"), 0, 0)
        self.address_label = QLabel("...")
        self.address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy_addr = QPushButton("Copy")
        copy_addr.clicked.connect(self._copy_address)
        grid.addWidget(self.address_label, 0, 1)
        grid.addWidget(copy_addr, 0, 2)

        # PIN
        grid.addWidget(QLabel("<b>PIN</b>"), 1, 0)
        self.pin_label = QLabel("------")
        big = QFont()
        big.setPointSize(20)
        big.setBold(True)
        self.pin_label.setFont(big)
        self.pin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy_pin = QPushButton("Copy")
        copy_pin.clicked.connect(self._copy_pin)
        regen = QPushButton("Regenerate")
        regen.clicked.connect(self.regenerate_pin_requested)
        grid.addWidget(self.pin_label, 1, 1)
        grid.addWidget(copy_pin, 1, 2)
        grid.addWidget(regen, 1, 3)

        # Fingerprint
        grid.addWidget(QLabel("<b>Fingerprint</b>"), 2, 0)
        self.fp_label = QLabel("...")
        self.fp_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fp_label.setWordWrap(True)
        grid.addWidget(self.fp_label, 2, 1, 1, 3)

        outer.addWidget(addr_box)

        # Connected viewers
        viewers_box = QGroupBox("Connected viewers")
        vbl = QVBoxLayout(viewers_box)
        self.viewers_list = QListWidget()
        vbl.addWidget(self.viewers_list)
        outer.addWidget(viewers_box, stretch=1)

        # Bottom buttons
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.quit_requested)
        bottom.addWidget(quit_btn)
        outer.addLayout(bottom)

        # Refresh timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        self.refresh()

    @pyqtSlot()
    def refresh(self) -> None:
        port = self.get_port()
        addresses = _local_ipv4_addresses()
        if addresses:
            text = ", ".join(f"{a}:{port}" for a in addresses)
        else:
            text = f"<this PC>:{port}"
        self.address_label.setText(text)
        self.pin_label.setText(self.get_pin())
        fp = self.get_fingerprint()
        self.fp_label.setText(fp if fp else "(generated on first launch)")

        # Refresh viewers list
        sessions = self.get_sessions()
        self.viewers_list.clear()
        for s in sessions:
            item = QListWidgetItem()
            row = _SessionRow(s, self.toggle_view_only_requested, self.kick_session_requested)
            item.setSizeHint(row.sizeHint())
            self.viewers_list.addItem(item)
            self.viewers_list.setItemWidget(item, row)

    def _copy_pin(self) -> None:
        QApplication.clipboard().setText(self.get_pin(), QClipboard.Mode.Clipboard)

    def _copy_address(self) -> None:
        QApplication.clipboard().setText(self.address_label.text(), QClipboard.Mode.Clipboard)


class _SessionRow(QWidget):
    def __init__(self, session, toggle_signal, kick_signal) -> None:
        super().__init__()
        self.session = session
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        name = QLabel(f"{getattr(session, 'client_name', '?')}  ({getattr(session, 'peer', '?')})")
        layout.addWidget(name, stretch=1)

        view_only = QCheckBox("View-only")
        view_only.setChecked(bool(getattr(session, "view_only", False)))
        view_only.toggled.connect(
            lambda checked: toggle_signal.emit(getattr(session, "session_id", ""), checked)
        )
        layout.addWidget(view_only)

        kick = QPushButton("Disconnect")
        kick.clicked.connect(
            lambda: kick_signal.emit(getattr(session, "session_id", ""))
        )
        layout.addWidget(kick)


def _local_ipv4_addresses() -> list[str]:
    """Return non-loopback IPv4 addresses for the host (best-effort)."""
    addrs: list[str] = []
    try:
        hostname = socket.gethostname()
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in addrs:
                addrs.append(ip)
    except OSError:
        pass
    # Also try a UDP-connect trick to find the route-out IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip not in addrs:
                addrs.insert(0, ip)
        finally:
            s.close()
    except OSError:
        pass
    return addrs
