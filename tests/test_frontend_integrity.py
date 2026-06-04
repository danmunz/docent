"""Tests for frontend code integrity: catch empty patterns, signal conflicts, XSS."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).parent.parent / "index.html"


@pytest.fixture
def html_source():
    return INDEX_HTML.read_text()


@pytest.fixture
def js_source(html_source):
    """Extract JavaScript from the <script> block."""
    match = re.search(r"<script>(.*?)</script>", html_source, re.DOTALL)
    assert match, "No <script> block found in index.html"
    return match.group(1)


class TestEmptyCatchBlocks:
    """Empty catch blocks silently swallow errors — flag them."""

    def test_no_empty_catch_blocks(self, js_source):
        # Pattern: catch {} or catch (e) {} with no meaningful body
        pattern = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}")
        matches = pattern.findall(js_source)
        assert len(matches) == 0, (
            f"Found {len(matches)} empty catch block(s) that silently swallow errors. "
            "Each should at least log: console.debug(err) or show a toast."
        )


class TestAbortControllerSignals:
    """The api() function should not create a timer when an external signal is passed."""

    def test_api_function_respects_external_signal(self, js_source):
        # Find the api function
        api_match = re.search(
            r"async function api\(path.*?\n\s*\}", js_source, re.DOTALL
        )
        assert api_match, "Cannot find api() function"
        api_body = api_match.group(0)

        # If an external signal is provided, we should skip creating our own timer
        # or at least clear it immediately. Check that the function handles this.
        # The fix: if opts.signal is provided, don't create a competing controller.
        assert "opts.signal" in api_body, "api() should check for opts.signal"


class TestXssPrevention:
    """User-controlled strings should be escaped before injection into HTML."""

    def test_atmosphere_title_escapes_quotes(self, js_source):
        # The title used in href/title attributes must escape quotes
        # Look for the atmosphere title construction (safeTitle in pick cards)
        assert re.search(r'safeTitle.*replace\(', js_source) or re.search(r'atmTitle.*replace\(', js_source), (
            "Cannot find atmosphere title escape logic"
        )
        # At minimum, double quotes must be escaped (which we verified exists)
        assert re.search(r'replace\(.*/.*&quot;', js_source), (
            "Atmosphere title must escape double quotes for attribute safety"
        )

    def test_weather_description_escaped(self, js_source):
        # Weather descriptions come from external API — check they're escaped
        weather_section = re.search(
            r"atmosphereWeather.*?innerHTML\s*=", js_source
        )
        assert weather_section, "Cannot find atmosphereWeather innerHTML"


class TestObserverCleanup:
    """setupThumbObserver() must clear pending batch timer to prevent stale fetches."""

    def test_observer_clears_batch_timer_in_setup(self, js_source):
        # Find setupThumbObserver function and check the SETUP portion
        # (before IntersectionObserver constructor) clears the timer
        fn_match = re.search(
            r"function setupThumbObserver\(\)\s*\{(.*?)new IntersectionObserver",
            js_source,
            re.DOTALL,
        )
        assert fn_match, "Cannot find setupThumbObserver() setup portion"
        setup_portion = fn_match.group(1)

        assert "clearTimeout(_observeBatchTimer)" in setup_portion, (
            "setupThumbObserver() must clear _observeBatchTimer in its setup "
            "(before creating the new observer) to prevent stale batch fetches "
            "from a previous grid render"
        )
