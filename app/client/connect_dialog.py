"""
Connect dialog: address + port + PIN entry, with:
- "Your address" banner (so user knows what to give to whoever wants to
  connect to THIS PC)
- Saved-connections dropdown
- "Save this connection as <name>" checkbox
- Better layout

PIN is intentionally never saved.
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from app.shared import config
from app.shared.config import SavedConnection
from app.shared.network_info import local_ipv4_addresses, public_ipv4_async


@dataclass
class ConnectInputs:
    address: str
    port: int
    pin: str
    save_name: str = ""        # if non-empty after the dialog, caller persists


class ConnectDialog(QDialog):
    """
    Phase-4 dialog redesigned for the saved-connections + your-IP UX.

    Caller:
        dlg = ConnectDialog(client_config)
        if dlg.exec() == ConnectDialog.DialogCode.Accepted:
            inputs = dlg.values()
            ...
    """

    NEW_LABEL = "(new connection)"

    def __init__(self, client_cfg: config.ClientConfig) -> None:
        super().__init__()
        self.setWindowTitle("Connect to a PC")
        self.setMinimumWidth(520)
        self._cfg = client_cfg

        outer = QVBoxLayout(self)
        outer.addWidget(_h3("Connect to a PC"))

        # --- "Your address" banner ---
        my_addr_box = _section("Your PC's address (give this to whoever wants to connect TO you)")
        self._my_local_label = QLabel("Detecting...")
        self._my_local_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._my_local_label.setWordWrap(True)
        my_addr_box.addWidget(self._my_local_label)

        self._my_public_row = QHBoxLayout()
        self._my_public_label = QLabel("Internet IP: detecting...")
        self._my_public_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        help_btn = QPushButton("?")
        help_btn.setFixedWidth(28)
        help_btn.setToolTip("How do I let someone connect from a different network?")
        help_btn.clicked.connect(self._show_internet_help)
        self._my_public_row.addWidget(self._my_public_label, stretch=1)
        self._my_public_row.addWidget(help_btn)
        my_addr_box.addLayout(self._my_public_row)
        outer.addWidget(_wrap(my_addr_box))

        # --- Saved connections ---
        saved_row = QHBoxLayout()
        saved_row.addWidget(QLabel("Saved connection:"))
        self.saved_combo = QComboBox()
        self.saved_combo.addItem(self.NEW_LABEL)
        for sc in client_cfg.get_saved():
            self.saved_combo.addItem(self._format_saved(sc), sc)
        self.saved_combo.currentIndexChanged.connect(self._on_saved_changed)
        saved_row.addWidget(self.saved_combo, stretch=1)
        self.delete_saved_btn = QPushButton("Delete")
        self.delete_saved_btn.clicked.connect(self._on_delete_saved)
        self.delete_saved_btn.setEnabled(False)
        saved_row.addWidget(self.delete_saved_btn)
        outer.addLayout(saved_row)

        outer.addWidget(_hline())

        # --- Connection inputs ---
        form = QFormLayout()
        self.address_edit = QLineEdit(client_cfg.last_address or "")
        self.address_edit.setPlaceholderText("e.g. 203.0.113.5 or hostname")
        form.addRow("Address:", self.address_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(client_cfg.last_port or config.DEFAULT_PORT)
        form.addRow("Port:", self.port_spin)

        self.pin_edit = QLineEdit()
        self.pin_edit.setPlaceholderText("6-digit PIN from the host")
        big = QFont()
        big.setPointSize(14)
        self.pin_edit.setFont(big)
        self.pin_edit.setMaxLength(12)
        form.addRow("PIN:", self.pin_edit)
        outer.addLayout(form)

        # --- Save section ---
        save_row = QHBoxLayout()
        self.save_chk = QCheckBox("Save this connection as:")
        self.save_chk.toggled.connect(self._on_save_toggled)
        self.save_name_edit = QLineEdit()
        self.save_name_edit.setPlaceholderText("e.g. my main PC, work laptop")
        self.save_name_edit.setEnabled(False)
        save_row.addWidget(self.save_chk)
        save_row.addWidget(self.save_name_edit, stretch=1)
        outer.addLayout(save_row)
        note = QLabel(
            "<i>Note: the PIN is intentionally not saved - the host generates a "
            "fresh PIN each session, so a saved PIN is rarely valid.</i>"
        )
        note.setWordWrap(True)
        outer.addWidget(note)

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # --- Populate "your IP" banner ---
        self._refresh_local_ips()
        public_ipv4_async(self._on_public_ip_resolved)

        # Focus most-likely next-input
        if not self.address_edit.text():
            self.address_edit.setFocus()
        else:
            self.pin_edit.setFocus()

    # ---------- public ----------

    def values(self) -> ConnectInputs:
        save_name = ""
        if self.save_chk.isChecked():
            save_name = self.save_name_edit.text().strip()
        return ConnectInputs(
            address=self.address_edit.text().strip(),
            port=int(self.port_spin.value()),
            pin=self.pin_edit.text().strip(),
            save_name=save_name,
        )

    # ---------- handlers ----------

    def _on_saved_changed(self, idx: int) -> None:
        if idx <= 0:
            self.delete_saved_btn.setEnabled(False)
            return
        sc = self.saved_combo.itemData(idx)
        if isinstance(sc, SavedConnection):
            self.address_edit.setText(sc.address)
            self.port_spin.setValue(sc.port)
            self.delete_saved_btn.setEnabled(True)
            self.pin_edit.setFocus()
            self.pin_edit.selectAll()

    def _on_delete_saved(self) -> None:
        idx = self.saved_combo.currentIndex()
        if idx <= 0:
            return
        sc = self.saved_combo.itemData(idx)
        if not isinstance(sc, SavedConnection):
            return
        if QMessageBox.question(self, "Delete connection",
                                f"Delete saved connection '{sc.name}'?") != QMessageBox.StandardButton.Yes:
            return
        if self._cfg.remove_saved(sc.name):
            try:
                # Persist immediately
                full_cfg = config.load()
                full_cfg.client.saved_connections = self._cfg.saved_connections
                config.save(full_cfg)
            except Exception:
                pass
            self.saved_combo.removeItem(idx)
            self.saved_combo.setCurrentIndex(0)

    def _on_save_toggled(self, on: bool) -> None:
        self.save_name_edit.setEnabled(on)
        if on and not self.save_name_edit.text().strip() and self.address_edit.text().strip():
            self.save_name_edit.setText(self.address_edit.text().strip())
        if on:
            self.save_name_edit.setFocus()

    def _refresh_local_ips(self) -> None:
        try:
            ips = local_ipv4_addresses(include_link_local=False)
        except Exception:
            ips = []
        port = config.DEFAULT_PORT
        if ips:
            text = "Local network: " + ",  ".join(f"{ip}:{port}" for ip in ips)
        else:
            text = "Local network: (could not detect interfaces)"
        self._my_local_label.setText(text)

    def _on_public_ip_resolved(self, ip: str | None) -> None:
        # Marshalling-from-bg-thread: use a single-shot QTimer in the Qt thread
        def _set_text() -> None:
            port = config.DEFAULT_PORT
            if ip:
                self._my_public_label.setText(
                    f"Internet IP: <b>{ip}:{port}</b>  (requires router port-forward)"
                )
            else:
                self._my_public_label.setText(
                    "Internet IP: not detected (offline?)"
                )
        QTimer.singleShot(0, _set_text)

    def _show_internet_help(self) -> None:
        QMessageBox.information(
            self,
            "Connecting from a different network",
            (
                "<p>To let someone on a <b>different network</b> (different building, "
                "different city, etc.) connect to this PC, two things are needed:</p>"
                "<ol>"
                "<li>You must give them this PC's <b>Internet IP</b> (the public/WAN IP "
                "of your router), <b>not</b> the local 192.168.x.x address.</li>"
                "<li>Your router must be configured to <b>port-forward</b> incoming "
                "TCP traffic on port 7777 to this PC's local IP. Look for "
                "'Port Forwarding' or 'NAT' in your router's admin page.</li>"
                "</ol>"
                "<p>Without port forwarding, the connection will time out. There is no "
                "app-side workaround for this in the current build.</p>"
                "<p>For PCs on the <b>same network</b>, the Local IP works directly "
                "with no router setup.</p>"
            ),
        )

    def _format_saved(self, sc: SavedConnection) -> str:
        return f"{sc.name}  -  {sc.address}:{sc.port}"


# ---------------- fingerprint confirm dialog (unchanged) ----------------

class FingerprintConfirmDialog(QDialog):
    def __init__(self, host_key: str, fingerprint: str) -> None:
        super().__init__()
        self.setWindowTitle("Confirm host identity")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.addWidget(_h3(f"First connect to {host_key}"))
        layout.addWidget(QLabel(
            "The host has presented a self-signed certificate. Compare the "
            "fingerprint below with what is shown on the host PC. If they "
            "match, click 'Trust and continue' to pin this fingerprint for "
            "future connections."
        ))
        fp_label = QLabel(fingerprint)
        fp_label.setWordWrap(True)
        fp_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        fp_label.setFont(f)
        layout.addWidget(fp_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Trust and continue")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ---------------- helpers ----------------

def _h3(text: str) -> QLabel:
    lbl = QLabel(f"<h3>{text}</h3>")
    return lbl


def _hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _section(title: str) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.addWidget(QLabel(f"<b>{title}</b>"))
    return layout


def _wrap(layout: QVBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
