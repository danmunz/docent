"""Tests for the /api/upload endpoint.

Regression coverage for the upload path, which had no tests — a size-limit
change (commit c96df06) replaced ``await file.read()`` with
``async for chunk in file`` which raises ``TypeError`` on a Starlette
``UploadFile`` (it is not async-iterable), breaking every upload.
"""
from __future__ import annotations

import io

from PIL import Image


def _image_bytes(fmt: str = "PNG", size: tuple[int, int] = (32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(buf, format=fmt)
    return buf.getvalue()


class TestUpload:
    async def test_upload_reads_file_and_pushes_to_tv(self, client, mock_tv):
        """A normal upload streams the file and reaches the TV (regression:
        the streaming read used to crash with a TypeError)."""
        mock_tv.upload.return_value = "MY_F0001"

        resp = await client.post(
            "/api/upload",
            files={"file": ("art.png", _image_bytes(), "image/png")},
            data={"matte": "none", "filename": "Test Art"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["content_id"] == "MY_F0001"
        assert body["title"] == "Test Art"
        assert mock_tv.upload.called

    async def test_upload_enforces_size_limit(self, client, mock_tv, monkeypatch):
        """The 50 MB cap (the reason the streaming read was introduced) still
        works: an oversized upload is rejected with 413."""
        monkeypatch.setattr("server.MAX_UPLOAD_BYTES", 8)  # smaller than any real image

        resp = await client.post(
            "/api/upload",
            files={"file": ("big.png", _image_bytes(), "image/png")},
            data={"matte": "none"},
        )

        assert resp.status_code == 413
        assert not mock_tv.upload.called

    async def test_upload_rejects_unsupported_format(self, client, mock_tv):
        """BMP is bomb-prone and not in the allowlist — rejected with 400."""
        resp = await client.post(
            "/api/upload",
            files={"file": ("art.bmp", _image_bytes(fmt="BMP"), "image/bmp")},
            data={"matte": "none"},
        )

        assert resp.status_code == 400
        assert not mock_tv.upload.called

    async def test_upload_rejects_decompression_bomb(self, client, mock_tv, monkeypatch):
        """Images exceeding MAX_IMAGE_PIXELS are rejected with 400."""
        monkeypatch.setattr("server.Image.MAX_IMAGE_PIXELS", 100)

        resp = await client.post(
            "/api/upload",
            files={"file": ("art.png", _image_bytes(), "image/png")},
            data={"matte": "none"},
        )

        assert resp.status_code == 400
        assert not mock_tv.upload.called
