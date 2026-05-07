"""
First-launch role picker. Shows a small Qt dialog asking the user to choose
"Share this PC" (host) or "Connect to a PC" (client).

Returns "host", "client", or "" if the user closes the dialog.
"""
from __future__ import annotations

import sys
from typing import Literal


def pick_role(default: str = "") -> Literal["host", "client", ""]:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (
        QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    )

    app = QApplication.instance() or QApplication(sys.argv)

    class _Picker(QDialog):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("RemoteControl")
            self.setModal(True)
            self.choice: str = default

            layout = QVBoxLayout(self)
            title = QLabel("<h2>RemoteControl</h2>")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub = QLabel("Choose what you want to do:")
            sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title)
            layout.addWidget(sub)

            row = QHBoxLayout()
            host_btn = QPushButton("Share this PC")
            host_btn.setMinimumHeight(60)
            client_btn = QPushButton("Connect to a PC")
            client_btn.setMinimumHeight(60)
            host_btn.clicked.connect(lambda: self._set_and_close("host"))
            client_btn.clicked.connect(lambda: self._set_and_close("client"))
            row.addWidget(host_btn)
            row.addWidget(client_btn)
            layout.addLayout(row)

            self.setFixedSize(420, 200)

        def _set_and_close(self, role: str) -> None:
            self.choice = role
            self.accept()

    dlg = _Picker()
    dlg.exec()
    return dlg.choice  # type: ignore[return-value]
