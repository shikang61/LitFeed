"""Category config: seed file vs optional config.json."""

from __future__ import annotations

import json

import state_store


def test_seed_categories_loads_shared_defaults(monkeypatch):
    monkeypatch.setattr(state_store, "_seed_categories_cache", None)
    monkeypatch.setattr(state_store, "DEFAULT_CATEGORIES", [])
    seed = state_store._seed_categories()
    assert seed
    assert "physics.plasm-ph" in seed
    assert "cs.LG" in seed


def test_load_config_uses_seed_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "_seed_categories_cache", None)
    monkeypatch.setattr(state_store, "DEFAULT_CATEGORIES", [])
    monkeypatch.setattr(state_store, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(state_store, "_d1_get_last_update_id", lambda: 0)
    monkeypatch.setattr(state_store, "_d1_get_sent_ids", lambda: [])
    cfg = state_store.load_config()
    assert cfg["categories"] == state_store._seed_categories()


def test_load_config_prefers_config_json(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "_seed_categories_cache", None)
    monkeypatch.setattr(state_store, "DEFAULT_CATEGORIES", [])
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"categories": ["cs.AI"]}), encoding="utf-8")
    monkeypatch.setattr(state_store, "CONFIG_PATH", path)
    monkeypatch.setattr(state_store, "_d1_get_last_update_id", lambda: 0)
    monkeypatch.setattr(state_store, "_d1_get_sent_ids", lambda: [])
    cfg = state_store.load_config()
    assert cfg["categories"] == ["cs.AI"]
