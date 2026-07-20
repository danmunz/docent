"""Unit tests for _open_validated_image (issue #24): format allowlist and
decompression-bomb guarding. Covers both the /api/upload and Drive-import
call sites, which share this helper.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

import server


def _image_bytes(fmt: str = "PNG", size: tuple[int, int] = (32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(buf, format=fmt)
    return buf.getvalue()


class TestOpenValidatedImage:
    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
    def test_accepts_allowed_formats(self, fmt):
        img = server._open_validated_image(_image_bytes(fmt=fmt))
        assert img.format == fmt

    def test_rejects_bmp(self):
        with pytest.raises(server._UnsupportedImageError):
            server._open_validated_image(_image_bytes(fmt="BMP"))

    def test_rejects_garbage_bytes(self):
        with pytest.raises(server._UnsupportedImageError):
            server._open_validated_image(b"not an image")

    def test_rejects_oversized_dimensions(self, monkeypatch):
        monkeypatch.setattr(server.Image, "MAX_IMAGE_PIXELS", 100)
        with pytest.raises(server._UnsupportedImageError):
            server._open_validated_image(_image_bytes())
