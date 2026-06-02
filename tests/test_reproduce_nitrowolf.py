"""Reproduce Nitrowolf's exact failure pattern from issue #67 / PR #69.

Nitrowolf (524 artworks, Docker) reports:
  - Thumbnails fail to load; most show "Tap to retry"
  - Progress bar never appears
  - Clicking a failed thumbnail pops it up instantly
  - Server logs show "POST /api/thumbnails 200 53.48s"
  - Cascading ConnectionFailure / socket closed errors

This test replicates the exact same behavior locally by simulating the
TV's failure pattern with scaled-down timing to keep tests fast.

Scaling: real timing → test timing  (50× speedup)
  TV_RETRY_DELAY:   2s    → 0.04s
  TV per-op:        ~2s   → 0.04s
  Client timeout:   30s   → 0.6s (proportional)

All the same dynamics apply — the ratio between server response time
and client timeout is what matters, not the absolute values.
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import MagicMock

import pytest

import server
from samsungtvws.exceptions import ConnectionFailure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_art(
    *,
    batch_fail: bool = True,
    individual_fail_after: int = 0,
    per_call_delay: float = 0.04,
):
    """Create a mock art object that simulates Nitrowolf's TV behavior.

    Args:
        batch_fail: If True, get_thumbnail_list always raises ConnectionFailure.
        individual_fail_after: Number of successful get_thumbnail calls before
            all subsequent calls raise ConnectionFailure. 0 = all fail.
        per_call_delay: Simulated D2D transfer time per operation.
    """
    art = MagicMock()
    call_count = {"individual": 0, "batch": 0}

    def mock_batch(ids):
        call_count["batch"] += 1
        time.sleep(per_call_delay)
        if batch_fail:
            raise ConnectionFailure({"reason": "socket closed"})
        return {
            f"{cid}.jpg": bytearray(b"\xff\xd8\xff\xe0thumb")
            for cid in ids
        }

    def mock_individual(cid):
        call_count["individual"] += 1
        time.sleep(per_call_delay)
        if call_count["individual"] <= individual_fail_after:
            return bytearray(b"\xff\xd8\xff\xe0thumb-" + cid.encode())
        raise ConnectionFailure({"reason": "socket closed"})

    art.get_thumbnail_list.side_effect = mock_batch
    art.get_thumbnail.side_effect = mock_individual
    art._call_count = call_count
    return art


# ---------------------------------------------------------------------------
# Test: Reproduce the full Nitrowolf scenario
# ---------------------------------------------------------------------------

class TestNitrowolfReproduction:
    """End-to-end reproduction of Nitrowolf's failure pattern."""

    @pytest.fixture
    def nitrowolf_tv(self, monkeypatch):
        """Configure the server to simulate Nitrowolf's TV behavior."""
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.04)
        monkeypatch.setattr(server, "_tv_lock", asyncio.Lock())

        art = _make_mock_art(
            batch_fail=True,
            individual_fail_after=3,  # First 3 individual calls succeed, rest fail
            per_call_delay=0.04,
        )

        monkeypatch.setattr(server, "_ensure_tv_connection", lambda: art)
        monkeypatch.setattr(server, "_close_tv_connection", lambda: None)
        return art

    async def test_server_response_exceeds_client_timeout(
        self, client, nitrowolf_tv, tmp_data_dir
    ):
        """The server takes longer to respond than the frontend's 30s timeout.

        This is the smoking gun: the frontend ALWAYS aborts before seeing
        the response, so the progress bar never shows and the thumbnail
        data is lost (even though it's cached on disk).

        Timeline (scaled 50×):
          t=0.00s  Server receives batch of 20 IDs
          t=0.12s  get_thumbnail_list fails after 3 attempts (0.04s × 3)
          t=0.20s  Individual fallback starts (20 IDs × 3 attempts × 0.04s)
          t=0.60s  ← CLIENT TIMEOUT (30s equivalent) — frontend aborts here
          t=~2.5s  Server finally finishes processing all 20 IDs

        Real timing (unscaled):
          t=0s     Server receives batch of 20 IDs
          t=~6s    get_thumbnail_list fails after 3 attempts
          t=~10s   Individual fallback starts
          t=30s    ← CLIENT TIMEOUT — frontend aborts
          t=~53s   Server finally responds (Nitrowolf saw 53.48s)
        """
        SCALED_CLIENT_TIMEOUT = 0.6  # 30s / 50
        ids = [f"MY_F{i:04d}" for i in range(20)]  # Nitrowolf's ID format

        # Time the full server response
        start = time.monotonic()
        resp = await client.post("/api/thumbnails", json={"content_ids": ids})
        server_time = time.monotonic() - start

        data = resp.json()

        # 1. Server response took longer than the client timeout
        assert server_time > SCALED_CLIENT_TIMEOUT, (
            f"Server responded in {server_time:.2f}s, but client timeout is "
            f"{SCALED_CLIENT_TIMEOUT:.2f}s. Expected server to be SLOWER. "
            f"This means the frontend would abort before receiving this response."
        )

        # 2. Server DID enter fallback mode
        assert data["fallback"] is True, (
            "Server should have fallen back to individual fetches"
        )

        # 3. Some thumbnails were fetched (first 3 succeed)
        assert len(data["thumbnails"]) == 3, (
            f"Expected 3 successful thumbnails, got {len(data['thumbnails'])}"
        )

        # 4. 17 thumbnails are still missing
        assert len(data["missing"]) == 17

        # 5. Extrapolate to real timing
        real_time_estimate = server_time * 50
        print(f"\n{'='*60}")
        print(f"REPRODUCTION RESULTS")
        print(f"{'='*60}")
        print(f"Server response time (scaled):  {server_time:.2f}s")
        print(f"Client timeout (scaled):        {SCALED_CLIENT_TIMEOUT:.2f}s")
        print(f"Server exceeded timeout by:     {server_time - SCALED_CLIENT_TIMEOUT:.2f}s")
        print(f"")
        print(f"Estimated real timing:          ~{real_time_estimate:.0f}s")
        print(f"Nitrowolf's observed timing:    53.48s")
        print(f"Real client timeout:            30s")
        print(f"")
        print(f"Thumbnails fetched:             {len(data['thumbnails'])}/20")
        print(f"Still missing:                  {len(data['missing'])}/20")
        print(f"Progress bar would show:        NO (response never received)")
        print(f"{'='*60}")

    async def test_click_to_retry_reads_from_cache(
        self, client, nitrowolf_tv, tmp_data_dir
    ):
        """After the server processes a batch (even if client aborts), cached
        thumbnails are available instantly on retry.

        This is why Nitrowolf says: "I decided to click on them and the
        thumbnail popped up instantly."
        """
        ids = [f"MY_F{i:04d}" for i in range(20)]

        # First request: server processes all 20, caches 3 to disk
        await client.post("/api/thumbnails", json={"content_ids": ids})

        # Verify disk cache
        cached = [
            cid for cid in ids
            if (server.THUMB_DIR / f"{cid}.jpg").exists()
        ]
        assert len(cached) == 3, (
            f"Expected 3 cached thumbnails on disk, found {len(cached)}"
        )

        # Second request: "click to retry" on one of the cached IDs
        # This simulates what happens when the user clicks a failed thumbnail
        retry_id = cached[0]
        start = time.monotonic()
        resp = await client.post(
            "/api/thumbnails",
            json={"content_ids": [retry_id]},
        )
        retry_time = time.monotonic() - start

        data = resp.json()
        assert retry_id in data["thumbnails"], "Cached thumbnail should load"
        assert data["fallback"] is False, "No fallback needed — served from cache"
        assert data["missing"] == []

        # The retry was nearly instant (no TV contact)
        assert retry_time < 0.1, (
            f"Retry took {retry_time:.3f}s — should be instant from disk cache"
        )
        print(f"\nClick-to-retry time: {retry_time*1000:.1f}ms (instant from cache)")

    async def test_progress_bar_never_shown(
        self, client, nitrowolf_tv, tmp_data_dir
    ):
        """The progress bar is gated on `data.fallback === true` in the
        frontend response. But the frontend aborts before receiving the
        response, so `_thumbFallbackSeen` is never set to true.

        Nitrowolf confirmed: "No, I've not seen any loading progress bar."
        """
        SCALED_CLIENT_TIMEOUT = 0.6
        ids = [f"MY_F{i:04d}" for i in range(20)]

        # Simulate the frontend's AbortController behavior:
        # Fire the request and check if we can get a response within timeout
        fallback_seen = False
        timed_out = False

        async def frontend_request():
            nonlocal fallback_seen
            resp = await client.post(
                "/api/thumbnails",
                json={"content_ids": ids},
            )
            data = resp.json()
            if data.get("fallback"):
                fallback_seen = True  # This would trigger the progress bar
            return data

        try:
            await asyncio.wait_for(
                frontend_request(),
                timeout=SCALED_CLIENT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            timed_out = True

        # The request timed out — exactly what happens in Nitrowolf's browser
        assert timed_out, "Request should have exceeded the client timeout"

        # And because it timed out, the frontend never saw fallback=true
        assert not fallback_seen, (
            "Progress bar should NOT be triggered — "
            "response was aborted before it arrived"
        )


class TestRetryLoopReproduction:
    """Reproduce the frontend's retry-abort-retry loop that creates the
    cascading failure pattern in Nitrowolf's logs.
    """

    @pytest.fixture
    def struggling_tv(self, monkeypatch):
        """TV that fails everything — worst case scenario."""
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.04)
        monkeypatch.setattr(server, "_tv_lock", asyncio.Lock())

        art = _make_mock_art(
            batch_fail=True,
            individual_fail_after=0,  # ALL individual calls also fail
            per_call_delay=0.04,
        )

        monkeypatch.setattr(server, "_ensure_tv_connection", lambda: art)
        monkeypatch.setattr(server, "_close_tv_connection", lambda: None)
        return art

    async def test_three_attempt_retry_loop(
        self, client, struggling_tv, tmp_data_dir
    ):
        """Simulate the frontend's exact retry logic from loadThumbnailBatch:

            attempt 0: send batch → timeout after 30s → catch, retry in 2s
            attempt 1: send batch → timeout after 30s → catch, retry in 4s
            attempt 2: send batch → timeout after 30s → catch, show errors

        With the _tv_lock, these requests serialize: while attempt 0's server
        handler is still processing, attempt 1 waits for the lock, making
        everything even slower.
        """
        SCALED_CLIENT_TIMEOUT = 0.6
        BATCH_SIZE = 20
        ids = [f"MY_F{i:04d}" for i in range(BATCH_SIZE)]

        timeline = []
        start = time.monotonic()

        def log_event(event: str):
            elapsed = time.monotonic() - start
            timeline.append((elapsed, event))

        # Simulate 3 frontend attempts (matching loadThumbnailBatch attempt < 2 + final)
        for attempt in range(3):
            log_event(f"attempt {attempt}: sending POST /api/thumbnails")
            try:
                await asyncio.wait_for(
                    client.post("/api/thumbnails", json={"content_ids": ids}),
                    timeout=SCALED_CLIENT_TIMEOUT,
                )
                log_event(f"attempt {attempt}: received response (unexpected!)")
                break
            except asyncio.TimeoutError:
                log_event(f"attempt {attempt}: ABORTED (timeout after {SCALED_CLIENT_TIMEOUT}s)")

            # Frontend retry backoff: 2s × (attempt + 1), scaled
            if attempt < 2:
                backoff = 0.04 * (attempt + 1)  # 2s×1, 2s×2 scaled
                log_event(f"attempt {attempt}: scheduling retry in {backoff:.2f}s")
                await asyncio.sleep(backoff)

        total_time = time.monotonic() - start
        log_event("ALL ATTEMPTS EXHAUSTED → showing 'Tap to retry'")

        # Print timeline matching Nitrowolf's log format
        print(f"\n{'='*60}")
        print(f"FRONTEND RETRY LOOP REPRODUCTION")
        print(f"{'='*60}")
        for elapsed, event in timeline:
            real_equiv = elapsed * 50
            print(f"  t={elapsed:6.2f}s (≈{real_equiv:5.0f}s real)  {event}")
        print(f"{'='*60}")
        print(f"Total wall time (scaled):   {total_time:.2f}s")
        print(f"Total wall time (real est):  ~{total_time * 50:.0f}s")
        print(f"Thumbnails loaded:           0/{BATCH_SIZE}")
        print(f"User experience:             'Tap to retry' on ALL thumbnails")
        print(f"{'='*60}")

        # Assertions
        aborts = [e for _, e in timeline if "ABORTED" in e]
        assert len(aborts) >= 2, (
            f"Expected at least 2 timeout aborts, got {len(aborts)}. "
            f"The frontend aborts every request before the server responds."
        )

    async def test_concurrent_batches_compound_the_problem(
        self, client, struggling_tv, tmp_data_dir
    ):
        """Nitrowolf has 524 artworks = 26 batches of 20 + 1 batch of 4.
        The frontend sends these as the IntersectionObserver triggers for
        visible cards. Multiple batches are in-flight simultaneously, but
        _tv_lock serializes them — each waiting batch delays all others.

        This test shows that even 3 concurrent batches (the first 3 that
        fire from IntersectionObserver) create a massive queue.
        """
        SCALED_CLIENT_TIMEOUT = 0.6
        batch_ids = [
            [f"MY_F{i:04d}" for i in range(0, 20)],
            [f"MY_F{i:04d}" for i in range(20, 40)],
            [f"MY_F{i:04d}" for i in range(40, 60)],
        ]

        results = {"completed": 0, "timed_out": 0}
        start = time.monotonic()

        async def send_batch(ids, batch_num):
            try:
                await asyncio.wait_for(
                    client.post("/api/thumbnails", json={"content_ids": ids}),
                    timeout=SCALED_CLIENT_TIMEOUT,
                )
                results["completed"] += 1
            except asyncio.TimeoutError:
                results["timed_out"] += 1

        # Fire 3 batches concurrently (like IntersectionObserver would)
        await asyncio.gather(
            send_batch(batch_ids[0], 0),
            send_batch(batch_ids[1], 1),
            send_batch(batch_ids[2], 2),
        )
        total_time = time.monotonic() - start

        # All 3 batches should time out
        assert results["timed_out"] >= 2, (
            f"Expected at least 2 timeouts (got {results['timed_out']}). "
            f"The _tv_lock serializes all batches, and each one exceeds "
            f"the client timeout."
        )

        print(f"\n{'='*60}")
        print(f"CONCURRENT BATCH REPRODUCTION")
        print(f"{'='*60}")
        print(f"Batches sent concurrently:  3 (of {len(batch_ids[0])} IDs each)")
        print(f"Completed before timeout:   {results['completed']}")
        print(f"Timed out:                  {results['timed_out']}")
        print(f"Total wall time (scaled):   {total_time:.2f}s")
        print(f"Total wall time (real est):  ~{total_time * 50:.0f}s")
        print(f"{'='*60}")


class TestSettingsDelayReproduction:
    """Reproduce Nitrowolf's report: "Clicking settings is still massively
    delayed and pops up over modal dialogs. It took over a minute before
    the first settings window popped up."

    This happens because the Settings endpoint calls _tv_op (for info/mattes)
    and the lock is held by the thumbnail fallback loop.
    """

    @pytest.fixture
    def busy_tv(self, monkeypatch):
        """TV occupied with thumbnail fallback."""
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0.04)
        monkeypatch.setattr(server, "_tv_lock", asyncio.Lock())

        art = _make_mock_art(
            batch_fail=True,
            individual_fail_after=0,
            per_call_delay=0.04,
        )
        # Mattes endpoint also uses the TV
        art.get_matte_list.return_value = [
            {"matte_id": "none", "matte_type": "none"}
        ]

        monkeypatch.setattr(server, "_ensure_tv_connection", lambda: art)
        monkeypatch.setattr(server, "_close_tv_connection", lambda: None)
        return art

    async def test_settings_blocked_by_thumbnail_fallback(
        self, client, busy_tv, tmp_data_dir
    ):
        """While thumbnail individual fallback is processing, a GET /api/mattes
        request (triggered by opening Settings) is blocked by _tv_lock.

        The lock IS released during retry delays (by design), but the
        individual fallback loop holds the lock for each of the 20 IDs
        sequentially — so a mattes request has to wait for at least one
        full _tv_op cycle to complete before it can acquire the lock.
        """
        ids = [f"MY_F{i:04d}" for i in range(20)]

        start = time.monotonic()
        results = {}

        async def thumbnail_batch():
            """Simulate ongoing thumbnail loading."""
            resp = await client.post(
                "/api/thumbnails",
                json={"content_ids": ids},
            )
            results["thumb_time"] = time.monotonic() - start
            return resp

        async def settings_request():
            """User clicks Settings AFTER batch failure, during individual
            fallback phase. We delay long enough to ensure the batch
            attempts are done and individual fallback has started.
            """
            # Wait for batch to fail (3 attempts × ~0.04s + delays ≈ 0.2s)
            # and individual fallback to start processing IDs
            await asyncio.sleep(0.4)
            req_start = time.monotonic()
            resp = await client.get("/api/mattes")
            results["mattes_time"] = time.monotonic() - start
            results["mattes_wait"] = time.monotonic() - req_start
            return resp

        await asyncio.gather(thumbnail_batch(), settings_request())

        # The mattes request waited while individual fallback held the lock
        assert results["mattes_wait"] > 0.03, (
            f"Settings/mattes request should be delayed by lock contention "
            f"during individual fallback. Waited {results['mattes_wait']:.3f}s"
        )

        print(f"\n{'='*60}")
        print(f"SETTINGS DELAY REPRODUCTION")
        print(f"{'='*60}")
        print(f"Thumbnail batch completed at:  t={results['thumb_time']:.2f}s")
        print(f"Settings/mattes completed at:  t={results['mattes_time']:.2f}s")
        print(f"Settings wait time (scaled):   {results['mattes_wait']:.2f}s")
        print(f"Settings wait time (real est):  ~{results['mattes_wait'] * 50:.0f}s")
        print(f"Nitrowolf reported:            'over a minute'")
        print(f"{'='*60}")
