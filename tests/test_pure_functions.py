"""Unit tests for pure/near-pure functions — no HTTP mocks needed."""
from __future__ import annotations

import json

import server


# ---------------------------------------------------------------------------
# _parse_ai_response
# ---------------------------------------------------------------------------

class TestParseAiResponse:
    def test_raw_json(self):
        text = '{"title": "Starry Night", "artist": "Van Gogh"}'
        result = server._parse_ai_response(text)
        assert result == {"title": "Starry Night", "artist": "Van Gogh"}

    def test_fenced_json(self):
        text = '```json\n{"title": "Starry Night"}\n```'
        result = server._parse_ai_response(text)
        assert result["title"] == "Starry Night"

    def test_fenced_no_lang(self):
        text = '```\n{"title": "Starry Night"}\n```'
        result = server._parse_ai_response(text)
        assert result["title"] == "Starry Night"

    def test_json_after_prose(self):
        text = 'Here is the analysis:\n{"title": "Starry Night", "artist": "Van Gogh"}'
        result = server._parse_ai_response(text)
        assert result["title"] == "Starry Night"

    def test_invalid_text(self):
        assert server._parse_ai_response("no json here") is None

    def test_empty_string(self):
        assert server._parse_ai_response("") is None

    def test_nested_braces_in_strings(self):
        text = '{"title": "Still Life {with Flowers}", "artist": "Unknown"}'
        result = server._parse_ai_response(text)
        assert result["title"] == "Still Life {with Flowers}"


# ---------------------------------------------------------------------------
# _extract_identification
# ---------------------------------------------------------------------------

class TestExtractIdentification:
    def test_empty_dict(self):
        title, artist, score = server._extract_identification({})
        assert title == ""
        assert artist == ""
        assert score == 0.0

    def test_best_guess_labels_only(self):
        web = {"bestGuessLabels": [{"label": "The Starry Night"}]}
        title, artist, score = server._extract_identification(web)
        assert title == "The Starry Night"
        assert artist == ""

    def test_museum_page_by_pattern(self):
        web = {
            "pagesWithMatchingImages": [{
                "pageTitle": "The Cornell Farm by Edward Hicks | Art Institute",
                "url": "https://artic.edu/artworks/12345",
            }],
        }
        title, artist, _ = server._extract_identification(web)
        assert title == "The Cornell Farm"
        assert artist == "Edward Hicks"

    def test_wikipedia_pattern(self):
        web = {
            "pagesWithMatchingImages": [{
                "pageTitle": "Nighthawks (Edward Hopper) - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Nighthawks",
            }],
        }
        title, artist, _ = server._extract_identification(web)
        assert title == "Nighthawks"
        assert artist == "Edward Hopper"

    def test_non_museum_pages_skipped(self):
        web = {
            "pagesWithMatchingImages": [{
                "pageTitle": "Starry Night by Van Gogh",
                "url": "https://randomsite.com/art",
            }],
        }
        title, artist, _ = server._extract_identification(web)
        assert artist == ""

    def test_entity_fallback_for_artist(self):
        web = {
            "bestGuessLabels": [{"label": "The Starry Night"}],
            "webEntities": [
                {"description": "Vincent van Gogh", "score": 1.2},
            ],
        }
        title, artist, _ = server._extract_identification(web)
        assert title == "The Starry Night"
        assert artist == "Vincent van Gogh"

    def test_generic_terms_filtered(self):
        web = {
            "bestGuessLabels": [{"label": "Some Painting"}],
            "webEntities": [
                {"description": "painting", "score": 2.0},
                {"description": "oil painting", "score": 1.5},
                {"description": "Claude Monet", "score": 0.8},
            ],
        }
        _, artist, _ = server._extract_identification(web)
        assert artist == "Claude Monet"

    def test_museum_entities_filtered(self):
        web = {
            "bestGuessLabels": [{"label": "Some Painting"}],
            "webEntities": [
                {"description": "National Gallery of Art", "score": 1.5},
                {"description": "Rembrandt", "score": 0.7},
            ],
        }
        _, artist, _ = server._extract_identification(web)
        assert artist == "Rembrandt"

    def test_low_score_entities_skipped(self):
        web = {
            "bestGuessLabels": [{"label": "Some Painting"}],
            "webEntities": [
                {"description": "J.M.W. Turner", "score": 0.3},
            ],
        }
        _, artist, _ = server._extract_identification(web)
        assert artist == ""


# ---------------------------------------------------------------------------
# _extract_folder_id
# ---------------------------------------------------------------------------

class TestExtractFolderId:
    def test_folders_url(self):
        url = "https://drive.google.com/drive/folders/1A2B3C4D5E"
        assert server._extract_folder_id(url) == "1A2B3C4D5E"

    def test_id_query_param(self):
        url = "https://drive.google.com/open?id=1A2B3C4D5E"
        assert server._extract_folder_id(url) == "1A2B3C4D5E"

    def test_no_match(self):
        assert server._extract_folder_id("https://google.com") is None

    def test_empty_string(self):
        assert server._extract_folder_id("") is None


# ---------------------------------------------------------------------------
# _build_analysis_prompt
# ---------------------------------------------------------------------------

class TestBuildAnalysisPrompt:
    def test_thumbnail_source(self):
        prompt = server._build_analysis_prompt("low-resolution thumbnail", "TEST001")
        assert "small thumbnail" in prompt

    def test_original_source(self):
        prompt = server._build_analysis_prompt("high-resolution original", "TEST001")
        assert "high-resolution" in prompt

    def test_filename_hint_included(self, tmp_data_dir):
        meta = {"artwork": {"TEST001": {"original_filename": "Starry Night - Van Gogh.jpg"}}}
        (tmp_data_dir / "artwork_meta.json").write_text(json.dumps(meta))
        prompt = server._build_analysis_prompt("thumbnail", "TEST001")
        assert "Starry Night - Van Gogh.jpg" in prompt

    def test_hash_filename_suppressed(self, tmp_data_dir):
        meta = {"artwork": {"TEST001": {"original_filename": "full_landscape_abcdef1234567890abcdef1234567890.jpg"}}}
        (tmp_data_dir / "artwork_meta.json").write_text(json.dumps(meta))
        prompt = server._build_analysis_prompt("thumbnail", "TEST001")
        assert "full_landscape_" not in prompt

    def test_vision_hint_included(self):
        prompt = server._build_analysis_prompt(
            "thumbnail", "TEST001",
            vision_title="The Starry Night", vision_artist="Vincent van Gogh",
        )
        assert "Google reverse-image search" in prompt
        assert "The Starry Night" in prompt
        assert "Vincent van Gogh" in prompt
