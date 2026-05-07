"""
Hub Settings dialog: two tabs

  - "Run as Hub": enable the broker on this PC, manage registrations
    (Add -> generates a one-time-displayed token; Remove)
  - "Register with Hub": enter Hub address, laptop name, token. The host
    starts a persistent registrar that auto-reconnects.

Changes are applied immediately (broker start/stop, registrar start/stop)
through callbacks supplied by the host orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QClipboard, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from app.shared import config


@dataclass
class HubSettingsCallbacks:
    list_registrations: Callable[[], list]                # returns list of Registration
    list_active: Callable[[], list[str]]                  # names currently online
    add_registration: Callable[[str], object]             # name -> Registration with .token
    remove_registration: Callable[[str], bool]
    set_hub_enabled: Callable[[bool, int], None]
    is_hub_running: Callable[[], bool]
    get_registration: Callable[[], object | None]         # current laptop-side registration cfg
    save_registration: Callable[[str, int, str, str], None]   # hub_addr, hub_port, name, token
    clear_registration: Callable[[], None]
    registrar_status: Callable[[], tuple[str, str]]       # (status, last_error)


class HubSettingsDialog(QDialog):
    closed = pyqtSignal()

    def __init__(self, cb: HubSettingsCallbacks, host_cfg: config.HostConfig) -> None:
        super().__init__()
        self.cb = cb
        self.host_cfg = host_cfg
        self.setWindowTitle("Hub Settings")
        self.setMinimumSize(620, 480)

        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        tabs.addTab(self._build_run_hub_tab(), "Run as Hub")
        tabs.addTab(self._build_register_tab(), "Register with Hub")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(1000)
        self._refresh()

    # ---------- Tab 1: Run as Hub ----------

    def _build_run_hub_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel(
            "<b>Run as Hub</b>: this PC accepts laptop registrations and routes "
            "viewer connections through them. Use this on your dedicated public-IP PC."
        ))

        run_row = QHBoxLayout()
        self.hub_enabled_chk = QCheckBox("Run Hub broker on this PC")
        self.hub_enabled_chk.setChecked(bool(self.host_cfg.hub_enabled))
        self.hub_enabled_chk.toggled.connect(self._on_hub_enabled_toggled)
        run_row.addWidget(self.hub_enabled_chk)
        run_row.addWidget(QLabel("Port:"))
        self.hub_port_spin = QSpinBox()
        self.hub_port_spin.setRange(1, 65535)
        self.hub_port_spin.setValue(self.host_cfg.hub_port)
        self.hub_port_spin.valueChanged.connect(self._on_hub_port_changed)
        run_row.addWidget(self.hub_port_spin)
        self.hub_status_label = QLabel("(stopped)")
        run_row.addWidget(self.hub_status_label)
        run_row.addStretch(1)
        layout.addLayout(run_row)

        gb = QGroupBox("Registrations")
        gbl = QVBoxLayout(gb)
        self.regs_list = QListWidget()
        gbl.addWidget(self.regs_list)
        row = QHBoxLayout()
        add_btn = QPushButton("Add registration...")
        add_btn.clicked.connect(self._on_add_reg)
        rm_btn = QPushButton("Remove selected")
        rm_btn.clicked.connect(self._on_remove_reg)
        row.addWidget(add_btn)
        row.addWidget(rm_btn)
        row.addStretch(1)
        gbl.addLayout(row)
        layout.addWidget(gb, stretch=1)
        return w

    def _refresh_run_hub(self) -> None:
        # status text
        running = self.cb.is_hub_running()
        self.hub_status_label.setText("(running)" if running else "(stopped)")

        # registrations + active state
        active = set(self.cb.list_active())
        self.regs_list.clear()
        for reg in self.cb.list_registrations():
            label = f"{reg.name}"
            if reg.name.lower() in {a.lower() for a in active}:
                label += "  [ONLINE]"
            else:
                label += "  [offline]"
            if reg.last_seen_iso:
                label += f"   last seen: {reg.last_seen_iso}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, reg.name)
            self.regs_list.addItem(item)

    def _on_hub_enabled_toggled(self, on: bool) -> None:
        port = int(self.hub_port_spin.value())
        try:
            self.cb.set_hub_enabled(on, port)
        except Exception as exc:
            QMessageBox.warning(self, "Hub", f"Could not toggle Hub: {exc}")
            self.hub_enabled_chk.blockSignals(True)
            self.hub_enabled_chk.setChecked(not on)
            self.hub_enabled_chk.blockSignals(False)

    def _on_hub_port_changed(self, _val: int) -> None:
        # Apply only if currently running - bounce by toggling
        if self.hub_enabled_chk.isChecked():
            self.cb.set_hub_enabled(False, self.host_cfg.hub_port)
            self.cb.set_hub_enabled(True, int(self.hub_port_spin.value()))

    def _on_add_reg(self) -> None:
        name, ok = _ask_name(self, "Add registration",
                             "Name for the laptop being registered (e.g. 'work-laptop'):")
        if not ok or not name.strip():
            return
        reg = self.cb.add_registration(name.strip())
        # Show the token ONCE
        TokenRevealDialog(reg.name, reg.token, self).exec()
        self._refresh_run_hub()

    def _on_remove_reg(self) -> None:
        item = self.regs_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole) or item.text()
        if QMessageBox.question(self, "Remove registration",
                                f"Remove registration '{name}'?") != QMessageBox.StandardButton.Yes:
            return
        self.cb.remove_registration(str(name))
        self._refresh_run_hub()

    # ---------- Tab 2: Register with Hub ----------

    def _build_register_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel(
            "<b>Register this PC with a Hub</b>. Once registered, the Hub admin "
            "and any client they trust can connect TO this PC by name, even "
            "from a different network. The Hub admin gives you a one-time "
            "token for the name you chose."
        ))

        reg = self.cb.get_registration()
        form = QFormLayout()
        self.reg_hub_addr_edit = QLineEdit(getattr(reg, "hub_address", "") or "")
        self.reg_hub_addr_edit.setPlaceholderText("Hub PC's address (IP or hostname)")
        form.addRow("Hub address:", self.reg_hub_addr_edit)
        self.reg_hub_port_spin = QSpinBox()
        self.reg_hub_port_spin.setRange(1, 65535)
        self.reg_hub_port_spin.setValue(getattr(reg, "hub_port", config.DEFAULT_HUB_PORT))
        form.addRow("Hub port:", self.reg_hub_port_spin)
        self.reg_name_edit = QLineEdit(getattr(reg, "laptop_name", "") or "")
        self.reg_name_edit.setPlaceholderText("name the Hub admin chose for this PC")
        form.addRow("Laptop name:", self.reg_name_edit)
        self.reg_token_edit = QLineEdit(getattr(reg, "token", "") or "")
        self.reg_token_edit.setPlaceholderText("32-hex-char token from the Hub admin")
        form.addRow("Token:", self.reg_token_edit)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save and connect")
        save_btn.clicked.connect(self._on_save_reg)
        clear_btn = QPushButton("Clear / disconnect")
        clear_btn.clicked.connect(self._on_clear_reg)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.reg_status_label = QLabel("Status: idle")
        layout.addWidget(self.reg_status_label)
        layout.addStretch(1)
        return w

    def _refresh_register(self) -> None:
        status, last_error = self.cb.registrar_status()
        text = f"Status: {status}"
        if last_error:
            text += f"  -  {last_error}"
        self.reg_status_label.setText(text)

    def _on_save_reg(self) -> None:
        addr = self.reg_hub_addr_edit.text().strip()
        port = int(self.reg_hub_port_spin.value())
        name = self.reg_name_edit.text().strip()
        token = self.reg_token_edit.text().strip()
        if not (addr and name and token):
            QMessageBox.warning(self, "Register",
                                "Hub address, laptop name and token are all required.")
            return
        try:
            self.cb.save_registration(addr, port, name, token)
        except Exception as exc:
            QMessageBox.critical(self, "Register", f"Failed to save registration: {exc}")
            return

    def _on_clear_reg(self) -> None:
        if QMessageBox.question(self, "Clear", "Stop registrar and forget Hub details?") != QMessageBox.StandardButton.Yes:
            return
        self.cb.clear_registration()
        self.reg_hub_addr_edit.clear()
        self.reg_name_edit.clear()
        self.reg_token_edit.clear()

    # ---------- shared ----------

    def _refresh(self) -> None:
        try:
            self._refresh_run_hub()
            self._refresh_register()
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.closed.emit()
        super().closeEvent(event)


# ---------- helper widgets ----------

class TokenRevealDialog(QDialog):
    """Shows a freshly-generated token ONCE; user must copy it now."""

    def __init__(self, name: str, token: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Registration token")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"<h3>Token for '{name}'</h3>"
            "<p><b>This token is shown only once.</b> Copy it now and give it "
            "to the laptop user. You'll need to remove and re-add the "
            "registration if it's lost.</p>"
        ))
        token_edit = QTextEdit()
        token_edit.setPlainText(token)
        token_edit.setReadOnly(True)
        token_edit.setFixedHeight(80)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        token_edit.setFont(f)
        layout.addWidget(token_edit)

        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Copy to clipboard")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(token, QClipboard.Mode.Clipboard))
        btn_row.addWidget(copy_btn)
        btn_row.addStretch(1)
        ok = QPushButton("Done")
        ok.clicked.connect(self.accept)
        btn_row.addWidget(ok)
        layout.addLayout(btn_row)


def _ask_name(parent, title: str, prompt: str) -> tuple[str, bool]:
    """Tiny single-line text prompt (uses QInputDialog under the hood)."""
    from PyQt6.QtWidgets import QInputDialog
    text, ok = QInputDialog.getText(parent, title, prompt)
    return text, ok
