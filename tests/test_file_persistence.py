"""Tests for file-based persistence: config loading, usage tracking, round-trips."""
from __future__ import annotations

import json

import server


# ---------------------------------------------------------------------------
# _load_ai_config — default merging
# ---------------------------------------------------------------------------

class TestLoadAiConfig:
    def test_no_file_returns_defaults(self):
        config = server._load_ai_config()
        assert config["provider"] == "claude"
        assert config["use_google_vision"] is True
        assert config["openai"]["model"] == "gpt-4.1"
        assert config["google_vision"]["api_key"] == ""

    def test_old_file_missing_new_keys(self, tmp_data_dir):
        old = {"auto_analyze": True, "claude": {"api_key": "sk-test", "model": "claude-sonnet-4-20250514"}}
        (tmp_data_dir / "ai_config.json").write_text(json.dumps(old))
        config = server._load_ai_config()
        assert config["auto_analyze"] is True
        assert config["use_google_vision"] is True
        assert config["provider"] == "claude"
        assert "openai" in config
        assert config["openai"]["model"] == "gpt-4.1"

    def test_old_file_missing_nested_key(self, tmp_data_dir):
        partial = {
            "provider": "claude",
            "claude": {"api_key": "sk-test"},
        }
        (tmp_data_dir / "ai_config.json").write_text(json.dumps(partial))
        config = server._load_ai_config()
        assert config["claude"]["api_key"] == "sk-test"
        assert config["claude"]["model"] == "claude-sonnet-4-20250514"

    def test_complete_file_unchanged(self, tmp_data_dir):
        full = {
            "provider": "openai",
            "auto_analyze": True,
            "opus_fallback": True,
            "use_google_vision": False,
            "claude": {"api_key": "sk-c", "model": "claude-opus-4-20250514"},
            "openai": {"api_key": "sk-o", "model": "gpt-4o"},
            "google_vision": {"api_key": "AIza-gv"},
        }
        (tmp_data_dir / "ai_config.json").write_text(json.dumps(full))
        config = server._load_ai_config()
        assert config["provider"] == "openai"
        assert config["openai"]["api_key"] == "sk-o"
        assert config["use_google_vision"] is False


# ---------------------------------------------------------------------------
# _record_api_usage
# ---------------------------------------------------------------------------

class TestRecordApiUsage:
    def test_creates_file_on_first_call(self, tmp_data_dir):
        server._record_api_usage("claude-sonnet-4-20250514", 100, 50)
        data = json.loads((tmp_data_dir / "api_usage.json").read_text())
        month_data = list(data["monthly"].values())[0]
        assert month_data["input_tokens"] == 100
        assert month_data["output_tokens"] == 50
        assert month_data["calls"] == 1

    def test_increments_existing(self, tmp_data_dir):
        server._record_api_usage("gpt-4.1", 200, 100)
        server._record_api_usage("gpt-4.1", 300, 150)
        data = json.loads((tmp_data_dir / "api_usage.json").read_text())
        month_data = list(data["monthly"].values())[0]
        assert month_data["input_tokens"] == 500
        assert month_data["output_tokens"] == 250
        assert month_data["calls"] == 2

    def test_multiple_models(self, tmp_data_dir):
        server._record_api_usage("claude-sonnet-4-20250514", 100, 50)
        server._record_api_usage("gpt-4.1", 200, 80)
        data = json.loads((tmp_data_dir / "api_usage.json").read_text())
        by_model = list(data["monthly"].values())[0]["by_model"]
        assert "claude-sonnet-4-20250514" in by_model
        assert "gpt-4.1" in by_model
        assert by_model["gpt-4.1"]["input_tokens"] == 200


# ---------------------------------------------------------------------------
# Collections round-trip
# ---------------------------------------------------------------------------

class TestCollections:
    def test_no_file_returns_empty(self):
        data = server._load_collections()
        assert data == {"collections": []}

    def test_save_then_load(self, tmp_data_dir):
        data = {"collections": [{"id": "c1", "name": "Test", "content_ids": ["A", "B"]}]}
        server._save_collections(data)
        loaded = server._load_collections()
        assert loaded["collections"][0]["name"] == "Test"
        assert loaded["collections"][0]["content_ids"] == ["A", "B"]


# ---------------------------------------------------------------------------
# Artwork meta round-trip
# ---------------------------------------------------------------------------

class TestArtworkMeta:
    def test_no_file_returns_empty(self):
        data = server._load_artwork_meta()
        assert data == {"artwork": {}}

    def test_save_then_load(self, tmp_data_dir):
        data = {"artwork": {"MY_F0001": {"title": "Test Art", "ai_meta": {"title": "Test"}}}}
        server._save_artwork_meta(data)
        loaded = server._load_artwork_meta()
        assert loaded["artwork"]["MY_F0001"]["title"] == "Test Art"


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

class TestCostCalculation:
    def test_known_pricing(self, tmp_data_dir):
        usage = {
            "monthly": {
                "2026-05": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "calls": 10,
                    "by_model": {
                        "claude-sonnet-4-20250514": {
                            "input_tokens": 1_000_000,
                            "output_tokens": 1_000_000,
                            "calls": 10,
                        }
                    },
                }
            }
        }
        (tmp_data_dir / "api_usage.json").write_text(json.dumps(usage))
        data = server._load_api_usage()
        month = data["monthly"]["2026-05"]
        cost = 0.0
        for model_id, stats in month.get("by_model", {}).items():
            pricing = server.MODEL_PRICING.get(model_id)
            if pricing:
                cost += stats["input_tokens"] / 1_000_000 * pricing["input_per_mtok"]
                cost += stats["output_tokens"] / 1_000_000 * pricing["output_per_mtok"]
        assert cost == 18.0  # 1M * $3 input + 1M * $15 output


# ---------------------------------------------------------------------------
# Corrupted JSON recovery (#57)
# ---------------------------------------------------------------------------

class TestCorruptedJsonRecovery:
    def test_corrupted_collections_returns_default(self, tmp_data_dir):
        (tmp_data_dir / "collections.json").write_text("{broken json!!!")
        data = server._load_collections()
        assert data == {"collections": []}
        # Verify corrupt backup was created
        corrupt_files = list(tmp_data_dir.glob("collections.corrupt.*"))
        assert len(corrupt_files) == 1

    def test_corrupted_artwork_meta_returns_default(self, tmp_data_dir):
        (tmp_data_dir / "artwork_meta.json").write_text("not valid json")
        data = server._load_artwork_meta()
        assert data == {"artwork": {}}

    def test_corrupted_ai_config_returns_defaults(self, tmp_data_dir):
        (tmp_data_dir / "ai_config.json").write_text("{{{")
        config = server._load_ai_config()
        assert config["provider"] == "claude"
        assert config["openai"]["model"] == "gpt-4.1"

    def test_corrupted_api_usage_returns_default(self, tmp_data_dir):
        (tmp_data_dir / "api_usage.json").write_text("\\x00\\x00")
        data = server._load_api_usage()
        assert data == {"monthly": {}}

    def test_corrupted_drive_sync_returns_default(self, tmp_data_dir):
        (tmp_data_dir / "drive_sync.json").write_text("[invalid")
        data = server._load_drive_sync()
        assert data == {"api_key": None, "syncs": []}

    def test_backup_on_write(self, tmp_data_dir):
        original = {"collections": [{"id": "c1", "name": "Original"}]}
        server._save_collections(original)
        updated = {"collections": [{"id": "c1", "name": "Updated"}]}
        server._save_collections(updated)
        bak = tmp_data_dir / "collections.bak"
        assert bak.exists()
        bak_data = json.loads(bak.read_text())
        assert bak_data["collections"][0]["name"] == "Original"
