from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image
from samsungtvws import SamsungTVWS

TV_IP = "192.168.1.36"
TV_PORT = 8001
TV_TIMEOUT = 15
TOKEN_FILE = Path(__file__).parent / ".tv-token"

CACHE_DIR = Path(__file__).parent / ".cache"
THUMB_DIR = CACHE_DIR / "thumbnails"
COLLECTIONS_FILE = Path(__file__).parent / "collections.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("frame-art-manager")

_art_cache: list[dict] | None = None
_current_id_cache: str | None = None
_tv_lock = asyncio.Lock()


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


def _save_thumbnail(content_id: str, data: bytes | bytearray) -> None:
    path = THUMB_DIR / f"{content_id}.jpg"
    path.write_bytes(bytes(data))


def _get_cached_thumbnail(content_id: str) -> bytes | None:
    path = THUMB_DIR / f"{content_id}.jpg"
    if path.exists():
        return path.read_bytes()
    return None


def _cached_content_ids() -> set[str]:
    return {p.stem for p in THUMB_DIR.glob("*.jpg")}


def _refresh_art_cache_sync(force: bool = False) -> dict:
    global _art_cache, _current_id_cache

    if not force and _art_cache is not None:
        return {"items": _art_cache, "current_id": _current_id_cache}

    old_ids = {item["content_id"] for item in (_art_cache or [])}
    cached_on_disk = _cached_content_ids()

    try:
        tv = get_tv()
        art = art_connection(tv)
        try:
            items = art.available()
            current = art.get_current()
            current_id = current.get("content_id") if isinstance(current, dict) else None

            new_ids_set = set()
            for item in items:
                cid = item["content_id"]
                if cid not in old_ids and cid not in cached_on_disk:
                    new_ids_set.add(cid)

            if new_ids_set:
                new_ids_list = list(new_ids_set)
                BATCH = 10
                for i in range(0, len(new_ids_list), BATCH):
                    batch = new_ids_list[i : i + BATCH]
                    try:
                        result = art.get_thumbnail_list(batch)
                        for name, thumb_data in result.items():
                            for cid in batch:
                                if name.startswith(cid):
                                    _save_thumbnail(cid, thumb_data)
                                    break
                    except Exception:
                        for cid in batch:
                            try:
                                thumb_data = art.get_thumbnail(cid)
                                if thumb_data:
                                    _save_thumbnail(cid, thumb_data)
                            except Exception:
                                pass

            current_ids = {item["content_id"] for item in items}
            removed_ids = old_ids - current_ids
            for cid in removed_ids:
                path = THUMB_DIR / f"{cid}.jpg"
                path.unlink(missing_ok=True)

            _art_cache = items
            _current_id_cache = current_id
            log.info(
                "Art cache refreshed: %d items, %d new, %d removed",
                len(items), len(new_ids_set), len(removed_ids),
            )
            return {
                "items": items,
                "current_id": current_id,
                "new_ids": list(new_ids_set),
                "removed_ids": list(removed_ids),
            }
        finally:
            tv.close()
    except Exception as e:
        if _art_cache is not None:
            log.warning("TV unreachable, serving stale cache: %s", e)
            return {
                "items": _art_cache,
                "current_id": _current_id_cache,
                "stale": True,
            }
        raise HTTPException(502, f"Cannot reach TV: {e}")


def _invalidate_art_cache() -> None:
    global _art_cache, _current_id_cache
    _art_cache = None
    _current_id_cache = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    CACHE_DIR.mkdir(exist_ok=True)
    THUMB_DIR.mkdir(exist_ok=True)
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


# --- Art listing (cached) ---

@app.get("/api/art")
async def list_art():
    async with _tv_lock:
        return await asyncio.to_thread(_refresh_art_cache_sync, False)


@app.post("/api/art/refresh")
async def refresh_art():
    async with _tv_lock:
        return await asyncio.to_thread(_refresh_art_cache_sync, True)


# --- Thumbnails (disk-cached) ---

@app.get("/api/thumbnail/{content_id}")
async def get_thumbnail(content_id: str):
    cached = _get_cached_thumbnail(content_id)
    if cached:
        return Response(content=cached, media_type="image/jpeg")
    async with _tv_lock:
        tv = get_tv()
        art = art_connection(tv)
        try:
            data = art.get_thumbnail(content_id)
            if not data:
                raise HTTPException(404, "No thumbnail")
            _save_thumbnail(content_id, data)
            return Response(content=bytes(data), media_type="image/jpeg")
        finally:
            tv.close()


@app.post("/api/thumbnails")
async def get_thumbnails_batch(body: dict):
    content_ids = body.get("content_ids", [])
    if not content_ids:
        return {"thumbnails": {}}

    encoded = {}
    missing = []
    for cid in content_ids:
        cached = _get_cached_thumbnail(cid)
        if cached:
            encoded[cid] = base64.b64encode(cached).decode()
        else:
            missing.append(cid)

    if missing:
        async with _tv_lock:
            tv = get_tv()
            art = art_connection(tv)
            try:
                result = art.get_thumbnail_list(missing)
                for name, data in result.items():
                    for cid in missing:
                        if name.startswith(cid):
                            _save_thumbnail(cid, data)
                            encoded[cid] = base64.b64encode(bytes(data)).decode()
                            break
            finally:
                tv.close()

    return {"thumbnails": encoded}


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
        global _current_id_cache
        _current_id_cache = content_id
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
        _invalidate_art_cache()
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
        for cid in content_ids:
            (THUMB_DIR / f"{cid}.jpg").unlink(missing_ok=True)
        _invalidate_art_cache()
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


# --- Collections ---

def _load_collections() -> dict:
    if COLLECTIONS_FILE.exists():
        return json.loads(COLLECTIONS_FILE.read_text())
    return {"collections": []}


def _save_collections(data: dict) -> None:
    COLLECTIONS_FILE.write_text(json.dumps(data, indent=2))


@app.get("/api/collections")
async def list_collections():
    return _load_collections()


@app.post("/api/collections")
async def create_collection(body: dict):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name required")
    data = _load_collections()
    collection = {
        "id": str(uuid.uuid4()),
        "name": name,
        "content_ids": [],
        "created": datetime.now(timezone.utc).isoformat(),
    }
    data["collections"].append(collection)
    _save_collections(data)
    return collection


@app.put("/api/collections/{collection_id}")
async def rename_collection(collection_id: str, body: dict):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name required")
    data = _load_collections()
    for c in data["collections"]:
        if c["id"] == collection_id:
            c["name"] = name
            _save_collections(data)
            return c
    raise HTTPException(404, "Collection not found")


@app.delete("/api/collections/{collection_id}")
async def delete_collection(collection_id: str):
    data = _load_collections()
    before = len(data["collections"])
    data["collections"] = [c for c in data["collections"] if c["id"] != collection_id]
    if len(data["collections"]) == before:
        raise HTTPException(404, "Collection not found")
    _save_collections(data)
    return {"ok": True}


@app.post("/api/collections/{collection_id}/items")
async def add_to_collection(collection_id: str, body: dict):
    content_ids = body.get("content_ids", [])
    if not content_ids:
        raise HTTPException(400, "content_ids required")
    data = _load_collections()
    for c in data["collections"]:
        if c["id"] == collection_id:
            existing = set(c["content_ids"])
            for cid in content_ids:
                if cid not in existing:
                    c["content_ids"].append(cid)
            _save_collections(data)
            return c
    raise HTTPException(404, "Collection not found")


@app.delete("/api/collections/{collection_id}/items")
async def remove_from_collection(collection_id: str, body: dict):
    content_ids = body.get("content_ids", [])
    if not content_ids:
        raise HTTPException(400, "content_ids required")
    to_remove = set(content_ids)
    data = _load_collections()
    for c in data["collections"]:
        if c["id"] == collection_id:
            c["content_ids"] = [cid for cid in c["content_ids"] if cid not in to_remove]
            _save_collections(data)
            return c
    raise HTTPException(404, "Collection not found")


def main():
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
