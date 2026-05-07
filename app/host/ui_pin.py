"""
Host PIN window: shows the address (LAN + Internet), PIN, cert fingerprint,
and a list of currently connected viewers with per-viewer view-only toggles.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QClipboard, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from app.shared.network_info import local_ipv4_addresses, public_ipv4_async


class HostPinWindow(QWidget):
    """
    Signals:
        regenerate_pin_requested
        toggle_view_only_requested(session_id, view_only)
        kick_session_requested(session_id)
        quit_requested
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

        self._public_ip: str | None = None

        self.setWindowTitle("RemoteControl - Host")
        self.setMinimumSize(620, 520)

        outer = QVBoxLayout(self)

        # ---- Local network ----
        local_box = QGroupBox("Local network (same wifi/LAN)")
        local_grid = QGridLayout(local_box)
        local_grid.addWidget(QLabel("<b>LAN address</b>"), 0, 0)
        self.local_label = QLabel("...")
        self.local_label.setWordWrap(True)
        self.local_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        local_grid.addWidget(self.local_label, 0, 1)
        copy_local = QPushButton("Copy")
        copy_local.clicked.connect(self._copy_local)
        local_grid.addWidget(copy_local, 0, 2)
        outer.addWidget(local_box)

        # ---- Internet (WAN) ----
        wan_box = QGroupBox("Internet (different network)")
        wan_grid = QGridLayout(wan_box)
        wan_grid.addWidget(QLabel("<b>Public address</b>"), 0, 0)
        self.public_label = QLabel("Detecting...")
        self.public_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        wan_grid.addWidget(self.public_label, 0, 1)
        copy_pub = QPushButton("Copy")
        copy_pub.clicked.connect(self._copy_public)
        wan_grid.addWidget(copy_pub, 0, 2)
        help_btn = QPushButton("How to set up?")
        help_btn.clicked.connect(self._show_internet_help)
        wan_grid.addWidget(help_btn, 0, 3)
        wan_hint = QLabel(
            "<i>For PCs on a different network, the host PC's router must "
            "forward TCP/{port} to this PC.</i>"
        )
        wan_hint.setWordWrap(True)
        self._wan_hint = wan_hint
        wan_grid.addWidget(wan_hint, 1, 0, 1, 4)
        outer.addWidget(wan_box)

        # ---- PIN ----
        pin_box = QGroupBox("Connection PIN")
        pin_grid = QGridLayout(pin_box)
        pin_grid.addWidget(QLabel("<b>PIN</b>"), 0, 0)
        self.pin_label = QLabel("------")
        big = QFont()
        big.setPointSize(22)
        big.setBold(True)
        self.pin_label.setFont(big)
        self.pin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pin_grid.addWidget(self.pin_label, 0, 1)
        copy_pin = QPushButton("Copy")
        copy_pin.clicked.connect(self._copy_pin)
        pin_grid.addWidget(copy_pin, 0, 2)
        regen = QPushButton("Regenerate")
        regen.clicked.connect(self.regenerate_pin_requested)
        pin_grid.addWidget(regen, 0, 3)

        pin_grid.addWidget(QLabel("<b>Fingerprint</b>"), 1, 0)
        self.fp_label = QLabel("...")
        self.fp_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fp_label.setWordWrap(True)
        pin_grid.addWidget(self.fp_label, 1, 1, 1, 3)
        outer.addWidget(pin_box)

        # ---- Connected viewers ----
        viewers_box = QGroupBox("Connected viewers")
        vbl = QVBoxLayout(viewers_box)
        self.viewers_list = QListWidget()
        vbl.addWidget(self.viewers_list)
        outer.addWidget(viewers_box, stretch=1)

        # ---- Bottom buttons ----
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.quit_requested)
        bottom.addWidget(quit_btn)
        outer.addLayout(bottom)

        # Refresh timer (PIN, sessions, LAN IPs)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        self.refresh()

        # Public IP fetched once at startup, async
        public_ipv4_async(self._on_public_ip_resolved)

    # ---------- public ----------

    @pyqtSlot()
    def refresh(self) -> None:
        port = self.get_port()
        try:
            ips = local_ipv4_addresses(include_link_local=False)
        except Exception:
            ips = []
        self.local_label.setText(
            ",  ".join(f"{ip}:{port}" for ip in ips) if ips else "(no LAN interfaces detected)"
        )
        if self._public_ip:
            self.public_label.setText(f"<b>{self._public_ip}:{port}</b>")
        # update placeholder hint with current port
        self._wan_hint.setText(
            f"<i>For PCs on a different network, the host PC's router must "
            f"forward TCP/{port} to this PC.</i>"
        )
        self.pin_label.setText(self.get_pin())
        fp = self.get_fingerprint()
        self.fp_label.setText(fp if fp else "(generated on first launch)")

        sessions = self.get_sessions()
        self.viewers_list.clear()
        if not sessions:
            placeholder = QListWidgetItem("(no viewers connected)")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.viewers_list.addItem(placeholder)
        else:
            for s in sessions:
                item = QListWidgetItem()
                row = _SessionRow(s, self.toggle_view_only_requested, self.kick_session_requested)
                item.setSizeHint(row.sizeHint())
                self.viewers_list.addItem(item)
                self.viewers_list.setItemWidget(item, row)

    # ---------- helpers ----------

    def _on_public_ip_resolved(self, ip: str | None) -> None:
        def _set() -> None:
            self._public_ip = ip
            if ip:
                port = self.get_port()
                self.public_label.setText(f"<b>{ip}:{port}</b>")
            else:
                self.public_label.setText("Not detected (offline?)")
        QTimer.singleShot(0, _set)

    def _copy_pin(self) -> None:
        QApplication.clipboard().setText(self.get_pin(), QClipboard.Mode.Clipboard)

    def _copy_local(self) -> None:
        QApplication.clipboard().setText(self.local_label.text(), QClipboard.Mode.Clipboard)

    def _copy_public(self) -> None:
        if self._public_ip:
            text = f"{self._public_ip}:{self.get_port()}"
        else:
            text = self.public_label.text()
        QApplication.clipboard().setText(text, QClipboard.Mode.Clipboard)

    def _show_internet_help(self) -> None:
        port = self.get_port()
        QMessageBox.information(
            self,
            "Connecting from a different network",
            (
                f"<p>To let someone on a <b>different network</b> connect to this PC, "
                f"two things are needed:</p>"
                f"<ol>"
                f"<li>Give them this PC's <b>Public address</b> (shown above), "
                f"<b>not</b> the LAN 192.168.x.x address.</li>"
                f"<li>Configure your <b>router</b> to port-forward incoming TCP "
                f"traffic on port {port} to this PC's local IP. Look for "
                f"'Port Forwarding' or 'NAT' in the router admin page.</li>"
                f"</ol>"
                f"<p>Without port forwarding the connection will time out.</p>"
                f"<p>For PCs on the same wifi/LAN, the LAN address works directly with "
                f"no router setup.</p>"
            ),
        )


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
