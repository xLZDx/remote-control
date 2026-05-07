"""Tests for saved_connections in ClientConfig + JSON round-trip."""
from __future__ import annotations

from app.shared import config
from app.shared.config import ClientConfig, SavedConnection


def test_default_empty() -> None:
    cc = ClientConfig()
    assert cc.saved_connections == []
    assert cc.get_saved() == []


def test_upsert_adds_new() -> None:
    cc = ClientConfig()
    cc.upsert_saved(SavedConnection(name="main", address="203.0.113.5", port=7777))
    cc.upsert_saved(SavedConnection(name="laptop", address="laptop.local", port=8000))
    saved = cc.get_saved()
    assert len(saved) == 2
    assert saved[0].name == "main"
    assert saved[0].address == "203.0.113.5"
    assert saved[1].port == 8000


def test_upsert_replaces_by_name() -> None:
    cc = ClientConfig()
    cc.upsert_saved(SavedConnection(name="main", address="203.0.113.5", port=7777))
    cc.upsert_saved(SavedConnection(name="MAIN", address="198.51.100.1", port=9000))
    saved = cc.get_saved()
    assert len(saved) == 1
    assert saved[0].address == "198.51.100.1"
    assert saved[0].port == 9000


def test_upsert_rejects_blank() -> None:
    cc = ClientConfig()
    cc.upsert_saved(SavedConnection(name="", address="x", port=7777))
    cc.upsert_saved(SavedConnection(name="ok", address="", port=7777))
    assert cc.get_saved() == []


def test_remove_saved() -> None:
    cc = ClientConfig()
    cc.upsert_saved(SavedConnection(name="main", address="203.0.113.5"))
    cc.upsert_saved(SavedConnection(name="laptop", address="laptop.local"))
    assert cc.remove_saved("main") is True
    assert len(cc.get_saved()) == 1
    assert cc.get_saved()[0].name == "laptop"
    assert cc.remove_saved("notfound") is False


def test_remove_is_case_insensitive() -> None:
    cc = ClientConfig()
    cc.upsert_saved(SavedConnection(name="MyMain", address="203.0.113.5"))
    assert cc.remove_saved("mymain") is True
    assert cc.get_saved() == []


def test_get_saved_skips_invalid_entries() -> None:
    cc = ClientConfig(saved_connections=[
        {"name": "good", "address": "1.2.3.4", "port": 7777},
        {"name": "bad-port", "address": "1.2.3.4", "port": "not-a-number"},
        "junk",  # not a dict
        {"name": "no-address", "port": 1234},  # address missing -> still loads with empty
    ])
    saved = cc.get_saved()
    # "good", "no-address" load; "bad-port" rejected; "junk" rejected
    names = [s.name for s in saved]
    assert "good" in names
    assert "bad-port" not in names


def test_round_trip_via_save_load(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "cfg.json")
    cfg = config.AppConfig()
    cfg.client.upsert_saved(SavedConnection(name="main", address="203.0.113.5", port=7777))
    cfg.client.upsert_saved(SavedConnection(name="laptop", address="laptop.local", port=9000))
    cfg.client.last_address = "203.0.113.5"
    config.save(cfg)

    loaded = config.load()
    saved = loaded.client.get_saved()
    assert len(saved) == 2
    assert saved[0].name == "main"
    assert loaded.client.last_address == "203.0.113.5"
