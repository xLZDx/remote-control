"""
Connect dialog with Direct and Via-Hub tabs, plus saved connections and
"your address" banner.

PIN is intentionally never saved.
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from app.shared import config
from app.shared.config import SavedConnection
from app.shared.network_info import local_ipv4_addresses, public_ipv4_async


@dataclass
class ConnectInputs:
    # Always set
    pin: str
    save_name: str = ""
    # Direct fields
    address: str = ""
    port: int = config.DEFAULT_PORT
    # Via-Hub fields (kind == "via_hub" if set)
    kind: str = "direct"
    hub_address: str = ""
    hub_port: int = config.DEFAULT_HUB_PORT
    laptop_name: str = ""


class ConnectDialog(QDialog):
    NEW_LABEL = "(new connection)"
    _public_ip_resolved = pyqtSignal(str)

    def __init__(self, client_cfg: config.ClientConfig) -> None:
        super().__init__()
        self.setWindowTitle("Connect to a PC")
        self.setMinimumWidth(560)
        self._cfg = client_cfg

        outer = QVBoxLayout(self)
        outer.addWidget(_h3("Connect to a PC"))

        # Your-IP banner
        my_box = _section("Your PC's address (give this to whoever connects TO you)")
        self._my_local_label = QLabel("Detecting...")
        self._my_local_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._my_local_label.setWordWrap(True)
        my_box.addWidget(self._my_local_label)
        my_pub_row = QHBoxLayout()
        self._my_public_label = QLabel("Internet IP: detecting...")
        self._my_public_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        help_btn = QPushButton("?")
        help_btn.setFixedWidth(28)
        help_btn.clicked.connect(self._show_internet_help)
        my_pub_row.addWidget(self._my_public_label, stretch=1)
        my_pub_row.addWidget(help_btn)
        my_box.addLayout(my_pub_row)
        outer.addWidget(_wrap(my_box))

        # Saved connections row
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

        # Tabs
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        # ---- Direct tab ----
        direct = QWidget()
        df = QFormLayout(direct)
        self.address_edit = QLineEdit(client_cfg.last_address or "")
        self.address_edit.setPlaceholderText("e.g. 203.0.113.5 or hostname")
        df.addRow("Address:", self.address_edit)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(client_cfg.last_port or config.DEFAULT_PORT)
        df.addRow("Port:", self.port_spin)
        self.tabs.addTab(direct, "Direct")

        # ---- Via Hub tab ----
        viahub = QWidget()
        vf = QFormLayout(viahub)
        self.hub_address_edit = QLineEdit(client_cfg.last_hub_address or "")
        self.hub_address_edit.setPlaceholderText("public IP or hostname of your dedicated PC")
        vf.addRow("Hub address:", self.hub_address_edit)
        self.hub_port_spin = QSpinBox()
        self.hub_port_spin.setRange(1, 65535)
        self.hub_port_spin.setValue(client_cfg.last_hub_port or config.DEFAULT_HUB_PORT)
        vf.addRow("Hub port:", self.hub_port_spin)
        self.laptop_name_edit = QLineEdit(client_cfg.last_laptop_name or "")
        self.laptop_name_edit.setPlaceholderText("e.g. work-laptop, kitchen-pc")
        vf.addRow("Laptop name:", self.laptop_name_edit)
        info = QLabel(
            "<i>Connects through your dedicated-IP PC (Hub) to the named laptop. "
            "The laptop must be registered with the Hub and currently online.</i>"
        )
        info.setWordWrap(True)
        vf.addRow(info)
        self.tabs.addTab(viahub, "Via Hub")

        # PIN row (shared)
        pin_row = QFormLayout()
        self.pin_edit = QLineEdit()
        self.pin_edit.setPlaceholderText("6-digit PIN from the host")
        big = QFont()
        big.setPointSize(14)
        self.pin_edit.setFont(big)
        self.pin_edit.setMaxLength(12)
        pin_row.addRow("PIN:", self.pin_edit)
        outer.addLayout(pin_row)

        # Save section
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
            "<i>Note: the PIN is never saved - the host generates a fresh PIN "
            "each session.</i>"
        )
        note.setWordWrap(True)
        outer.addWidget(note)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._refresh_local_ips()
        self._public_ip_resolved.connect(self._apply_public_ip)
        public_ipv4_async(lambda ip: self._public_ip_resolved.emit(ip or ""))

        if not self.address_edit.text() and not self.hub_address_edit.text():
            self.address_edit.setFocus()
        else:
            self.pin_edit.setFocus()

    # ---------- public ----------

    def values(self) -> ConnectInputs:
        save_name = ""
        if self.save_chk.isChecked():
            save_name = self.save_name_edit.text().strip()
        if self.tabs.currentIndex() == 1:
            # Via Hub tab
            return ConnectInputs(
                pin=self.pin_edit.text().strip(),
                save_name=save_name,
                kind="via_hub",
                hub_address=self.hub_address_edit.text().strip(),
                hub_port=int(self.hub_port_spin.value()),
                laptop_name=self.laptop_name_edit.text().strip(),
            )
        return ConnectInputs(
            pin=self.pin_edit.text().strip(),
            save_name=save_name,
            kind="direct",
            address=self.address_edit.text().strip(),
            port=int(self.port_spin.value()),
        )

    # ---------- handlers ----------

    def _on_saved_changed(self, idx: int) -> None:
        if idx <= 0:
            self.delete_saved_btn.setEnabled(False)
            return
        sc = self.saved_combo.itemData(idx)
        if not isinstance(sc, SavedConnection):
            return
        if sc.kind == "via_hub":
            self.tabs.setCurrentIndex(1)
            self.hub_address_edit.setText(sc.hub_address)
            self.hub_port_spin.setValue(sc.hub_port)
            self.laptop_name_edit.setText(sc.laptop_name)
        else:
            self.tabs.setCurrentIndex(0)
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
                full_cfg = config.load()
                full_cfg.client.saved_connections = self._cfg.saved_connections
                config.save(full_cfg)
            except Exception:
                pass
            self.saved_combo.removeItem(idx)
            self.saved_combo.setCurrentIndex(0)

    def _on_save_toggled(self, on: bool) -> None:
        self.save_name_edit.setEnabled(on)
        if on and not self.save_name_edit.text().strip():
            if self.tabs.currentIndex() == 1 and self.laptop_name_edit.text().strip():
                self.save_name_edit.setText(self.laptop_name_edit.text().strip())
            elif self.address_edit.text().strip():
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
            primary = ips[0]
            others = ips[1:]
            text = f"<b>Primary:</b> {primary}:{port}"
            if others:
                text += "    <i>Other:</i> " + ",  ".join(f"{ip}:{port}" for ip in others)
        else:
            text = "Local network: (could not detect interfaces)"
        self._my_local_label.setText(text)

    @pyqtSlot(str)
    def _apply_public_ip(self, ip: str) -> None:
        port = config.DEFAULT_PORT
        if ip:
            self._my_public_label.setText(
                f"Internet IP: <b>{ip}:{port}</b>  (requires router port-forward for direct connects)"
            )
        else:
            self._my_public_label.setText("Internet IP: not detected (offline?)")

    def _show_internet_help(self) -> None:
        QMessageBox.information(
            self,
            "Connecting from a different network",
            (
                "<p>You have two options:</p>"
                "<ul>"
                "<li><b>Direct</b> - share your Internet IP, set up a router "
                "port-forward of TCP 7777 to your PC. The other PC connects to "
                "<i>your IP:7777</i>.</li>"
                "<li><b>Via Hub</b> - run a Hub on a PC with a stable public IP. "
                "Other PCs (laptops) register with that Hub. Anyone can then "
                "connect to a registered laptop without that laptop needing port "
                "forwarding. Set this up via Hub admin on the dedicated PC.</li>"
                "</ul>"
                "<p>For PCs on the same wifi/LAN, the Direct local IP works "
                "without any setup.</p>"
            ),
        )

    def _format_saved(self, sc: SavedConnection) -> str:
        if sc.kind == "via_hub":
            return f"{sc.name}  -  via {sc.hub_address}:{sc.hub_port} -> {sc.laptop_name}"
        return f"{sc.name}  -  {sc.address}:{sc.port}"


# ---------------- fingerprint confirm dialog ----------------

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
    return QLabel(f"<h3>{text}</h3>")


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
