"""Tests for the resilient TV connection helper (`_tv_op`) and Wake-on-LAN.

The Samsung Frame frequently accepts the TCP socket without answering the
art-mode handshake (busy with its on-screen UI, or mid sleep/wake), so a
single connection attempt is unreliable. `_tv_op` retries with a Wake-on-LAN
nudge and bounds each attempt with a timeout so a hung call can never hold the
TV lock forever.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import server
from samsungtvws.exceptions import ResponseError


def _ok_tv():
    tv = MagicMock()
    art = MagicMock()
    tv.art.return_value = art
    art.open.return_value = None
    art.supported.return_value = True
    return tv


class TestTvOpReliability:
    async def test_retries_then_succeeds_and_wakes_tv(self, monkeypatch):
        """A first connection failure triggers a Wake-on-LAN and a retry that
        succeeds — rather than surfacing as 'TV went to sleep'."""
        attempts = {"n": 0}

        def flaky_get_tv():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("No route to host")
            return _ok_tv()

        woke = {"n": 0}
        monkeypatch.setattr(server, "get_tv", flaky_get_tv)
        monkeypatch.setattr(server, "_wake_tv", lambda: woke.__setitem__("n", woke["n"] + 1))
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0)

        result = await server._tv_op(lambda art: art.supported())

        assert result is True
        assert attempts["n"] == 2   # retried exactly once
        assert woke["n"] == 1       # WoL sent before the retry

    async def test_gives_up_after_max_attempts(self, monkeypatch):
        attempts = {"n": 0}

        def always_fail():
            attempts["n"] += 1
            raise OSError("No route to host")

        monkeypatch.setattr(server, "get_tv", always_fail)
        monkeypatch.setattr(server, "_wake_tv", lambda: None)
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0)
        monkeypatch.setattr(server, "TV_CONNECT_ATTEMPTS", 3)

        with pytest.raises(OSError):
            await server._tv_op(lambda art: art.supported())
        assert attempts["n"] == 3

    async def test_definitive_response_error_is_not_retried(self, monkeypatch):
        """A ResponseError means the TV answered with a verdict (e.g. matte
        '-10') — retrying is pointless and could duplicate work."""
        attempts = {"n": 0}

        def get_tv():
            attempts["n"] += 1
            tv = _ok_tv()
            tv.art.return_value.upload.side_effect = ResponseError("send_image -10")
            return tv

        woke = {"n": 0}
        monkeypatch.setattr(server, "get_tv", get_tv)
        monkeypatch.setattr(server, "_wake_tv", lambda: woke.__setitem__("n", woke["n"] + 1))
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0)

        with pytest.raises(ResponseError):
            await server._tv_op(lambda art: art.upload(b"x"))
        assert attempts["n"] == 1   # not retried
        assert woke["n"] == 0       # no WoL on a definitive error

    async def test_single_attempt_mode_does_not_retry(self, monkeypatch):
        """Uploads pass attempts=1 so a lost response can't trigger a duplicate."""
        attempts = {"n": 0}

        def always_fail():
            attempts["n"] += 1
            raise OSError("connection reset")

        monkeypatch.setattr(server, "get_tv", always_fail)
        monkeypatch.setattr(server, "_wake_tv", lambda: None)

        with pytest.raises(OSError):
            await server._tv_op(lambda art: art.upload(b"x"), attempts=1)
        assert attempts["n"] == 1


class TestWakeOnLan:
    def test_builds_and_sends_magic_packet(self, monkeypatch):
        sent = []

        class FakeSock:
            def setsockopt(self, *a):
                pass

            def sendto(self, data, addr):
                sent.append((data, addr))

            def close(self):
                pass

        monkeypatch.setattr(server.socket, "socket", lambda *a, **k: FakeSock())
        monkeypatch.setattr(server, "TV_MAC", "a4:30:7a:61:7a:e4")
        monkeypatch.setattr(server, "TV_IP", "192.168.1.50")

        server._wake_tv()

        assert sent, "no WoL packet was sent"
        data, _addr = sent[0]
        assert len(data) == 102            # 6x 0xFF + 16x 6-byte MAC
        assert data[:6] == b"\xff" * 6
        assert data[6:12] == bytes.fromhex("a4307a617ae4")

    def test_no_mac_is_noop(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(server.socket, "socket", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setattr(server, "TV_MAC", "")
        server._wake_tv()
        assert called["n"] == 0


class TestTvWorker:
    """TV calls run on one long-lived thread that can be unstuck or replaced.

    samsungtvws bounds each socket read but not the loops around them, so a
    misbehaving TV can park the calling thread for good — and a parked thread
    can't be killed. These cover the two properties that keeps contained: the
    thread count stays fixed, and a parked worker is written off rather than
    left to swallow every later call.
    """

    @pytest.fixture(autouse=True)
    def _stub_connection(self, monkeypatch):
        monkeypatch.setattr(server, "_ensure_tv_connection", lambda: MagicMock())
        monkeypatch.setattr(server, "_close_tv_connection", lambda: None)
        monkeypatch.setattr(server, "TV_RETRY_DELAY", 0)

    async def test_all_ops_share_one_thread(self):
        """One worker, not one thread per call — that's what bounds the leak."""
        threads = set()

        def record(art):
            threads.add(threading.current_thread().name)
            return True

        for _ in range(5):
            await server._tv_op(record, attempts=1, timeout=1)

        assert len(threads) == 1

    async def test_parked_worker_is_replaced_and_later_ops_still_run(self, monkeypatch):
        """A call that never returns must not swallow every op after it."""
        monkeypatch.setattr(server, "TV_RECOVER_GRACE", 0.05)
        release = threading.Event()

        await server._tv_op(lambda art: "ok", attempts=1, timeout=1)
        wedged = server._tv_worker

        with pytest.raises(asyncio.TimeoutError):
            await server._tv_op(lambda art: release.wait(timeout=5), attempts=1, timeout=0.05)

        assert server._tv_worker is not wedged, "parked worker was not written off"

        # The next op must run on the fresh worker rather than queue behind
        # the parked job, which is still holding the old thread.
        assert await server._tv_op(lambda art: "after", attempts=1, timeout=1) == "after"
        release.set()

    def test_interrupt_shuts_down_the_socket(self, monkeypatch):
        """Shutting the socket down is the only way to unstick a blocked read."""
        how = []
        sock = SimpleNamespace(shutdown=how.append)
        monkeypatch.setattr(server, "_tv_art", SimpleNamespace(connection=SimpleNamespace(sock=sock)))

        assert server._interrupt_tv_socket() is True
        assert how == [server.socket.SHUT_RDWR]

    def test_interrupt_without_a_connection_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(server, "_tv_art", None)
        assert server._interrupt_tv_socket() is False
