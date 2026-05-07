"""
System tray icon for the host. Stays in the tray; right-click for menu.
We use Qt's QSystemTrayIcon (cross-platform within Qt) instead of pystray
to avoid a second GUI dependency.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor, QFont
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon


def _make_icon(text: str = "RC") -> QIcon:
    """Build a simple RC-monogram icon at runtime - avoids shipping a PNG."""
    pix = QPixmap(64, 64)
    pix.fill(QColor(20, 90, 160))
    p = QPainter(pix)
    p.setPen(QColor(255, 255, 255))
    f = QFont()
    f.setBold(True)
    f.setPointSize(28)
    p.setFont(f)
    p.drawText(pix.rect(), 0x84, text)  # Qt.AlignmentFlag.AlignCenter == 0x84
    p.end()
    return QIcon(pix)


class HostTray(QSystemTrayIcon):
    show_window_requested = pyqtSignal()
    regenerate_pin_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, get_status_text: Callable[[], str]) -> None:
        super().__init__(_make_icon())
        self.get_status_text = get_status_text

        menu = QMenu()
        show_action = QAction("Show window", menu)
        show_action.triggered.connect(self.show_window_requested)
        menu.addAction(show_action)

        regen_action = QAction("Regenerate PIN", menu)
        regen_action.triggered.connect(self.regenerate_pin_requested)
        menu.addAction(regen_action)

        menu.addSeparator()

        quit_action = QAction("Quit RemoteControl", menu)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)
        self.update_tooltip()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window_requested.emit()

    def update_tooltip(self) -> None:
        try:
            self.setToolTip(self.get_status_text())
        except Exception:
            self.setToolTip("RemoteControl")
