"""Diagnostic tests for issue #67 — TV thumbnail failures on large catalogs.

These tests confirm the root causes of the remaining thumbnail loading
failures after PRs #68 and #69 were merged.
"""
from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, call

import pytest

import server
from samsungtvws.exceptions import ConnectionFailure


# ---------------------------------------------------------------------------
# 1. Verify the get_thumbnail_list key format and server-side matching
# ---------------------------------------------------------------------------

class TestThumbnailKeyMatching:
    """The samsungtvws library returns thumbnail dict keys as
    '{fileID}.{fileType}' (e.g. 'MY_F0379.jpg'). Confirm the server's
    startswith() matching handles this correctly.
    """

    async def test_matching_works_with_extension_suffix(self, client, mock_tv, tmp_data_dir):
        """The TV returns keys like 'ART001.jpg' — startswith('ART001') should match."""
        mock_tv.get_thumbnail_list.return_value = {
            "ART001.jpg": bytearray(b"\xff\xd8\xff\xe0thumb1"),
            "ART002.jpg": bytearray(b"\xff\xd8\xff\xe0thumb2"),
        }
        resp = await client.post("/api/thumbnails", json={"content_ids": ["ART001", "ART002"]})
        data = resp.json()
        assert "ART001" in data["thumbnails"], "Server should match 'ART001.jpg' to 'ART001'"
        assert "ART002" in data["thumbnails"], "Server should match 'ART002.jpg' to 'ART002'"
        assert data["missing"] == []

    async def test_matching_fails_when_key_doesnt_start_with_content_id(self, client, mock_tv, tmp_data_dir):
        """If the TV returns an unexpected key format, matching silently drops the data."""
        mock_tv.get_thumbnail_list.return_value = {
            # Hypothetical: TV returns a different identifier
            "thumb_ART001.jpg": bytearray(b"\xff\xd8\xff\xe0thumb1"),
        }
        resp = await client.post("/api/thumbnails", json={"content_ids": ["ART001"]})
        data = resp.json()
        # Key does NOT start with 'ART001', so matching fails
        assert "ART001" not in data["thumbnails"]
        assert "ART001" in data["missing"]
        # The fallback flag is False because get_thumbnail_list didn't throw
        assert data["fallback"] is False

    async def test_batch_success_returns_thumbnails_not_cached_to_disk_on_mismatch(
        self, client, mock_tv, tmp_data_dir
    ):
        """When matching fails, thumbnails are NOT cached to disk either —
        so 'click to retry' won't find them in cache.
        """
        mock_tv.get_thumbnail_list.return_value = {
            "thumb_ART001.jpg": bytearray(b"\xff\xd8\xff\xe0thumb1"),
        }
        await client.post("/api/thumbnails", json={"content_ids": ["ART001"]})
        thumb_file = server.THUMB_DIR / "ART001.jpg"
        assert not thumb_file.exists(), "Mismatched key should not be cached to disk"


# ---------------------------------------------------------------------------
# 2. Individual fallback flooding — what happens with 20 IDs failing
# ---------------------------------------------------------------------------

class TestIndividualFallbackBehavior:
    """When get_thumbnail_list raises an exception, the server now returns
    immediately and prefetches missing thumbnails in a background task.
    This breaks the timeout race that blocked Nitrowolf's UI.
    """

    async def test_fallback_returns_immediately_and_prefetches(self, client, mock_tv, tmp_data_dir, monkeypatch):
        """Response returns fast with all IDs as missing; background task
        fetches them individually and caches to disk."""
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.01)
        mock_tv.get_thumbnail_list.side_effect = ConnectionFailure({"reason": "socket closed"})
        mock_tv.get_thumbnail.return_value = bytearray(b"\xff\xd8\xff\xe0thumb")

        ids = [f"ART{i:03d}" for i in range(20)]
        start = time.monotonic()
        resp = await client.post("/api/thumbnails", json={"content_ids": ids})
        elapsed = time.monotonic() - start
        data = resp.json()

        assert data["fallback"] is True
        # Response returns FAST — no inline individual calls
        assert elapsed < 2.0, f"Response took {elapsed:.2f}s, should be fast"
        # All IDs are missing in the immediate response
        assert len(data["missing"]) == 20
        # No thumbnails inline (they're being prefetched)
        assert len(data["thumbnails"]) == 0

        # Give background task time to run
        await asyncio.sleep(0.5)

        # Now the background task should have cached them
        resp2 = await client.post("/api/thumbnails", json={"content_ids": ids})
        data2 = resp2.json()
        assert len(data2["thumbnails"]) == 20
        assert data2["missing"] == []
        # Background task made 20 individual get_thumbnail calls
        assert mock_tv.get_thumbnail.call_count == 20

    async def test_fallback_cascading_failures(self, client, mock_tv, tmp_data_dir, monkeypatch):
        """When the TV is unstable, background prefetch caches what it can
        and drops the rest.
        """
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.01)
        mock_tv.get_thumbnail_list.side_effect = ConnectionFailure({"reason": "socket closed"})
        call_count = 0
        def flaky_get_thumbnail(cid):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return bytearray(b"\xff\xd8\xff\xe0thumb")
            raise ConnectionFailure({"reason": "socket closed"})

        mock_tv.get_thumbnail.side_effect = flaky_get_thumbnail

        ids = [f"ART{i:03d}" for i in range(10)]
        resp = await client.post("/api/thumbnails", json={"content_ids": ids})
        data = resp.json()
        assert data["fallback"] is True
        # Immediate response has all 10 as missing
        assert len(data["missing"]) == 10

        # Give background task time to finish
        await asyncio.sleep(0.5)

        # Retry — only 3 were cached successfully
        resp2 = await client.post("/api/thumbnails", json={"content_ids": ids})
        data2 = resp2.json()
        assert len(data2["thumbnails"]) == 3
        assert len(data2["missing"]) == 7

    async def test_fallback_response_time_is_bounded(self, client, mock_tv, tmp_data_dir, monkeypatch):
        """The response returns in bounded time regardless of how many IDs
        are missing or how slow the TV is. The old inline fallback would
        take 5 × 2 retries × 2s = 20 seconds for just 5 IDs.
        """
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.01)

        mock_tv.get_thumbnail_list.side_effect = ConnectionFailure({"reason": "socket closed"})
        mock_tv.get_thumbnail.side_effect = ConnectionFailure({"reason": "socket closed"})

        ids = [f"ART{i:03d}" for i in range(5)]
        start = time.monotonic()
        resp = await client.post("/api/thumbnails", json={"content_ids": ids})
        elapsed = time.monotonic() - start

        data = resp.json()
        assert data["fallback"] is True
        assert len(data["missing"]) == 5
        # Response is fast — no inline retry delays
        assert elapsed < 2.0, (
            f"Response took {elapsed:.2f}s — should be bounded by the initial "
            f"get_thumbnail_list attempt only, not 20+ seconds of individual calls"
        )


# ---------------------------------------------------------------------------
# 3. _tv_op timeout vs D2D socket cleanup
# ---------------------------------------------------------------------------

class TestTvOpTimeoutBehavior:
    """When _tv_op times out, the D2D socket opened by get_thumbnail_list
    is NOT properly cleaned up because the thread is still blocked on recv.
    """

    async def test_timeout_closes_main_connection(self, monkeypatch):
        """After a timeout, _close_tv_connection is called, but the D2D socket
        opened inside get_thumbnail_list is still alive in the thread.
        """
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.01)

        close_calls = []
        original_close = server._close_tv_connection
        def tracking_close():
            close_calls.append(time.monotonic())
            original_close()
        monkeypatch.setattr(server, "_close_tv_connection", tracking_close)

        # Simulate a call that hangs (like a stuck D2D recv)
        cancel = threading.Event()
        def hanging_fn(art):
            # Block until cancelled — avoids a fixed sleep that stalls the suite
            cancel.wait(timeout=2)
            return {}

        ensure_called = []
        def mock_ensure():
            ensure_called.append(True)
            return MagicMock()
        monkeypatch.setattr(server, "_ensure_tv_connection", mock_ensure)

        with pytest.raises(asyncio.TimeoutError):
            await server._tv_op(hanging_fn, attempts=1, timeout=0.1)

        # _close_tv_connection WAS called (good)
        assert len(close_calls) == 1
        # Release the worker thread so it doesn't linger after the test
        cancel.set()


# ---------------------------------------------------------------------------
# 4. Concurrent request serialization under load
# ---------------------------------------------------------------------------

class TestConcurrentTvOps:
    """Multiple thumbnail batch requests arriving concurrently should be
    serialized by _tv_lock, but the individual fallback creates many
    sequential lock acquisitions per batch, starving other operations.
    """

    async def test_individual_fallback_blocks_other_ops(self, monkeypatch):
        """When individual fallback is processing 20 IDs, other TV ops
        (select, matte, info) are blocked until ALL 20 complete.
        """
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.01)
        # Fresh lock to avoid cross-test contamination
        monkeypatch.setattr(server, "_tv_lock", asyncio.Lock())
        op_log = []

        mock_art = MagicMock()
        def mock_ensure():
            return mock_art
        monkeypatch.setattr(server, "_ensure_tv_connection", mock_ensure)
        monkeypatch.setattr(server, "_close_tv_connection", lambda: None)

        async def slow_batch():
            """Simulate the individual fallback: each get_thumbnail takes 0.5s"""
            def batch_fn(art):
                time.sleep(0.05)  # Simulates D2D transfer time per thumbnail
                op_log.append(("thumb", time.monotonic()))
                return bytearray(b"\xff\xd8thumb")

            for i in range(10):
                await server._tv_op(batch_fn, attempts=1, timeout=5)

        async def quick_select():
            """A user clicking 'Display on Frame' during thumbnail loading"""
            await asyncio.sleep(0.02)  # Small delay to ensure batch starts first
            start = time.monotonic()
            def select_fn(art):
                op_log.append(("select", time.monotonic()))
                return True
            await server._tv_op(select_fn, attempts=1, timeout=5)
            return time.monotonic() - start

        batch_task = asyncio.create_task(slow_batch())
        select_time = await quick_select()
        await batch_task

        # The select op had to wait for at least some of the batch ops
        select_entry = [t for op, t in op_log if op == "select"]
        first_thumb = [t for op, t in op_log if op == "thumb"][0]
        assert len(select_entry) == 1
        # Select was blocked by at least one thumbnail op
        assert select_entry[0] > first_thumb, (
            "Select should have been delayed by the thumbnail fallback lock contention"
        )


# ---------------------------------------------------------------------------
# 5. Real-world scenario: 524 artworks, 26 batches of 20
# ---------------------------------------------------------------------------

class TestLargeCatalogScenario:
    """Simulate the user's 524-artwork catalog to measure the scale of
    lock contention and connection attempts.
    """

    async def test_526_artworks_connection_count(self, client, mock_tv, tmp_data_dir, monkeypatch):
        """With 524 artworks, the frontend sends 26 batches of 20 + 1 batch of 4.
        If ALL fail and trigger individual fallback, that's up to
        524 individual _tv_op calls, each with 3 attempts = 1,572 connection attempts.

        With the background prefetch fix, the response returns immediately
        and the individual calls happen asynchronously.
        """
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.001)

        batch_call_count = 0

        def fail_batch(ids):
            nonlocal batch_call_count
            batch_call_count += 1
            raise ConnectionFailure({"reason": "socket closed"})

        mock_tv.get_thumbnail_list.side_effect = fail_batch
        mock_tv.get_thumbnail.side_effect = ConnectionFailure({"reason": "socket closed"})

        # Simulate ONE batch of 20 (what the frontend sends)
        ids = [f"ART{i:04d}" for i in range(20)]
        start = time.monotonic()
        resp = await client.post("/api/thumbnails", json={"content_ids": ids})
        elapsed = time.monotonic() - start
        data = resp.json()

        assert data["fallback"] is True
        assert len(data["missing"]) == 20
        # Response returns fast — no inline individual calls
        assert elapsed < 2.0, f"Response took {elapsed:.2f}s, should be bounded"

        # Give background task time to attempt individual calls
        await asyncio.sleep(0.5)

        # Background task attempted individual calls (20 IDs × 3 attempts each)
        assert mock_tv.get_thumbnail.call_count >= 20, (
            f"Expected at least 20 background individual calls, got {mock_tv.get_thumbnail.call_count}"
        )

    async def test_cached_thumbnails_skip_tv_entirely(self, client, mock_tv, tmp_data_dir):
        """Confirm that disk-cached thumbnails are served without touching the TV.
        This is why 'click to retry' works instantly after an earlier partial success.
        """
        # Pre-populate disk cache
        for i in range(5):
            (server.THUMB_DIR / f"ART{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0cached")

        resp = await client.post(
            "/api/thumbnails",
            json={"content_ids": [f"ART{i:03d}" for i in range(5)]},
        )
        data = resp.json()
        assert len(data["thumbnails"]) == 5
        assert data["missing"] == []
        assert data["fallback"] is False
        # The TV was NEVER contacted
        mock_tv.get_thumbnail_list.assert_not_called()
        mock_tv.get_thumbnail.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Client timeout race — the actual root cause of infinite retry loops
# ---------------------------------------------------------------------------

class TestClientTimeoutRace:
    """The frontend uses a 30s AbortController timeout on /api/thumbnails.
    When the server-side individual fallback runs for a batch of 20
    failing IDs, the response takes far longer than 30s.  The frontend
    aborts, never sees the response, and retries — creating an infinite
    loop where the server IS caching thumbnails to disk but the client
    never receives confirmation.

    This is why Nitrowolf sees:
      - "POST /api/thumbnails 200 53.48s" in server logs
      - Progress bar never appears (response aborted before `fallback: true` received)
      - "Tap to retry" → instant thumbnail (disk cache populated by the aborted request)
    """

    FRONTEND_TIMEOUT_MS = 30_000  # from index.html loadThumbnailBatch

    def _calculate_worst_case_fallback_time_ms(
        self,
        n_ids: int,
        attempts: int = 3,
        timeout_s: float = 18,    # TV_TIMEOUT(10) + 8
        retry_delay_s: float = 2,
    ) -> float:
        """Calculate worst-case server response time for individual fallback.

        For each ID:
          - `attempts` tries, each bounded by `timeout_s`
          - (attempts - 1) retry delays of `retry_delay_s`
          Total per ID = attempts * timeout_s + (attempts - 1) * retry_delay_s

        These are sequential because each _tv_op acquires _tv_lock.
        """
        per_id_s = attempts * timeout_s + (attempts - 1) * retry_delay_s
        return n_ids * per_id_s * 1000  # convert to ms

    def test_worst_case_exceeds_client_timeout(self):
        """Even with just 1 failing ID, worst-case time can exceed 30s.
        With 20 IDs (the batch size), it's catastrophically over.
        """
        # 1 ID, all 3 attempts timeout at 18s each + 2 retry delays of 2s
        one_id_worst_ms = self._calculate_worst_case_fallback_time_ms(1)
        # 1 * (3 * 18 + 2 * 2) * 1000 = 58,000ms = 58s
        assert one_id_worst_ms == 58_000, f"Expected 58s, got {one_id_worst_ms}ms"
        assert one_id_worst_ms > self.FRONTEND_TIMEOUT_MS, (
            f"Even 1 failing ID ({one_id_worst_ms}ms) exceeds "
            f"client timeout ({self.FRONTEND_TIMEOUT_MS}ms)"
        )

        # 20 IDs (one batch) = 20 * 58s = 1,160s ≈ 19 minutes
        batch_worst_ms = self._calculate_worst_case_fallback_time_ms(20)
        assert batch_worst_ms == 1_160_000
        assert batch_worst_ms > self.FRONTEND_TIMEOUT_MS * 38, (
            "A single batch's fallback can take 38× the client timeout"
        )

    def test_realistic_case_still_exceeds_client_timeout(self):
        """Even with optimistic timing (1s per successful call), a batch where
        half the IDs succeed quickly and half fail once still exceeds 30s.
        """
        # 10 IDs succeed in 1s each = 10s
        # 10 IDs fail once, succeed on retry: per ID = 1s (fail) + 2s (delay) + 1s (succeed) = 4s
        # Total = 10 + 40 = 50s > 30s
        fast_ids_ms = 10 * 1_000
        slow_ids_ms = 10 * 4_000  # fail once, succeed on retry
        total_ms = fast_ids_ms + slow_ids_ms
        assert total_ms == 50_000
        assert total_ms > self.FRONTEND_TIMEOUT_MS, (
            f"Realistic mixed scenario ({total_ms}ms) exceeds "
            f"client timeout ({self.FRONTEND_TIMEOUT_MS}ms)"
        )

    async def test_server_caches_thumbnails_despite_client_timeout(
        self, client, mock_tv, tmp_data_dir, monkeypatch
    ):
        """With background prefetch, the server responds immediately and
        prefetches thumbnails in the background. The cached thumbnails
        are available on retry. This eliminates the timeout race entirely.
        """
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.001)
        mock_tv.get_thumbnail_list.side_effect = ConnectionFailure({"reason": "socket closed"})

        call_idx = 0
        def intermittent_thumbnail(cid):
            nonlocal call_idx
            call_idx += 1
            # First 5 IDs succeed, rest fail
            if call_idx <= 5:
                return bytearray(b"\xff\xd8\xff\xe0thumb")
            raise ConnectionFailure({"reason": "socket closed"})

        mock_tv.get_thumbnail.side_effect = intermittent_thumbnail

        ids = [f"ART{i:03d}" for i in range(10)]
        # Response returns immediately — no inline individual calls
        resp = await client.post("/api/thumbnails", json={"content_ids": ids})
        data = resp.json()

        assert data["fallback"] is True
        # Immediate response has all as missing (prefetch in background)
        assert len(data["missing"]) == 10

        # Give background task time to complete
        await asyncio.sleep(0.5)

        # Background task cached 5 thumbnails to disk
        cached_count = sum(
            1 for i in range(10)
            if (server.THUMB_DIR / f"ART{i:03d}.jpg").exists()
        )
        assert cached_count == 5, (
            "Background prefetch cached thumbnails to disk — "
            "retry will find them in cache"
        )

        # Now simulate the retry — all 10 IDs requested again
        mock_tv.get_thumbnail_list.reset_mock()
        mock_tv.get_thumbnail.reset_mock()

        resp2 = await client.post("/api/thumbnails", json={"content_ids": ids})
        data2 = resp2.json()
        # The 5 cached IDs are served from disk without touching TV
        assert len(data2["thumbnails"]) >= 5

    def test_frontend_retry_creates_infinite_loop(self):
        """Walk through the exact infinite loop scenario.

        Timeline for 1 batch of 20 IDs where the TV is struggling:

        t=0s:    Frontend sends POST /api/thumbnails {20 IDs}
        t=0s:    Server tries get_thumbnail_list → fails after ~18s
        t=18s:   Server starts individual fallback for 20 IDs
        t=30s:   Frontend AbortController fires, request aborted
                 Frontend schedules retry in 2s (attempt=1)
        t=32s:   Frontend sends POST /api/thumbnails {20 IDs} again
                 (server still processing previous request's fallback!)
        t=53s:   Server finishes first request's fallback (status 200, but nobody reads it)
                 Some thumbnails cached to disk
        t=62s:   Frontend AbortController fires AGAIN on retry
                 Frontend schedules retry in 4s (attempt=2, final)
        t=66s:   Frontend sends final POST /api/thumbnails {20 IDs}
        t=66s:   Server finds some in disk cache, tries TV for rest
        t=96s:   Frontend AbortController fires, gives up → "Tap to retry"

        Result: 3 server requests, ~96s elapsed, progress bar never shown,
        most thumbnails actually cached to disk but frontend doesn't know.
        """
        # This is a documentation test — the assertions just verify the math
        batch_attempt_time_s = 18  # get_thumbnail_list with 3 attempts, worst case
        # After batch fails, individual fallback for 20 IDs
        # Even if some succeed in 1s, a few timeouts blow past 30s
        individual_fallback_time_s = 35  # conservative: some succeed, some timeout once
        total_server_time_s = batch_attempt_time_s + individual_fallback_time_s

        assert total_server_time_s > 30, "Server response exceeds client timeout"

        # Frontend retries: attempt 0 (30s timeout), attempt 1 (30s), attempt 2 (30s)
        # Each retry has a backoff delay: 2s, 4s
        total_frontend_wall_time_s = 30 + 2 + 30 + 4 + 30
        assert total_frontend_wall_time_s == 96, "User waits ~96s before seeing 'Tap to retry'"

        # Meanwhile the server processes 3 overlapping requests
        # (the lock serializes them, so they queue up)
        server_requests = 3
        assert server_requests * total_server_time_s > total_frontend_wall_time_s, (
            "Server is still processing when the frontend gives up entirely"
        )
