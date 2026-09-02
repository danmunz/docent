from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import server


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    cache_dir = tmp_path / ".cache"
    thumb_dir = cache_dir / "thumbnails"
    originals_dir = cache_dir / "originals"
    cache_dir.mkdir()
    thumb_dir.mkdir()
    originals_dir.mkdir()

    monkeypatch.setattr(server, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(server, "THUMB_DIR", thumb_dir)
    monkeypatch.setattr(server, "ORIGINALS_DIR", originals_dir)
    monkeypatch.setattr(server, "COLLECTIONS_FILE", tmp_path / "collections.json")
    monkeypatch.setattr(server, "ARTWORK_META_FILE", tmp_path / "artwork_meta.json")
    monkeypatch.setattr(server, "AI_CONFIG_FILE", tmp_path / "ai_config.json")
    monkeypatch.setattr(server, "API_USAGE_FILE", tmp_path / "api_usage.json")
    monkeypatch.setattr(server, "DRIVE_SYNC_FILE", tmp_path / "drive_sync.json")
    monkeypatch.setattr(server, "ATMOSPHERE_HISTORY_FILE", tmp_path / "atmosphere_history.json")
    monkeypatch.setattr(server, "TOKEN_FILE", tmp_path / ".tv-token")

    monkeypatch.setattr(server, "_art_cache", None)
    monkeypatch.setattr(server, "_current_id_cache", None)
    monkeypatch.setattr(server, "_tv_conn", None)
    monkeypatch.setattr(server, "_tv_art", None)
    monkeypatch.setattr(server, "_tv_last_used", 0)
    monkeypatch.setattr(server, "_nws_station_cache", {})
    monkeypatch.setattr(server, "_thumb_prefetch_in_progress", set())
    monkeypatch.setattr(server, "_thumb_prefetch_running", False)
    monkeypatch.setattr(server, "_batch_thumb_last_failure", 0)
    monkeypatch.setattr(server, "_tv_lock", asyncio.Lock())

    return tmp_path


@pytest.fixture
def client():
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=server.app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def mock_tv(monkeypatch):
    tv = MagicMock()
    art = MagicMock()
    tv.art.return_value = art
    art.open.return_value = None
    tv.close.return_value = None
    monkeypatch.setattr(server, "get_tv", lambda: tv)
    return art


@pytest.fixture
def seed_ai_config(tmp_data_dir):
    def _seed(**overrides):
        config = {
            "provider": "claude",
            "auto_analyze": False,
            "opus_fallback": False,
            "use_google_vision": True,
            "claude": {"api_key": "sk-ant-test-key-1234ZgAA", "model": "claude-sonnet-5"},
            "openai": {"api_key": "", "model": "gpt-4.1"},
            "google_vision": {"api_key": ""},
        }
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(config.get(k), dict):
                config[k].update(v)
            else:
                config[k] = v
        (tmp_data_dir / "ai_config.json").write_text(json.dumps(config))
        return config

    return _seed
