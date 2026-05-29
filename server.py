from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles
from PIL import Image
from samsungtvws import SamsungTVWS

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TV_IP = os.environ.get("DOCENT_TV_IP", "")
TV_PORT = int(os.environ.get("DOCENT_TV_PORT", "8001"))
TV_TIMEOUT = int(os.environ.get("DOCENT_TV_TIMEOUT", "15"))
TOKEN_FILE = Path(__file__).parent / ".tv-token"

CACHE_DIR = Path(__file__).parent / ".cache"
THUMB_DIR = CACHE_DIR / "thumbnails"
ORIGINALS_DIR = CACHE_DIR / "originals"
COLLECTIONS_FILE = Path(__file__).parent / "collections.json"
ARTWORK_META_FILE = Path(__file__).parent / "artwork_meta.json"
AI_CONFIG_FILE = Path(__file__).parent / "ai_config.json"
API_USAGE_FILE = Path(__file__).parent / "api_usage.json"
DRIVE_SYNC_FILE = Path(__file__).parent / "drive_sync.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("docent")

_art_cache: list[dict] | None = None
_current_id_cache: str | None = None
_tv_lock = asyncio.Lock()
_meta_lock = asyncio.Lock()
_collections_lock = asyncio.Lock()
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
_nws_station_cache: dict[str, str] = {}


class WeatherUnavailable(Exception):
    pass


def get_tv() -> SamsungTVWS:
    return SamsungTVWS(
        TV_IP,
        port=TV_PORT,
        timeout=TV_TIMEOUT,
        token_file=str(TOKEN_FILE),
    )


def art_connection(tv: SamsungTVWS):
    art = tv.art()
    try:
        art.open()
    except Exception:
        tv.close()
        raise
    return art


def _save_thumbnail(content_id: str, data: bytes | bytearray) -> None:
    path = THUMB_DIR / f"{content_id}.jpg"
    path.write_bytes(bytes(data))


def _get_cached_thumbnail(content_id: str) -> bytes | None:
    path = THUMB_DIR / f"{content_id}.jpg"
    if path.exists():
        return path.read_bytes()
    return None


_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _validate_content_id(cid: str) -> str:
    if not cid or not _CONTENT_ID_RE.match(cid):
        raise HTTPException(400, "Invalid content_id format")
    return cid


def _validate_content_ids(cids: list) -> list[str]:
    return [_validate_content_id(cid) for cid in cids]


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_NAME_LENGTH = 200


def _validate_name(name: str, field: str = "name") -> str:
    name = name.strip()
    if not name:
        raise HTTPException(400, f"{field} cannot be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise HTTPException(400, f"{field} must be {MAX_NAME_LENGTH} characters or fewer")
    if _CONTROL_CHAR_RE.search(name):
        raise HTTPException(400, f"{field} contains invalid characters")
    return name


def _cached_content_ids() -> set[str]:
    return {p.stem for p in THUMB_DIR.glob("*.jpg")}


def _fetch_thumbnails_sync(content_ids: list[str]) -> None:
    """Fetch thumbnails from TV for the given IDs (best-effort, no errors raised)."""
    if not content_ids:
        return
    try:
        tv = get_tv()
        art = art_connection(tv)
        try:
            BATCH = 10
            for i in range(0, len(content_ids), BATCH):
                batch = content_ids[i : i + BATCH]
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
        finally:
            tv.close()
    except Exception as e:
        log.warning("Thumbnail pre-fetch failed: %s", e)


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
        raise HTTPException(502, "Cannot reach TV — is it on and connected?")


def _invalidate_art_cache() -> None:
    global _art_cache, _current_id_cache
    _art_cache = None
    _current_id_cache = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    CACHE_DIR.mkdir(exist_ok=True)
    THUMB_DIR.mkdir(exist_ok=True)
    ORIGINALS_DIR.mkdir(exist_ok=True)
    if not TV_IP:
        log.warning("DOCENT_TV_IP is not set — copy .env.example to .env and add your TV's IP address")
    else:
        log.info("Docent starting — TV at %s:%s", TV_IP, TV_PORT)
    yield


app = FastAPI(title="Docent", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "assets"), name="assets")


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
        log.warning("TV connection failed: %s", e)
        raise HTTPException(502, "Cannot reach TV — is it on and connected?")


@app.get("/api/device-info")
async def device_info():
    try:
        tv = get_tv()
        art = art_connection(tv)
        try:
            info = art.get_device_info()
            return {"device_info": info}
        finally:
            tv.close()
    except Exception as e:
        log.warning("TV connection failed: %s", e)
        raise HTTPException(502, "Cannot reach TV — is it on and connected?")


# --- Art listing (cached) ---

@app.get("/api/art")
async def list_art():
    async with _tv_lock:
        result = await asyncio.to_thread(_refresh_art_cache_sync, False)
    # Pre-fetch new thumbnails in background (outside lock)
    new_ids = result.get("new_ids", [])
    if new_ids:
        asyncio.get_event_loop().run_in_executor(None, _fetch_thumbnails_sync, new_ids)
    return result


@app.post("/api/art/refresh")
async def refresh_art():
    async with _tv_lock:
        result = await asyncio.to_thread(_refresh_art_cache_sync, True)
    new_ids = result.get("new_ids", [])
    if new_ids:
        asyncio.get_event_loop().run_in_executor(None, _fetch_thumbnails_sync, new_ids)
    return result


# --- Thumbnails (disk-cached) ---

@app.get("/api/thumbnail/{content_id}")
async def get_thumbnail(content_id: str):
    _validate_content_id(content_id)
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
    _validate_content_ids(content_ids)

    encoded = {}
    missing = []
    for cid in content_ids:
        cached = _get_cached_thumbnail(cid)
        if cached:
            encoded[cid] = base64.b64encode(cached).decode()
        else:
            missing.append(cid)

    if missing:
        acquired = _tv_lock.locked()
        if not acquired:
            try:
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
            except Exception as e:
                log.warning("Batch thumbnail fetch failed: %s", e)

    return {"thumbnails": encoded, "missing": [c for c in missing if c not in encoded]}


# --- Select / display ---

@app.post("/api/select")
async def select_art(body: dict):
    content_id = body.get("content_id")
    if not content_id:
        raise HTTPException(400, "content_id required")
    _validate_content_id(content_id)
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
    filename: str = Form(""),
    analyze: bool = Query(False),
):
    chunks = []
    total = 0
    async for chunk in file:
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    ext = Path(file.filename or "image.jpg").suffix.lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"

    img = Image.open(io.BytesIO(data))
    w, h = img.size

    if ext not in ("jpg", "png"):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()
        ext = "jpg"

    if w > 3840 or h > 2160:
        img.thumbnail((3840, 2160), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        data = buf.getvalue()
        w, h = img.size
        ext = "jpg"

    original_name = filename.strip() or Path(file.filename or "image.jpg").stem

    tv = get_tv()
    art = art_connection(tv)
    try:
        log.info("Uploading %s (%dx%d, ext=%s, matte=%s)", original_name, w, h, ext, matte)
        content_id = art.upload(data, matte=matte, file_type=ext)
        _invalidate_art_cache()

        analysis_img = img.copy()
        analysis_img.thumbnail((1280, 720), Image.LANCZOS)
        buf = io.BytesIO()
        analysis_img.save(buf, format="JPEG", quality=80)
        (ORIGINALS_DIR / f"{content_id}.jpg").write_bytes(buf.getvalue())

        async with _meta_lock:
            meta = _load_artwork_meta()
            meta["artwork"][content_id] = {
                "title": original_name,
                "original_filename": file.filename or "",
                "width": w,
                "height": h,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_artwork_meta(meta)

        ai_result = None
        ai_config = _load_ai_config()
        provider = ai_config.get("provider", "claude")
        has_key = bool(ai_config.get(provider, {}).get("api_key"))
        should_analyze = ai_config.get("auto_analyze") and has_key

        if analyze and should_analyze:
            if not (THUMB_DIR / f"{content_id}.jpg").exists():
                try:
                    thumb_data = art.get_thumbnail(content_id)
                    if thumb_data:
                        _save_thumbnail(content_id, thumb_data)
                except Exception:
                    pass
            try:
                ai_result = await _analyze_artwork(content_id)
            except Exception as e:
                ai_result = {"error": str(e)}
        elif should_analyze:
            asyncio.create_task(_analyze_artwork_background(content_id))

        resp = {"ok": True, "content_id": content_id, "title": original_name, "width": w, "height": h}
        if ai_result:
            if "error" in ai_result:
                resp["ai_error"] = ai_result["error"]
            else:
                resp["ai_meta"] = ai_result["ai_meta"]
                resp["title"] = ai_result.get("title", original_name)
        return resp
    finally:
        tv.close()


# --- Delete ---

@app.post("/api/delete")
async def delete_art(body: dict):
    content_ids = body.get("content_ids", [])
    if not content_ids:
        raise HTTPException(400, "content_ids required")
    _validate_content_ids(content_ids)
    tv = get_tv()
    art = art_connection(tv)
    try:
        ok = art.delete_list(content_ids)
        for cid in content_ids:
            (THUMB_DIR / f"{cid}.jpg").unlink(missing_ok=True)
        _invalidate_art_cache()
        async with _meta_lock:
            meta = _load_artwork_meta()
            for cid in content_ids:
                meta["artwork"].pop(cid, None)
            _save_artwork_meta(meta)
        return {"ok": ok}
    finally:
        tv.close()


# --- Matte ---

@app.get("/api/mattes")
async def list_mattes():
    try:
        tv = get_tv()
    except Exception:
        raise HTTPException(502, "Cannot reach TV")
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
    _validate_content_id(content_id)
    try:
        tv = get_tv()
    except Exception:
        raise HTTPException(502, "Cannot reach TV")
    art = art_connection(tv)
    try:
        log.info("Changing matte: content_id=%s, matte_id=%s", content_id, matte_id)
        art.change_matte(content_id, matte_id)
        return {"ok": True}
    except Exception as e:
        err = str(e)
        if "error number" in err:
            raise HTTPException(422, f"TV rejected matte change — this image may not support that matte style")
        raise
    finally:
        tv.close()


# --- Favourite ---

@app.post("/api/favourite")
async def toggle_favourite(body: dict):
    content_id = body.get("content_id")
    status = body.get("status", "on")
    if not content_id:
        raise HTTPException(400, "content_id required")
    _validate_content_id(content_id)
    try:
        tv = get_tv()
    except Exception:
        raise HTTPException(502, "Cannot reach TV")
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
    try:
        tv = get_tv()
    except Exception:
        raise HTTPException(502, "Cannot reach TV")
    art = art_connection(tv)
    try:
        art.set_artmode(mode)
        return {"ok": True, "mode": mode}
    finally:
        tv.close()


# --- Photo filters ---

@app.get("/api/filters")
async def get_filters():
    try:
        tv = get_tv()
    except Exception:
        raise HTTPException(502, "Cannot reach TV")
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
    _validate_content_id(content_id)
    try:
        tv = get_tv()
    except Exception:
        raise HTTPException(502, "Cannot reach TV")
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
    try:
        tv = get_tv()
    except Exception:
        raise HTTPException(502, "Cannot reach TV")
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


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically: write to temp file, then rename into place."""
    content = json.dumps(data, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_collections(data: dict) -> None:
    _atomic_write_json(COLLECTIONS_FILE, data)


@app.get("/api/collections")
async def list_collections():
    return _load_collections()


@app.post("/api/collections")
async def create_collection(body: dict):
    name = _validate_name(body.get("name", ""))
    async with _collections_lock:
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
    name = _validate_name(body.get("name", ""))
    async with _collections_lock:
        data = _load_collections()
        for c in data["collections"]:
            if c["id"] == collection_id:
                c["name"] = name
                _save_collections(data)
                return c
    raise HTTPException(404, "Collection not found")


@app.delete("/api/collections/{collection_id}")
async def delete_collection(collection_id: str):
    async with _collections_lock:
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
    _validate_content_ids(content_ids)
    async with _collections_lock:
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
    _validate_content_ids(content_ids)
    to_remove = set(content_ids)
    async with _collections_lock:
        data = _load_collections()
        for c in data["collections"]:
            if c["id"] == collection_id:
                c["content_ids"] = [cid for cid in c["content_ids"] if cid not in to_remove]
                _save_collections(data)
                return c
    raise HTTPException(404, "Collection not found")


# --- Artwork metadata ---

def _load_artwork_meta() -> dict:
    if ARTWORK_META_FILE.exists():
        return json.loads(ARTWORK_META_FILE.read_text())
    return {"artwork": {}}


def _save_artwork_meta(data: dict) -> None:
    _atomic_write_json(ARTWORK_META_FILE, data)


@app.get("/api/artwork-meta")
async def get_artwork_meta():
    return _load_artwork_meta()


@app.put("/api/artwork-meta/{content_id}")
async def update_artwork_meta(content_id: str, body: dict):
    _validate_content_id(content_id)
    async with _meta_lock:
        data = _load_artwork_meta()
        if content_id not in data["artwork"]:
            data["artwork"][content_id] = {}
        for key, value in body.items():
            if key == "title":
                if isinstance(value, str):
                    value = value.strip()
                    if not value:
                        continue
                    value = _validate_name(value, "title")
            data["artwork"][content_id][key] = value
        _save_artwork_meta(data)
    return {"ok": True, "content_id": content_id, "meta": data["artwork"][content_id]}


# --- AI config ---

def _load_ai_config() -> dict:
    defaults = {"provider": "claude", "auto_analyze": False, "opus_fallback": False, "use_google_vision": True, "claude": {"api_key": "", "model": "claude-sonnet-4-20250514"}, "openai": {"api_key": "", "model": "gpt-4.1"}, "google_vision": {"api_key": ""}}
    if AI_CONFIG_FILE.exists():
        saved = json.loads(AI_CONFIG_FILE.read_text())
        for k, v in defaults.items():
            if k not in saved:
                saved[k] = v
            elif isinstance(v, dict):
                for dk, dv in v.items():
                    saved[k].setdefault(dk, dv)
        return saved
    return defaults


def _save_ai_config(data: dict) -> None:
    _atomic_write_json(AI_CONFIG_FILE, data)


@app.get("/api/ai/config")
async def get_ai_config():
    config = _load_ai_config()
    masked = dict(config)
    if masked.get("claude", {}).get("api_key"):
        key = masked["claude"]["api_key"]
        masked["claude"] = dict(masked["claude"])
        masked["claude"]["api_key"] = "..." + key[-4:] if len(key) > 4 else "****"
    if masked.get("openai", {}).get("api_key"):
        key = masked["openai"]["api_key"]
        masked["openai"] = dict(masked["openai"])
        masked["openai"]["api_key"] = "..." + key[-4:] if len(key) > 4 else "****"
    if masked.get("google_vision", {}).get("api_key"):
        key = masked["google_vision"]["api_key"]
        masked["google_vision"] = dict(masked["google_vision"])
        masked["google_vision"]["api_key"] = "..." + key[-4:] if len(key) > 4 else "****"
    return masked


@app.put("/api/ai/config")
async def update_ai_config(body: dict):
    config = _load_ai_config()
    if "auto_analyze" in body:
        config["auto_analyze"] = bool(body["auto_analyze"])
    if "opus_fallback" in body:
        config["opus_fallback"] = bool(body["opus_fallback"])
    if "provider" in body:
        config["provider"] = body["provider"]
    if "use_google_vision" in body:
        config["use_google_vision"] = bool(body["use_google_vision"])
    if "claude" in body:
        claude = body["claude"]
        if "api_key" in claude and claude["api_key"] and not claude["api_key"].startswith("..."):
            config.setdefault("claude", {})["api_key"] = claude["api_key"]
        if "model" in claude:
            config.setdefault("claude", {})["model"] = claude["model"]
    if "openai" in body:
        openai_conf = body["openai"]
        if "api_key" in openai_conf and openai_conf["api_key"] and not openai_conf["api_key"].startswith("..."):
            config.setdefault("openai", {})["api_key"] = openai_conf["api_key"]
        if "model" in openai_conf:
            config.setdefault("openai", {})["model"] = openai_conf["model"]
    if "google_vision" in body:
        gv = body["google_vision"]
        if "api_key" in gv and gv["api_key"] and not gv["api_key"].startswith("..."):
            config.setdefault("google_vision", {})["api_key"] = gv["api_key"]
    _save_ai_config(config)
    return {"ok": True}


# --- API usage tracking ---

MODEL_PRICING = {
    "claude-sonnet-4-20250514": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
    "claude-haiku-4-5-20251001": {"input_per_mtok": 0.80, "output_per_mtok": 4.0},
    "claude-opus-4-20250514": {"input_per_mtok": 15.0, "output_per_mtok": 75.0},
    "gpt-4.1": {"input_per_mtok": 2.0, "output_per_mtok": 8.0},
    "gpt-4.1-mini": {"input_per_mtok": 0.40, "output_per_mtok": 1.60},
    "gpt-4.1-nano": {"input_per_mtok": 0.10, "output_per_mtok": 0.40},
    "gpt-4o": {"input_per_mtok": 2.50, "output_per_mtok": 10.0},
}


def _load_api_usage() -> dict:
    if API_USAGE_FILE.exists():
        return json.loads(API_USAGE_FILE.read_text())
    return {"monthly": {}}


def _save_api_usage(data: dict) -> None:
    _atomic_write_json(API_USAGE_FILE, data)


def _record_api_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    data = _load_api_usage()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    m = data["monthly"].setdefault(month, {"input_tokens": 0, "output_tokens": 0, "calls": 0, "by_model": {}})
    m["input_tokens"] += input_tokens
    m["output_tokens"] += output_tokens
    m["calls"] += 1
    bm = m["by_model"].setdefault(model, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
    bm["input_tokens"] += input_tokens
    bm["output_tokens"] += output_tokens
    bm["calls"] += 1
    _save_api_usage(data)


@app.get("/api/ai/usage")
async def get_api_usage():
    data = _load_api_usage()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    m = data.get("monthly", {}).get(month, {})
    cost = 0.0
    for model_id, stats in m.get("by_model", {}).items():
        pricing = MODEL_PRICING.get(model_id)
        if pricing:
            cost += stats["input_tokens"] / 1_000_000 * pricing["input_per_mtok"]
            cost += stats["output_tokens"] / 1_000_000 * pricing["output_per_mtok"]
    return {
        "month": month,
        "input_tokens": m.get("input_tokens", 0),
        "output_tokens": m.get("output_tokens", 0),
        "calls": m.get("calls", 0),
        "estimated_cost_usd": round(cost, 4),
    }


# --- AI analysis ---

AI_SYSTEM = (
    "You are an expert art historian and curator with deep knowledge of Western "
    "and Eastern art traditions, from Old Masters through contemporary. You "
    "specialize in identifying artworks from reproductions, including "
    "low-resolution images. You reason carefully about visual evidence before "
    "concluding."
)

_HASH_FILENAME_RE = re.compile(r"^full_landscape_[0-9a-f]+$")


def _build_analysis_prompt(image_source: str, content_id: str,
                           vision_title: str = "", vision_artist: str = "") -> str:
    parts = []
    if "original" in image_source:
        parts.append("You are viewing a high-resolution image.")
    else:
        parts.append(
            "You are viewing a small thumbnail (~320x180 pixels). "
            "Fine details may not be visible."
        )

    meta = _load_artwork_meta()
    original_fn = meta.get("artwork", {}).get(content_id, {}).get("original_filename", "")
    stem = Path(original_fn).stem if original_fn else ""
    if stem and not _HASH_FILENAME_RE.match(stem):
        parts.append(
            f"\nThe file was uploaded as: '{original_fn}'. This may contain "
            "artist/title information — use as a hint but verify against "
            "visual evidence."
        )

    if vision_title or vision_artist:
        hint = "Google reverse-image search identified this artwork as"
        if vision_title and vision_artist:
            hint += f": \"{vision_title}\" by {vision_artist}."
        elif vision_title:
            hint += f": \"{vision_title}\" (artist unidentified)."
        else:
            hint += f" possibly by {vision_artist} (title unidentified)."
        hint += " Use this as strong context but verify against visual evidence."
        parts.append(f"\n{hint}")

    parts.append("""
Study the image carefully. Before answering, reason through:
1. Visual style and technique (brushwork, color palette, composition)
2. Subject matter and iconography
3. Period indicators (clothing, architecture, lighting style)
4. Any signatures, text, or distinctive marks visible

Then provide metadata in JSON:
{
  "title": "The artwork's actual title, or 'Untitled' if unidentifiable",
  "artist": "Full name, or 'Unknown' if unidentifiable",
  "year": "Year or period, or 'Unknown'",
  "medium": "e.g., 'Oil on canvas'",
  "school": "e.g., 'Impressionism', or 'N/A'",
  "vibes": ["adj1", "adj2", "adj3", "adj4", "adj5"],
  "description": "One-sentence description",
  "confidence": "high, medium, or low"
}

Provide exactly 5 vibe adjectives. End your response with ONLY the JSON block.""")

    return "\n".join(parts)


def _parse_ai_response(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


async def _analyze_artwork(content_id: str) -> dict:
    config = _load_ai_config()

    orig_path = ORIGINALS_DIR / f"{content_id}.jpg"
    thumb_path = THUMB_DIR / f"{content_id}.jpg"
    if orig_path.exists():
        image_path = orig_path
        image_source = "high-resolution original"
    elif thumb_path.exists():
        image_path = thumb_path
        image_source = "low-resolution thumbnail"
    else:
        raise HTTPException(404, f"No image for {content_id}")

    image_data = base64.b64encode(image_path.read_bytes()).decode()

    vision_title = ""
    vision_artist = ""
    vision_used = False
    gv_key = config.get("google_vision", {}).get("api_key", "")
    if config.get("use_google_vision") and gv_key:
        try:
            web = await _call_google_vision(gv_key, image_data)
            if web:
                vision_title, vision_artist, score = _extract_identification(web)
                vision_used = bool(vision_title or vision_artist)
                if vision_used:
                    log.info("Vision identified %s: '%s' by '%s' (score %.2f)",
                             content_id, vision_title, vision_artist, score)
        except Exception as e:
            log.warning("Google Vision failed for %s: %s", content_id, e)

    provider = config.get("provider", "claude")
    provider_config = config.get(provider, {})
    api_key = provider_config.get("api_key", "")
    model = provider_config.get("model", "")

    if not api_key:
        raise HTTPException(400, f"{provider.title()} API key not configured")

    user_prompt = _build_analysis_prompt(
        image_source, content_id,
        vision_title=vision_title, vision_artist=vision_artist,
    )

    if provider == "openai":
        parsed = await _call_openai_vision(api_key, model, image_data, user_prompt)
    else:
        parsed = await _call_claude_vision(api_key, model, image_data, user_prompt)

    ai_title = parsed.get("title", "") or "Unknown"
    artist = parsed.get("artist", "Unknown")
    confidence = parsed.get("confidence", "medium")

    opus_fallback = config.get("opus_fallback", False)
    if (
        confidence == "low"
        and opus_fallback
        and provider == "claude"
        and "opus" not in model
    ):
        first_pass = parsed
        opus_model = "claude-opus-4-20250514"
        escalation = (
            f"A preliminary analysis identified this as: "
            f"school={first_pass.get('school', 'N/A')}, "
            f"year={first_pass.get('year', 'Unknown')}, "
            f"description={first_pass.get('description', '')}.\n"
            f"Please examine more carefully and attempt to identify "
            f"the specific artist and painting.\n\n{user_prompt}"
        )
        try:
            opus_parsed = await _call_claude_vision(
                api_key, opus_model, image_data, escalation
            )
            opus_artist = opus_parsed.get("artist", "Unknown")
            if opus_artist != "Unknown":
                parsed = opus_parsed
                model = opus_model
                ai_title = parsed.get("title", "") or "Unknown"
                artist = opus_artist
                confidence = parsed.get("confidence", "medium")
                log.info("Opus escalation improved result for %s", content_id)
        except Exception as e:
            log.warning("Opus fallback failed for %s: %s", content_id, e)

    ai_meta = {
        "title": ai_title,
        "artist": artist,
        "year": parsed.get("year", "Unknown"),
        "medium": parsed.get("medium", "Unknown"),
        "school": parsed.get("school", "N/A"),
        "vibes": parsed.get("vibes", [])[:5],
        "description": parsed.get("description", ""),
        "confidence": confidence,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "vision_identified": vision_used,
    }

    async with _meta_lock:
        meta = _load_artwork_meta()
        if content_id not in meta["artwork"]:
            meta["artwork"][content_id] = {}
        meta["artwork"][content_id]["ai_meta"] = ai_meta
        meta["artwork"][content_id].pop("ai_failed", None)
        meta["artwork"][content_id].pop("ai_error", None)

        current_title = meta["artwork"][content_id].get("title", "")
        if ai_title not in ("Untitled", "Unknown", ""):
            display = ai_title
            if artist and artist != "Unknown":
                display = f"{ai_title} - {artist}"
            meta["artwork"][content_id]["title"] = display
        elif not current_title:
            meta["artwork"][content_id]["title"] = ai_title
        _save_artwork_meta(meta)
        saved_title = meta["artwork"][content_id].get("title")

    return {"ai_meta": ai_meta, "title": saved_title}


async def _call_claude_vision(
    api_key: str, model: str, image_b64: str, prompt: str
) -> dict:
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": AI_SYSTEM,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            },
        )
        if resp.status_code != 200:
            log.warning("Claude API error: %s — %s", resp.status_code, resp.text[:200])
            raise HTTPException(502, "AI analysis failed — check your API key and try again")

        result = resp.json()
        usage = result.get("usage", {})
        _record_api_usage(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        text = result.get("content", [{}])[0].get("text", "")
        parsed = _parse_ai_response(text)
        if not parsed:
            raise HTTPException(502, "Could not parse AI response")
        return parsed


async def _call_openai_vision(
    api_key: str, model: str, image_b64: str, prompt: str
) -> dict:
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": AI_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}",
                            "detail": "high",
                        }},
                        {"type": "text", "text": prompt},
                    ]},
                ],
            },
        )
        if resp.status_code != 200:
            log.warning("OpenAI API error: %s — %s", resp.status_code, resp.text[:200])
            raise HTTPException(502, "AI analysis failed — check your API key and try again")
        result = resp.json()
        usage = result.get("usage", {})
        _record_api_usage(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        text = result["choices"][0]["message"]["content"]
        parsed = _parse_ai_response(text)
        if not parsed:
            raise HTTPException(502, "Could not parse AI response")
        return parsed


async def _call_google_vision(api_key: str, image_b64: str) -> dict | None:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://vision.googleapis.com/v1/images:annotate",
            headers={"x-goog-api-key": api_key},
            json={
                "requests": [{
                    "image": {"content": image_b64},
                    "features": [{"type": "WEB_DETECTION", "maxResults": 10}],
                }],
            },
        )
        if resp.status_code != 200:
            log.warning("Google Vision API error: %s", resp.status_code)
            return None
        return resp.json().get("responses", [{}])[0].get("webDetection", {})


_MUSEUM_DOMAINS = (
    "wikipedia.org", "artic.edu", "metmuseum.org", "nga.gov",
    "tate.org", "moma.org", "artsy.net", "wikiart.org",
    "nationalgallery.org", "rijksmuseum.nl",
)
_GENERIC_TERMS = frozenset((
    "painting", "art", "oil painting", "poster", "japanese art",
    "artwork", "print", "canvas", "illustration",
))


def _extract_identification(web: dict) -> tuple[str, str, float]:
    title = ""
    artist = ""
    score = 0.0

    labels = web.get("bestGuessLabels", [])
    if labels:
        title = labels[0].get("label", "")

    entities = web.get("webEntities", [])
    for e in entities:
        s = e.get("score", 0)
        if s > score and e.get("description"):
            score = s

    pages = web.get("pagesWithMatchingImages", [])
    for p in pages:
        page_title = p.get("pageTitle", "")
        url = p.get("url", "")
        if not any(d in url for d in _MUSEUM_DOMAINS):
            continue
        if " by " in page_title:
            parts = page_title.split(" by ", 1)
            if not title:
                title = parts[0].strip().split(" | ")[0].split(" - ")[0].strip()
            artist = parts[1].strip().split(" | ")[0].split(" - ")[0].split(",")[0].strip()
            break
        if " - Wikipedia" in page_title:
            wiki_title = page_title.replace(" - Wikipedia", "").strip()
            m = re.match(r"(.+?)\s*\(([^)]+)\)", wiki_title)
            if m:
                if not title:
                    title = m.group(1).strip()
                artist = m.group(2).strip()
                break

    if title and not artist:
        for e in entities:
            desc = e.get("description", "")
            if not desc or e.get("score", 0) < 0.5:
                continue
            if desc.lower() in _GENERIC_TERMS:
                continue
            if any(w in desc.lower() for w in ("museum", "gallery", "institute")):
                continue
            artist = desc
            break

    return title, artist, score


@app.post("/api/ai/analyze/{content_id}")
async def analyze_artwork(content_id: str):
    _validate_content_id(content_id)
    result = await _analyze_artwork(content_id)
    return {"ok": True, "content_id": content_id, "ai_meta": result["ai_meta"], "title": result.get("title")}


@app.post("/api/ai/analyze-batch")
async def analyze_batch(body: dict):
    content_ids = body.get("content_ids", [])
    if content_ids:
        _validate_content_ids(content_ids)
    force = body.get("force", False)
    meta = _load_artwork_meta()

    if not content_ids:
        if force:
            content_ids = [item["content_id"] for item in (_art_cache or [])]
        else:
            content_ids = [
                cid for cid in {item["content_id"] for item in (_art_cache or [])}
                if cid not in {k for k, v in meta.get("artwork", {}).items() if v.get("ai_meta")}
            ]

    analyzed = 0
    skipped = 0
    failed = 0

    for cid in content_ids:
        existing = meta.get("artwork", {}).get(cid, {}).get("ai_meta")
        if existing and not force:
            skipped += 1
            continue
        try:
            result = await _analyze_artwork(cid)
            analyzed += 1
            await asyncio.sleep(1)
        except Exception as e:
            log.warning("AI analysis failed for %s: %s", cid, e)
            failed += 1

    return {"analyzed": analyzed, "skipped": skipped, "failed": failed, "total": len(content_ids)}


@app.get("/api/ai/estimate")
async def estimate_analysis(count: int = Query(...)):
    config = _load_ai_config()
    provider = config.get("provider", "claude")
    model = config.get(provider, {}).get("model", "claude-sonnet-4-20250514")
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["claude-sonnet-4-20250514"])

    avg_input = 1600
    avg_output = 200
    usage = _load_api_usage()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    month_data = usage.get("monthly", {}).get(month, {})
    calls = month_data.get("calls", 0)
    if calls > 0:
        avg_input = month_data["input_tokens"] // calls
        avg_output = month_data["output_tokens"] // calls

    cost_per = (avg_input / 1_000_000 * pricing["input_per_mtok"] +
                avg_output / 1_000_000 * pricing["output_per_mtok"])
    total_cost = cost_per * count
    est_minutes = round(count * 8 / 60, 1)

    return {
        "count": count,
        "estimated_cost_usd": round(total_cost, 2),
        "estimated_minutes": est_minutes,
        "model": model,
    }


async def _analyze_artwork_background(content_id: str):
    for _ in range(10):
        if (THUMB_DIR / f"{content_id}.jpg").exists():
            break
        await asyncio.sleep(2)
    try:
        await _analyze_artwork(content_id)
        log.info("Auto-analyzed %s", content_id)
    except Exception as e:
        log.warning("Auto-analyze failed for %s: %s", content_id, e)
        async with _meta_lock:
            meta = _load_artwork_meta()
            if content_id in meta.get("artwork", {}):
                meta["artwork"][content_id]["ai_failed"] = True
                meta["artwork"][content_id]["ai_error"] = str(e)[:200]
                _save_artwork_meta(meta)


# --- Google Drive sync ---

def _load_drive_sync() -> dict:
    if DRIVE_SYNC_FILE.exists():
        return json.loads(DRIVE_SYNC_FILE.read_text())
    return {"api_key": None, "syncs": []}


def _save_drive_sync(data: dict) -> None:
    _atomic_write_json(DRIVE_SYNC_FILE, data)


def _extract_folder_id(url: str) -> str | None:
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


async def _drive_list_files(folder_id: str, api_key: str) -> list[dict]:
    files = []
    page_token = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "q": f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false",
                "key": api_key,
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = await client.get("https://www.googleapis.com/drive/v3/files", params=params)
            if resp.status_code != 200:
                log.warning("Drive API error: %s — %s", resp.status_code, resp.text[:200])
                raise HTTPException(502, "Google Drive API error — check your API key")
            data = resp.json()
            files.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return files


async def _drive_download_file(file_id: str, api_key: str) -> bytes:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media", "key": api_key},
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"Drive download failed: {resp.status_code}")
        return resp.content


async def _drive_get_folder_name(folder_id: str, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://www.googleapis.com/drive/v3/files/{folder_id}",
            params={"key": api_key, "fields": "name"},
        )
        if resp.status_code != 200:
            return "Unknown Folder"
        return resp.json().get("name", "Unknown Folder")


@app.get("/api/drive/syncs")
async def list_drive_syncs():
    data = _load_drive_sync()
    masked = dict(data)
    if masked.get("api_key"):
        key = masked["api_key"]
        masked["api_key"] = "..." + key[-4:] if len(key) > 4 else "****"
    return masked


@app.post("/api/drive/settings")
async def save_drive_settings(body: dict):
    data = _load_drive_sync()
    if "api_key" in body and body["api_key"] and not body["api_key"].startswith("..."):
        data["api_key"] = body["api_key"]
    _save_drive_sync(data)
    return {"ok": True}


@app.post("/api/drive/syncs")
async def create_drive_sync(body: dict):
    folder_url = body.get("folder_url", "")
    direction = body.get("direction", "import")
    target = body.get("target", "all")

    folder_id = _extract_folder_id(folder_url)
    if not folder_id:
        raise HTTPException(400, "Could not extract folder ID from URL")

    data = _load_drive_sync()
    api_key = data.get("api_key")
    if not api_key:
        raise HTTPException(400, "Google Drive API key not configured")

    folder_name = await _drive_get_folder_name(folder_id, api_key)

    sync = {
        "id": str(uuid.uuid4()),
        "folder_id": folder_id,
        "folder_url": folder_url,
        "folder_name": folder_name,
        "direction": direction,
        "target": target,
        "last_sync_at": None,
        "file_map": {},
    }
    data["syncs"].append(sync)
    _save_drive_sync(data)
    return sync


@app.delete("/api/drive/syncs/{sync_id}")
async def delete_drive_sync(sync_id: str):
    data = _load_drive_sync()
    before = len(data["syncs"])
    data["syncs"] = [s for s in data["syncs"] if s["id"] != sync_id]
    if len(data["syncs"]) == before:
        raise HTTPException(404, "Sync not found")
    _save_drive_sync(data)
    return {"ok": True}


@app.post("/api/drive/syncs/{sync_id}/run")
async def run_drive_sync(sync_id: str):
    data = _load_drive_sync()
    sync = next((s for s in data["syncs"] if s["id"] == sync_id), None)
    if not sync:
        raise HTTPException(404, "Sync not found")

    api_key = data.get("api_key")
    if not api_key:
        raise HTTPException(400, "Google Drive API key not configured")

    direction = sync["direction"]
    file_map = sync.get("file_map", {})
    imported = 0
    skipped = 0
    failed = 0

    if direction in ("import", "both"):
        drive_files = await _drive_list_files(sync["folder_id"], api_key)
        for df in drive_files:
            file_id = df["id"]
            if file_id in file_map:
                skipped += 1
                continue
            try:
                image_data = await _drive_download_file(file_id, api_key)
                img = Image.open(io.BytesIO(image_data))
                w, h = img.size
                ext = Path(df["name"]).suffix.lstrip(".").lower()
                if ext == "jpeg":
                    ext = "jpg"
                if ext not in ("jpg", "png"):
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=95)
                    image_data = buf.getvalue()
                    ext = "jpg"
                if w > 3840 or h > 2160:
                    img.thumbnail((3840, 2160), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=95)
                    image_data = buf.getvalue()
                    w, h = img.size
                    ext = "jpg"

                matte = "shadowbox_polar"
                tv = get_tv()
                art = art_connection(tv)
                try:
                    content_id = art.upload(image_data, matte=matte, file_type=ext)
                finally:
                    tv.close()

                _invalidate_art_cache()

                analysis_img = img.copy()
                analysis_img.thumbnail((1280, 720), Image.LANCZOS)
                buf = io.BytesIO()
                analysis_img.save(buf, format="JPEG", quality=80)
                (ORIGINALS_DIR / f"{content_id}.jpg").write_bytes(buf.getvalue())

                original_name = Path(df["name"]).stem
                async with _meta_lock:
                    meta = _load_artwork_meta()
                    meta["artwork"][content_id] = {
                        "title": original_name,
                        "original_filename": df["name"],
                        "width": w,
                        "height": h,
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        "source": "google_drive",
                        "drive_file_id": file_id,
                        "drive_sync_id": sync_id,
                    }
                    _save_artwork_meta(meta)

                file_map[file_id] = {
                    "content_id": content_id,
                    "name": df["name"],
                    "drive_modified": df.get("modifiedTime", ""),
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                }

                if sync["target"] != "all":
                    async with _collections_lock:
                        coll_data = _load_collections()
                        for c in coll_data["collections"]:
                            if c["id"] == sync["target"] and content_id not in c["content_ids"]:
                                c["content_ids"].append(content_id)
                        _save_collections(coll_data)

                ai_config = _load_ai_config()
                _prov = ai_config.get("provider", "claude")
                if ai_config.get("auto_analyze") and ai_config.get(_prov, {}).get("api_key"):
                    asyncio.create_task(_analyze_artwork_background(content_id))

                imported += 1
                log.info("Imported from Drive: %s → %s", df["name"], content_id)
            except Exception as e:
                log.warning("Drive import failed for %s: %s", df["name"], e)
                failed += 1

    sync["file_map"] = file_map
    sync["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    _save_drive_sync(data)

    return {"imported": imported, "skipped": skipped, "failed": failed}


# --- Atmosphere: weather-based artwork recommendations ---

WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with light hail", 99: "Thunderstorm with heavy hail",
}


async def _fetch_weather_nws(lat: float, lng: float) -> dict:
    headers = {"User-Agent": "FrameArtManager/1.0"}
    cache_key = f"{lat:.2f},{lng:.2f}"
    async with httpx.AsyncClient(timeout=10) as client:
        station_id = _nws_station_cache.get(cache_key)
        if not station_id:
            resp = await client.get(
                f"https://api.weather.gov/points/{lat:.4f},{lng:.4f}", headers=headers,
            )
            if resp.status_code != 200:
                raise WeatherUnavailable("NWS points lookup failed")
            stations_url = resp.json()["properties"]["observationStations"]
            resp = await client.get(stations_url, headers=headers)
            if resp.status_code != 200:
                raise WeatherUnavailable("NWS stations lookup failed")
            features = resp.json().get("features", [])
            if not features:
                raise WeatherUnavailable("No NWS stations nearby")
            station_id = features[0]["properties"]["stationIdentifier"]
            _nws_station_cache[cache_key] = station_id

        resp = await client.get(
            f"https://api.weather.gov/stations/{station_id}/observations/latest",
            headers=headers,
        )
        if resp.status_code != 200:
            raise WeatherUnavailable("NWS observation fetch failed")
        props = resp.json()["properties"]
        temp = props["temperature"]["value"]
        if temp is None:
            raise WeatherUnavailable("NWS temperature unavailable")
        icon = props.get("icon", "")
        return {
            "temp": round(temp, 1),
            "temp_unit": "°C",
            "humidity": round(props["relativeHumidity"]["value"] or 0),
            "wind_speed": round(props["windSpeed"]["value"] or 0, 1),
            "weather_code": None,
            "description": props.get("textDescription", "Unknown"),
            "is_day": "/night/" not in icon,
        }


async def _fetch_weather_openmeteo(lat: float, lng: float) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lng,
        "current": "temperature_2m,relative_humidity_2m,weather_code,is_day,wind_speed_10m",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError:
            raise WeatherUnavailable("Open-Meteo request failed")
        if resp.status_code != 200:
            raise WeatherUnavailable("Open-Meteo returned non-200")
        data = resp.json()
        current = data["current"]
        units = data["current_units"]
        return {
            "temp": current["temperature_2m"],
            "temp_unit": units["temperature_2m"],
            "humidity": current["relative_humidity_2m"],
            "wind_speed": current["wind_speed_10m"],
            "weather_code": current["weather_code"],
            "description": WMO_CODES.get(current["weather_code"], "Unknown conditions"),
            "is_day": bool(current["is_day"]),
        }


async def _fetch_weather(lat: float, lng: float) -> dict:
    try:
        return await _fetch_weather_nws(lat, lng)
    except (WeatherUnavailable, Exception) as e:
        log.info("NWS weather failed (%s), trying Open-Meteo", e)
    try:
        return await _fetch_weather_openmeteo(lat, lng)
    except WeatherUnavailable:
        raise HTTPException(502, "Weather service unavailable")


ATMOSPHERE_PROMPT = """You are a personal art curator for a Samsung Frame TV. You sense the weather outside and choose artwork to match the mood of the moment.

Current weather:
- Conditions: {description}
- Temperature: {temp}{temp_unit}
- Time: {time_of_day}
- Humidity: {humidity}%
- Wind: {wind_speed} km/h

Available artwork:
{candidates}
{exclusion_clause}
Pick exactly one artwork whose vibes best resonate with this weather and moment. Write a curator_note under 40 words — evocative, not clinical, like a gallery wall label connecting the atmosphere outside to the feeling of the piece.

Respond with ONLY this JSON:
{{
  "content_id": "THE_ID",
  "curator_note": "Your poetic note here",
  "vibes_matched": ["vibe1", "vibe2"]
}}"""


@app.post("/api/atmosphere")
async def atmosphere(body: dict):
    lat = body.get("lat")
    lng = body.get("lng")
    exclude_ids = body.get("exclude_ids", [])

    if lat is None or lng is None:
        raise HTTPException(400, "Location required")

    config = _load_ai_config()
    provider = config.get("provider", "claude")
    provider_config = config.get(provider, {})
    api_key = provider_config.get("api_key", "")
    model = provider_config.get("model", "claude-sonnet-4-20250514" if provider == "claude" else "gpt-4.1")
    if not api_key:
        raise HTTPException(400, f"{provider.title()} API key not configured. Open Settings to add it.")

    weather = await _fetch_weather(lat, lng)

    meta = _load_artwork_meta()
    candidates = []
    for cid, info in meta.get("artwork", {}).items():
        ai = info.get("ai_meta", {})
        vibes = ai.get("vibes", [])
        if vibes:
            candidates.append({
                "content_id": cid,
                "title": info.get("title", cid),
                "description": ai.get("description", ""),
                "vibes": vibes,
            })

    if not candidates:
        raise HTTPException(400, "No artwork with AI analysis. Analyze your artwork first.")

    eligible = [c for c in candidates if c["content_id"] not in exclude_ids]
    if not eligible:
        eligible = candidates

    candidate_lines = "\n".join(
        f"- {c['content_id']}: \"{c['title']}\" — {c['description']} | Vibes: {', '.join(c['vibes'])}"
        for c in eligible
    )
    exclusion_clause = ""
    if exclude_ids:
        exclusion_clause = f"\nThe viewer has already seen these suggestions — choose something different: {', '.join(exclude_ids)}\n"

    prompt = ATMOSPHERE_PROMPT.format(
        description=weather["description"],
        temp=weather["temp"],
        temp_unit=weather["temp_unit"],
        time_of_day="Daytime" if weather["is_day"] else "Nighttime",
        humidity=weather["humidity"],
        wind_speed=weather["wind_speed"],
        candidates=candidate_lines,
        exclusion_clause=exclusion_clause,
    )

    async with httpx.AsyncClient(timeout=30) as client:
        if provider == "openai":
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                log.warning("OpenAI API error (atmosphere): %s", resp.status_code)
                raise HTTPException(502, "AI request failed — check your API key")
            atm_result = resp.json()
            usage = atm_result.get("usage", {})
            _record_api_usage(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            text = atm_result["choices"][0]["message"]["content"]
        else:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                log.warning("Claude API error (atmosphere): %s", resp.status_code)
                raise HTTPException(502, "AI request failed — check your API key")
            atm_result = resp.json()
            usage = atm_result.get("usage", {})
            _record_api_usage(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            text = atm_result.get("content", [{}])[0].get("text", "")
        parsed = _parse_ai_response(text)

        valid_ids = {c["content_id"] for c in eligible}
        if not parsed or parsed.get("content_id") not in valid_ids:
            fallback = eligible[0]
            parsed = {
                "content_id": fallback["content_id"],
                "curator_note": "A fitting choice for this moment.",
                "vibes_matched": fallback["vibes"][:2],
            }

        picked_id = parsed["content_id"]
        picked = next(c for c in eligible if c["content_id"] == picked_id)

        return {
            "content_id": picked_id,
            "artwork_title": picked["title"],
            "weather": weather,
            "curator_note": parsed.get("curator_note", ""),
            "vibes_matched": parsed.get("vibes_matched", []),
        }


@app.post("/api/atmosphere/geocode")
async def atmosphere_geocode(body: dict):
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(400, "Location query required")

    async with httpx.AsyncClient(timeout=10) as client:
        attempts = [query]
        first_word = query.split(",")[0].split()[0] if " " in query else None
        if first_word and first_word != query:
            attempts.append(first_word)

        for attempt in attempts:
            resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": attempt, "count": 1},
            )
            if resp.status_code != 200:
                raise HTTPException(502, "Geocoding service unavailable")
            results = resp.json().get("results", [])
            if results:
                r = results[0]
                return {"lat": r["latitude"], "lng": r["longitude"], "name": r["name"]}

        raise HTTPException(404, "Location not found")


def main():
    import uvicorn
    host = os.environ.get("DOCENT_HOST", "0.0.0.0")
    port = int(os.environ.get("DOCENT_PORT", "8000"))
    uvicorn.run("server:app", host=host, port=port, reload=True)


if __name__ == "__main__":
    main()
