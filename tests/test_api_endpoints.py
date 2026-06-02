"""Integration tests for API endpoints — async TestClient + respx for HTTP mocking."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

import server


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

class TestConfigGet:
    async def test_no_file_returns_defaults(self, client):
        resp = await client.get("/api/ai/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "claude"
        assert data["use_google_vision"] is True
        assert data["claude"]["api_key"] == ""
        assert data["openai"]["model"] == "gpt-4.1"

    async def test_masks_all_api_keys(self, client, seed_ai_config):
        seed_ai_config(
            claude={"api_key": "sk-ant-test-key-1234ABCD"},
            openai={"api_key": "sk-openai-test-key-9999"},
            google_vision={"api_key": "AIzaSyABCDEFGHIJKLMNOP"},
        )
        resp = await client.get("/api/ai/config")
        data = resp.json()
        assert data["claude"]["api_key"] == "...ABCD"
        assert data["openai"]["api_key"] == "...9999"
        assert data["google_vision"]["api_key"] == "...MNOP"

    async def test_short_key_fully_masked(self, client, seed_ai_config):
        seed_ai_config(claude={"api_key": "abc"})
        resp = await client.get("/api/ai/config")
        assert resp.json()["claude"]["api_key"] == "****"


class TestConfigPut:
    async def test_updates_fields(self, client, seed_ai_config):
        seed_ai_config()
        resp = await client.put("/api/ai/config", json={
            "provider": "openai",
            "auto_analyze": True,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = await client.get("/api/ai/config")
        assert resp.json()["provider"] == "openai"
        assert resp.json()["auto_analyze"] is True

    async def test_masked_key_not_overwritten(self, client, seed_ai_config):
        seed_ai_config(claude={"api_key": "sk-ant-real-key-XYZW"})
        await client.put("/api/ai/config", json={
            "claude": {"api_key": "...XYZW"},
        })
        config = server._load_ai_config()
        assert config["claude"]["api_key"] == "sk-ant-real-key-XYZW"

    async def test_real_key_updates(self, client, seed_ai_config):
        seed_ai_config()
        await client.put("/api/ai/config", json={
            "openai": {"api_key": "sk-new-openai-key"},
        })
        config = server._load_ai_config()
        assert config["openai"]["api_key"] == "sk-new-openai-key"


# ---------------------------------------------------------------------------
# Collections CRUD
# ---------------------------------------------------------------------------

class TestCollections:
    async def test_create_collection(self, client):
        resp = await client.post("/api/collections", json={"name": "Favorites"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Favorites"
        assert data["content_ids"] == []
        assert "id" in data
        assert "created" in data

    async def test_list_collections(self, client):
        await client.post("/api/collections", json={"name": "A"})
        await client.post("/api/collections", json={"name": "B"})
        resp = await client.get("/api/collections")
        names = [c["name"] for c in resp.json()["collections"]]
        assert "A" in names
        assert "B" in names

    async def test_add_items_deduplicates(self, client):
        col = (await client.post("/api/collections", json={"name": "Test"})).json()
        cid = col["id"]
        await client.post(f"/api/collections/{cid}/items", json={"content_ids": ["X", "Y"]})
        resp = await client.post(f"/api/collections/{cid}/items", json={"content_ids": ["Y", "Z"]})
        assert resp.json()["content_ids"] == ["X", "Y", "Z"]

    async def test_remove_items(self, client):
        col = (await client.post("/api/collections", json={"name": "Test"})).json()
        cid = col["id"]
        await client.post(f"/api/collections/{cid}/items", json={"content_ids": ["A", "B", "C"]})
        resp = await client.request("DELETE", f"/api/collections/{cid}/items", json={"content_ids": ["B"]})
        assert resp.json()["content_ids"] == ["A", "C"]

    async def test_rename_collection(self, client):
        col = (await client.post("/api/collections", json={"name": "Old"})).json()
        resp = await client.put(f"/api/collections/{col['id']}", json={"name": "New"})
        assert resp.json()["name"] == "New"

    async def test_delete_collection_and_404(self, client):
        col = (await client.post("/api/collections", json={"name": "Gone"})).json()
        resp = await client.delete(f"/api/collections/{col['id']}")
        assert resp.json()["ok"] is True
        resp = await client.delete(f"/api/collections/{col['id']}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Artwork metadata
# ---------------------------------------------------------------------------

class TestArtworkMeta:
    async def test_get_returns_stored(self, client, tmp_data_dir):
        meta = {"artwork": {"ID1": {"title": "Starry Night"}}}
        (tmp_data_dir / "artwork_meta.json").write_text(json.dumps(meta))
        resp = await client.get("/api/artwork-meta")
        assert resp.json()["artwork"]["ID1"]["title"] == "Starry Night"

    async def test_put_creates_entry(self, client):
        resp = await client.put("/api/artwork-meta/NEW1", json={"title": "Test Art", "width": 1920})
        assert resp.status_code == 200
        assert resp.json()["meta"]["title"] == "Test Art"
        assert resp.json()["meta"]["width"] == 1920

    async def test_put_empty_title_skipped(self, client, tmp_data_dir):
        meta = {"artwork": {"ID1": {"title": "Keep This"}}}
        (tmp_data_dir / "artwork_meta.json").write_text(json.dumps(meta))
        await client.put("/api/artwork-meta/ID1", json={"title": "  ", "width": 100})
        loaded = server._load_artwork_meta()
        assert loaded["artwork"]["ID1"]["title"] == "Keep This"
        assert loaded["artwork"]["ID1"]["width"] == 100


# ---------------------------------------------------------------------------
# AI analysis (with respx)
# ---------------------------------------------------------------------------

CLAUDE_RESPONSE = {
    "content": [{"type": "text", "text": json.dumps({
        "title": "The Starry Night",
        "artist": "Vincent van Gogh",
        "year": "1889",
        "medium": "Oil on canvas",
        "school": "Post-Impressionism",
        "vibes": ["dreamy", "swirling", "nocturnal", "luminous", "contemplative"],
        "description": "A post-impressionist masterpiece.",
        "confidence": "high",
    })}],
    "usage": {"input_tokens": 500, "output_tokens": 100},
}


class TestAnalyze:
    async def test_no_image_404(self, client, seed_ai_config):
        seed_ai_config(claude={"api_key": "sk-ant-test-key-1234ZgAA"})
        resp = await client.post("/api/ai/analyze/MISSING")
        assert resp.status_code == 404

    @respx.mock
    async def test_claude_analysis(self, client, seed_ai_config, tmp_data_dir):
        seed_ai_config(claude={"api_key": "sk-ant-test-key-1234ZgAA"})
        thumb = tmp_data_dir / ".cache" / "thumbnails" / "ART001.jpg"
        thumb.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=CLAUDE_RESPONSE)
        )

        resp = await client.post("/api/ai/analyze/ART001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["ai_meta"]["title"] == "The Starry Night"
        assert data["ai_meta"]["artist"] == "Vincent van Gogh"
        assert data["ai_meta"]["provider"] == "claude"
        assert data["ai_meta"]["vision_identified"] is False

    @respx.mock
    async def test_vision_plus_claude(self, client, seed_ai_config, tmp_data_dir):
        seed_ai_config(
            use_google_vision=True,
            claude={"api_key": "sk-ant-test-key-1234ZgAA"},
            google_vision={"api_key": "AIza-test-gv-key"},
        )
        thumb = tmp_data_dir / ".cache" / "thumbnails" / "ART002.jpg"
        thumb.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

        respx.post("https://vision.googleapis.com/v1/images:annotate").mock(
            return_value=httpx.Response(200, json={
                "responses": [{"webDetection": {
                    "bestGuessLabels": [{"label": "The Starry Night"}],
                    "webEntities": [{"description": "Vincent van Gogh", "score": 1.5}],
                }}],
            })
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=CLAUDE_RESPONSE)
        )

        resp = await client.post("/api/ai/analyze/ART002")
        assert resp.status_code == 200
        assert resp.json()["ai_meta"]["vision_identified"] is True


# ---------------------------------------------------------------------------
# Atmosphere (with respx)
# ---------------------------------------------------------------------------

class TestAtmosphere:
    @respx.mock
    async def test_full_pipeline(self, client, seed_ai_config, tmp_data_dir):
        seed_ai_config(claude={"api_key": "sk-ant-test-key-1234ZgAA"})
        meta = {"artwork": {"ART001": {
            "title": "Starry Night",
            "ai_meta": {
                "description": "A swirling night sky.",
                "vibes": ["dreamy", "nocturnal", "serene"],
            },
        }}}
        (tmp_data_dir / "artwork_meta.json").write_text(json.dumps(meta))

        respx.get("https://api.open-meteo.com/v1/forecast").mock(
            return_value=httpx.Response(200, json={
                "current": {
                    "temperature_2m": 18.5,
                    "relative_humidity_2m": 65,
                    "weather_code": 0,
                    "is_day": 0,
                    "wind_speed_10m": 5.2,
                },
                "current_units": {
                    "temperature_2m": "°C",
                    "wind_speed_10m": "km/h",
                },
            })
        )
        respx.get(url__startswith="https://api.weather.gov/").mock(
            return_value=httpx.Response(500)
        )

        ai_resp = json.dumps({
            "content_id": "ART001",
            "curator_note": "A perfect match for this evening.",
            "vibes_matched": ["dreamy", "nocturnal"],
        })
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json={
                "content": [{"text": ai_resp}],
                "usage": {"input_tokens": 200, "output_tokens": 50},
            })
        )

        resp = await client.post("/api/atmosphere", json={"lat": 40.7, "lng": -74.0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_id"] == "ART001"
        assert "curator_note" in data
        assert "weather" in data

    async def test_no_analyzed_artwork_400(self, client, seed_ai_config):
        seed_ai_config(claude={"api_key": "sk-ant-test-key-1234ZgAA"})
        resp = await client.post("/api/atmosphere", json={"lat": 40.7, "lng": -74.0})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Usage endpoint
# ---------------------------------------------------------------------------

class TestUsage:
    async def test_returns_cost(self, client, tmp_data_dir):
        from datetime import datetime, timezone
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = {"monthly": {month: {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "calls": 10,
            "by_model": {"claude-sonnet-4-20250514": {
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "calls": 10,
            }},
        }}}
        (tmp_data_dir / "api_usage.json").write_text(json.dumps(usage))
        resp = await client.get("/api/ai/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["estimated_cost_usd"] == 18.0
        assert data["calls"] == 10


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrors:
    async def test_missing_collection_name_400(self, client):
        resp = await client.post("/api/collections", json={"name": ""})
        assert resp.status_code == 400

    async def test_nonexistent_collection_404(self, client):
        resp = await client.delete("/api/collections/nonexistent-id")
        assert resp.status_code == 404

    async def test_missing_lat_lng_400(self, client, seed_ai_config):
        seed_ai_config(claude={"api_key": "sk-ant-test-key-1234ZgAA"})
        resp = await client.post("/api/atmosphere", json={})
        assert resp.status_code == 400

    async def test_no_api_key_400(self, client, tmp_data_dir):
        thumb = tmp_data_dir / ".cache" / "thumbnails" / "ART099.jpg"
        thumb.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
        resp = await client.post("/api/ai/analyze/ART099")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Health endpoint (#19)
# ---------------------------------------------------------------------------

class TestHealth:
    async def test_health_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")
        assert "version" in data
        assert "uptime_seconds" in data
        assert "data_files" in data

    async def test_health_reports_corrupt_file(self, client, tmp_data_dir):
        (tmp_data_dir / "collections.json").write_text("{broken!!!")
        resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["data_files"]["collections"] == "corrupt"
        assert data["status"] == "error"

    async def test_health_reports_missing_files(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert data["data_files"]["collections"] == "missing"


# ---------------------------------------------------------------------------
# Thumbnail batch fallback (#67)
# ---------------------------------------------------------------------------

class TestThumbnailBatchFallback:
    async def test_batch_success_no_fallback(self, client, mock_tv, tmp_data_dir):
        """When get_thumbnail_list succeeds, fallback should be False."""
        mock_tv.get_thumbnail_list.return_value = {
            "ART001": b"\xff\xd8\xff\xe0thumb1",
        }
        resp = await client.post("/api/thumbnails", json={"content_ids": ["ART001"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is False
        assert "ART001" in data["thumbnails"]
        assert data["missing"] == []

    async def test_batch_failure_falls_back_to_background_prefetch(self, client, mock_tv, tmp_data_dir):
        """When get_thumbnail_list fails, response returns immediately with
        fallback=True and missing IDs; a background task prefetches them."""
        mock_tv.get_thumbnail_list.side_effect = Exception("socket closed")
        mock_tv.get_thumbnail.return_value = b"\xff\xd8\xff\xe0thumb-individual"

        resp = await client.post("/api/thumbnails", json={"content_ids": ["ART001", "ART002"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is True
        # Thumbnails are NOT in the immediate response — they'll be
        # prefetched in the background and served from cache on retry.
        assert data["missing"] == ["ART001", "ART002"]

        # Give the background task time to run
        import asyncio
        await asyncio.sleep(0.2)

        # Now a retry request should find them cached on disk
        resp2 = await client.post("/api/thumbnails", json={"content_ids": ["ART001", "ART002"]})
        data2 = resp2.json()
        assert "ART001" in data2["thumbnails"]
        assert "ART002" in data2["thumbnails"]
        assert data2["missing"] == []

    async def test_partial_fallback_results(self, client, mock_tv, tmp_data_dir):
        """When batch fails, missing IDs are prefetched in background.
        IDs that the TV can't fetch remain missing even after retry."""
        mock_tv.get_thumbnail_list.side_effect = Exception("socket closed")

        def _get_thumb(cid):
            if cid == "ART001":
                return b"\xff\xd8\xff\xe0thumb1"
            raise Exception("TV busy")

        mock_tv.get_thumbnail.side_effect = _get_thumb

        resp = await client.post("/api/thumbnails", json={"content_ids": ["ART001", "ART002"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is True
        assert data["missing"] == ["ART001", "ART002"]

        # Give the background task time to run
        import asyncio
        await asyncio.sleep(0.2)

        # Retry — ART001 was cached by background task, ART002 still fails
        resp2 = await client.post("/api/thumbnails", json={"content_ids": ["ART001", "ART002"]})
        data2 = resp2.json()
        assert "ART001" in data2["thumbnails"]
        assert "ART002" not in data2["thumbnails"]
        assert data2["missing"] == ["ART002"]

    async def test_cached_ids_skip_tv(self, client, mock_tv, tmp_data_dir):
        """Cached thumbnails should be returned without hitting the TV."""
        thumb_dir = tmp_data_dir / ".cache" / "thumbnails"
        (thumb_dir / "ART001.jpg").write_bytes(b"\xff\xd8\xff\xe0cached")

        resp = await client.post("/api/thumbnails", json={"content_ids": ["ART001"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fallback"] is False
        assert "ART001" in data["thumbnails"]
        mock_tv.get_thumbnail_list.assert_not_called()
        mock_tv.get_thumbnail.assert_not_called()
