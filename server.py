from __future__ import annotations

import base64
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image
from samsungtvws import SamsungTVWS

TV_IP = "192.168.1.36"
TV_PORT = 8001
TV_TIMEOUT = 15
TOKEN_FILE = Path(__file__).parent / ".tv-token"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("frame-art-manager")


def get_tv() -> SamsungTVWS:
    return SamsungTVWS(
        TV_IP,
        port=TV_PORT,
        timeout=TV_TIMEOUT,
        token_file=str(TOKEN_FILE),
    )


def art_connection(tv: SamsungTVWS):
    art = tv.art()
    art.open()
    return art


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Frame Art Manager starting — TV at %s:%s", TV_IP, TV_PORT)
    yield


app = FastAPI(title="Frame Art Manager", lifespan=lifespan)


# --- Static frontend ---

@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


# --- TV info ---

@app.get("/api/info")
async def tv_info():
    try:
        tv = get_tv()
        art = art_connection(tv)
        try:
            supported = art.supported()
            api_version = art.get_api_version() if supported else None
            artmode = art.get_artmode() if supported else None
            return {
                "supported": supported,
                "api_version": api_version,
                "artmode": artmode,
                "ip": TV_IP,
            }
        finally:
            tv.close()
    except Exception as e:
        raise HTTPException(502, f"Cannot reach TV: {e}")


# --- Art listing ---

@app.get("/api/art")
async def list_art():
    tv = get_tv()
    art = art_connection(tv)
    try:
        items = art.available()
        current = art.get_current()
        current_id = current.get("content_id") if isinstance(current, dict) else None
        return {"items": items, "current_id": current_id}
    finally:
        tv.close()


# --- Thumbnails ---

@app.get("/api/thumbnail/{content_id}")
async def get_thumbnail(content_id: str):
    tv = get_tv()
    art = art_connection(tv)
    try:
        data = art.get_thumbnail(content_id)
        if not data:
            raise HTTPException(404, "No thumbnail")
        return Response(content=bytes(data), media_type="image/jpeg")
    finally:
        tv.close()


@app.post("/api/thumbnails")
async def get_thumbnails_batch(body: dict):
    content_ids = body.get("content_ids", [])
    if not content_ids:
        return {"thumbnails": {}}
    tv = get_tv()
    art = art_connection(tv)
    try:
        result = art.get_thumbnail_list(content_ids)
        encoded = {}
        for name, data in result.items():
            encoded[name] = base64.b64encode(bytes(data)).decode()
        return {"thumbnails": encoded}
    finally:
        tv.close()


# --- Select / display ---

@app.post("/api/select")
async def select_art(body: dict):
    content_id = body.get("content_id")
    if not content_id:
        raise HTTPException(400, "content_id required")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.select_image(content_id, show=True)
        return {"ok": True, "content_id": content_id}
    finally:
        tv.close()


# --- Upload ---

@app.post("/api/upload")
async def upload_art(
    file: UploadFile = File(...),
    matte: str = Form("shadowbox_polar"),
):
    data = await file.read()
    ext = Path(file.filename or "image.jpg").suffix.lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ("jpg", "png"):
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()
        ext = "jpg"

    tv = get_tv()
    art = art_connection(tv)
    try:
        content_id = art.upload(data, matte=matte, file_type=ext)
        return {"ok": True, "content_id": content_id}
    finally:
        tv.close()


# --- Delete ---

@app.post("/api/delete")
async def delete_art(body: dict):
    content_ids = body.get("content_ids", [])
    if not content_ids:
        raise HTTPException(400, "content_ids required")
    tv = get_tv()
    art = art_connection(tv)
    try:
        ok = art.delete_list(content_ids)
        return {"ok": ok}
    finally:
        tv.close()


# --- Matte ---

@app.get("/api/mattes")
async def list_mattes():
    tv = get_tv()
    art = art_connection(tv)
    try:
        return art.get_matte_list()
    finally:
        tv.close()


@app.post("/api/matte")
async def change_matte(body: dict):
    content_id = body.get("content_id")
    matte_id = body.get("matte_id", "none")
    if not content_id:
        raise HTTPException(400, "content_id required")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.change_matte(content_id, matte_id)
        return {"ok": True}
    finally:
        tv.close()


# --- Favourite ---

@app.post("/api/favourite")
async def toggle_favourite(body: dict):
    content_id = body.get("content_id")
    status = body.get("status", "on")
    if not content_id:
        raise HTTPException(400, "content_id required")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.set_favourite(content_id, status)
        return {"ok": True}
    finally:
        tv.close()


# --- Art mode toggle ---

@app.post("/api/artmode")
async def set_artmode(body: dict):
    mode = body.get("mode")
    if mode is None:
        raise HTTPException(400, "mode required (true/false)")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.set_artmode(mode)
        return {"ok": True, "mode": mode}
    finally:
        tv.close()


# --- Brightness / color temp ---

@app.get("/api/settings")
async def get_settings():
    tv = get_tv()
    art = art_connection(tv)
    try:
        brightness = art.get_brightness()
        color_temp = art.get_color_temperature()
        slideshow = None
        try:
            slideshow = art.get_slideshow_status()
        except Exception:
            pass
        return {
            "brightness": brightness,
            "color_temperature": color_temp,
            "slideshow": slideshow,
        }
    finally:
        tv.close()


@app.post("/api/brightness")
async def set_brightness(body: dict):
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "value required")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.set_brightness(value)
        return {"ok": True}
    finally:
        tv.close()


@app.post("/api/color_temperature")
async def set_color_temperature(body: dict):
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "value required")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.set_color_temperature(value)
        return {"ok": True}
    finally:
        tv.close()


# --- Photo filters ---

@app.get("/api/filters")
async def get_filters():
    tv = get_tv()
    art = art_connection(tv)
    try:
        return {"filters": art.get_photo_filter_list()}
    finally:
        tv.close()


@app.post("/api/filter")
async def set_filter(body: dict):
    content_id = body.get("content_id")
    filter_id = body.get("filter_id")
    if not content_id or not filter_id:
        raise HTTPException(400, "content_id and filter_id required")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.set_photo_filter(content_id, filter_id)
        return {"ok": True}
    finally:
        tv.close()


# --- Slideshow ---

@app.post("/api/slideshow")
async def set_slideshow(body: dict):
    duration = body.get("duration", 0)
    shuffle = body.get("shuffle", True)
    category_id = body.get("category_id")
    tv = get_tv()
    art = art_connection(tv)
    try:
        art.set_slideshow_status(
            duration=duration,
            type=shuffle,
            category_id=category_id,
        )
        return {"ok": True}
    finally:
        tv.close()


def main():
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
