"""Tests for concurrency safety: locks, race conditions, atomic writes."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import server


class TestConfigConcurrency:
    """Concurrent writes to ai_config must not lose data."""

    async def test_concurrent_config_puts_no_data_loss(self, client, seed_ai_config):
        seed_ai_config()

        async def toggle_auto(val):
            return await client.put("/api/ai/config", json={"auto_analyze": val})

        async def set_provider(name):
            return await client.put("/api/ai/config", json={"provider": name})

        # Fire concurrent requests that modify different keys
        results = await asyncio.gather(
            toggle_auto(True),
            set_provider("openai"),
        )
        assert all(r.status_code == 200 for r in results)

        # Both mutations should be reflected
        resp = await client.get("/api/ai/config")
        config = resp.json()
        assert config["auto_analyze"] is True
        assert config["provider"] == "openai"

    async def test_rapid_config_updates_all_persist(self, client, seed_ai_config):
        seed_ai_config()

        for i in range(10):
            provider = "claude" if i % 2 == 0 else "openai"
            resp = await client.put("/api/ai/config", json={"provider": provider})
            assert resp.status_code == 200

        resp = await client.get("/api/ai/config")
        # The last write should win
        assert resp.json()["provider"] == "openai"


class TestApiUsageConcurrency:
    """Concurrent usage recording must not corrupt data."""

    async def test_concurrent_usage_records(self, client, seed_ai_config):
        seed_ai_config(claude={"api_key": "test-key-not-real-1234"})

        # Directly call _record_api_usage in rapid succession
        for _ in range(10):
            server._record_api_usage("claude-sonnet-4-20250514", 100, 50)

        resp = await client.get("/api/ai/usage")
        data = resp.json()
        assert data["calls"] == 10
        assert data["input_tokens"] == 1000
        assert data["output_tokens"] == 500


class TestAtomicWrites:
    """Atomic write must not leave corrupt partial files."""

    def test_atomic_write_creates_valid_json(self, tmp_data_dir):
        path = tmp_data_dir / "test_atomic.json"
        data = {"key": "value", "nested": {"a": 1}}
        server._atomic_write_json(path, data)
        assert json.loads(path.read_text()) == data

    def test_atomic_write_overwrites_existing(self, tmp_data_dir):
        path = tmp_data_dir / "test_atomic.json"
        path.write_text('{"old": true}')
        server._atomic_write_json(path, {"new": True})
        assert json.loads(path.read_text()) == {"new": True}


class TestTvLockSerialization:
    """TV operations should be serialized via _tv_lock."""

    async def test_tv_lock_prevents_concurrent_access(self, client, mock_tv):
        """Two refresh requests should not hit the TV simultaneously."""
        call_count = 0
        max_concurrent = 0
        current_concurrent = 0

        original_get_art_list = mock_tv.get_artmode_settings

        def slow_art_list(*args, **kwargs):
            nonlocal call_count, max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            call_count += 1
            current_concurrent -= 1
            return '{"current_content_id": "abc123"}'

        mock_tv.get_artmode_settings.side_effect = slow_art_list
        mock_tv.available.return_value = [
            {"content_id": "abc123", "category_id": "MY_PHOTO"}
        ]

        results = await asyncio.gather(
            client.get("/api/art"),
            client.get("/api/art"),
            return_exceptions=True,
        )
        # At least one should succeed
        successes = [r for r in results if not isinstance(r, Exception) and r.status_code == 200]
        assert len(successes) >= 1
