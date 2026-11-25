"""Single-thumbnail fetches must work on Frames that ignore ``get_thumbnail``.

A 2025 Frame (art API 5.0.1.0) never answers the single-item ``get_thumbnail``
request, but answers ``get_thumbnail_list``.  See issue #95.
"""
from __future__ import annotations

from samsungtvws.exceptions import ConnectionFailure

import server

JPEG = b"\xff\xd8\xff\xe0thumb"


def _tv_without_single_thumbnail(mock_tv):
    mock_tv.get_thumbnail.side_effect = ConnectionFailure("Websocket Time out")


async def test_endpoint_uses_list_request(client, mock_tv, monkeypatch):
    monkeypatch.setattr(server, "TV_RETRY_DELAY", 0)
    _tv_without_single_thumbnail(mock_tv)
    mock_tv.get_thumbnail_list.return_value = {"ART001.jpg": bytearray(JPEG)}

    resp = await client.get("/api/thumbnail/ART001")

    assert resp.status_code == 200
    assert resp.content == JPEG
    mock_tv.get_thumbnail_list.assert_called_once_with(["ART001"])


async def test_prefetch_uses_list_request(mock_tv, monkeypatch):
    monkeypatch.setattr(server, "_PREFETCH_INTER_DELAY", 0)
    monkeypatch.setattr(server, "_PREFETCH_BACKOFF", 0)
    _tv_without_single_thumbnail(mock_tv)
    mock_tv.get_thumbnail_list.side_effect = lambda ids: {f"{ids[0]}.jpg": bytearray(JPEG)}

    await server._prefetch_thumbnails(["ART001", "ART002"])

    assert server._get_cached_thumbnail("ART001") == JPEG
    assert server._get_cached_thumbnail("ART002") == JPEG


async def test_items_without_thumbnail_do_not_abort_prefetch(mock_tv, monkeypatch):
    """The TV closes the data socket for items it has no thumbnail for."""
    monkeypatch.setattr(server, "_PREFETCH_INTER_DELAY", 0)
    monkeypatch.setattr(server, "_PREFETCH_BACKOFF", 0)
    _tv_without_single_thumbnail(mock_tv)

    def _list(ids):
        if ids[0].startswith("SAM"):
            raise ConnectionFailure({"reason": "socket closed"})
        return {f"{ids[0]}.jpg": bytearray(JPEG)}

    mock_tv.get_thumbnail_list.side_effect = _list
    # An aborted run sleeps, then reschedules itself; don't wait 5 minutes.
    monkeypatch.setattr(server, "_PREFETCH_RETRY_COOLDOWN", 0)
    no_thumb = [f"SAM{i}" for i in range(server._PREFETCH_MAX_FAILURES)]

    await server._prefetch_thumbnails([*no_thumb, "ART001"])

    assert server._get_cached_thumbnail("ART001") == JPEG


async def test_endpoint_404_when_tv_has_no_thumbnail(client, mock_tv):
    _tv_without_single_thumbnail(mock_tv)
    mock_tv.get_thumbnail_list.side_effect = ConnectionFailure({"reason": "socket closed"})

    resp = await client.get("/api/thumbnail/SAM001")

    assert resp.status_code == 404
