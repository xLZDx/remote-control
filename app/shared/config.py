"""Shared configuration constants and runtime config loader."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

APP_NAME = "RemoteControl"
DEFAULT_PORT = 7777
PROTOCOL_VERSION = 1

# Path to per-user config (host + client both read/write here)
def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p

CONFIG_PATH = appdata_dir() / "config.json"
HOST_CERT_PATH = appdata_dir() / "host_cert.pem"
HOST_KEY_PATH = appdata_dir() / "host_key.pem"
PINNED_CERTS_PATH = appdata_dir() / "pinned_certs.json"

# Streaming defaults
DEFAULT_BITRATE_KBPS = 4000
DEFAULT_FPS = 30
DEFAULT_KEYFRAME_INTERVAL = 60   # frames between keyframes
DEFAULT_PIN_LENGTH = 6
PIN_REGENERATE_ON_DISCONNECT = True

# Protocol limits
MAX_FRAME_BYTES = 16 * 1024 * 1024   # 16 MB hard cap on a single message
HANDSHAKE_TIMEOUT_S = 10.0
IDLE_PING_INTERVAL_S = 5.0
IDLE_TIMEOUT_S = 30.0


@dataclass
class HostConfig:
    port: int = DEFAULT_PORT
    bitrate_kbps: int = DEFAULT_BITRATE_KBPS
    fps: int = DEFAULT_FPS
    static_pin: str | None = None        # if None, regenerate per session
    autostart: bool = False
    bind_address: str = "0.0.0.0"


@dataclass
class SavedConnection:
    name: str
    address: str
    port: int = DEFAULT_PORT


@dataclass
class ClientConfig:
    last_address: str = ""
    last_port: int = DEFAULT_PORT
    pinned_fingerprints: dict[str, str] = field(default_factory=dict)
    saved_connections: list[dict] = field(default_factory=list)

    def get_saved(self) -> list[SavedConnection]:
        out: list[SavedConnection] = []
        for entry in self.saved_connections:
            if not isinstance(entry, dict):
                continue
            try:
                out.append(SavedConnection(
                    name=str(entry.get("name", "")),
                    address=str(entry.get("address", "")),
                    port=int(entry.get("port", DEFAULT_PORT)),
                ))
            except (TypeError, ValueError):
                continue
        return out

    def upsert_saved(self, conn: SavedConnection) -> None:
        """Add or update a saved connection by name (case-insensitive)."""
        if not conn.name or not conn.address:
            return
        key = conn.name.strip().lower()
        new_list: list[dict] = []
        replaced = False
        for entry in self.saved_connections:
            if str(entry.get("name", "")).strip().lower() == key:
                new_list.append({"name": conn.name, "address": conn.address, "port": int(conn.port)})
                replaced = True
            else:
                new_list.append(entry)
        if not replaced:
            new_list.append({"name": conn.name, "address": conn.address, "port": int(conn.port)})
        self.saved_connections = new_list

    def remove_saved(self, name: str) -> bool:
        if not name:
            return False
        key = name.strip().lower()
        before = len(self.saved_connections)
        self.saved_connections = [
            e for e in self.saved_connections
            if str(e.get("name", "")).strip().lower() != key
        ]
        return len(self.saved_connections) != before


@dataclass
class AppConfig:
    host: HostConfig = field(default_factory=HostConfig)
    client: ClientConfig = field(default_factory=ClientConfig)


def load() -> AppConfig:
    if not CONFIG_PATH.exists():
        return AppConfig()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return AppConfig(
            host=HostConfig(**(data.get("host") or {})),
            client=ClientConfig(**(data.get("client") or {})),
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return AppConfig()


def save(cfg: AppConfig) -> None:
    payload: dict[str, Any] = {
        "host": asdict(cfg.host),
        "client": asdict(cfg.client),
    }
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
