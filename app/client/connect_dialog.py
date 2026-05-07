"""
Connect dialog: enter host address, port, PIN.
"""
from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QSpinBox, QVBoxLayout,
)

from app.shared import config


@dataclass
class ConnectInputs:
    address: str
    port: int
    pin: str


class ConnectDialog(QDialog):
    def __init__(self, defaults: ConnectInputs | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Connect to a PC")
        self.setMinimumWidth(360)

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("<h3>Connect to a PC</h3>"))
        outer.addWidget(QLabel(
            "Enter the host address shown on the other PC's screen, "
            "the port (default 7777), and the PIN."
        ))

        form = QFormLayout()
        self.address_edit = QLineEdit(defaults.address if defaults else "")
        self.address_edit.setPlaceholderText("e.g. 203.0.113.5 or hostname")
        form.addRow("Address:", self.address_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(defaults.port if defaults else config.DEFAULT_PORT)
        form.addRow("Port:", self.port_spin)

        self.pin_edit = QLineEdit(defaults.pin if defaults else "")
        self.pin_edit.setPlaceholderText("6-digit PIN from the host")
        big = QFont()
        big.setPointSize(14)
        self.pin_edit.setFont(big)
        self.pin_edit.setMaxLength(12)
        form.addRow("PIN:", self.pin_edit)

        outer.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self.address_edit.setFocus()

    def values(self) -> ConnectInputs:
        return ConnectInputs(
            address=self.address_edit.text().strip(),
            port=int(self.port_spin.value()),
            pin=self.pin_edit.text().strip(),
        )


class FingerprintConfirmDialog(QDialog):
    """Shown the first time a client connects to a given host."""

    def __init__(self, host_key: str, fingerprint: str) -> None:
        super().__init__()
        self.setWindowTitle("Confirm host identity")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<h3>First connect to {host_key}</h3>"))
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
