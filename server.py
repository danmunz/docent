from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
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
ARTWORK_META_FILE = Path(__file__).parent / "artwork_meta.json"
AI_CONFIG_FILE = Path(__file__).parent / "ai_config.json"
DRIVE_SYNC_FILE = Path(__file__).parent / "drive_sync.json"

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
    filename: str = Form(""),
):
    data = await file.read()
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
        meta = _load_artwork_meta()
        meta["artwork"][content_id] = {
            "title": original_name,
            "width": w,
            "height": h,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_artwork_meta(meta)
        ai_config = _load_ai_config()
        if ai_config.get("auto_analyze") and ai_config.get("claude", {}).get("api_key"):
            asyncio.create_task(_analyze_artwork_background(content_id))
        return {"ok": True, "content_id": content_id, "title": original_name, "width": w, "height": h}
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


# --- Artwork metadata ---

def _load_artwork_meta() -> dict:
    if ARTWORK_META_FILE.exists():
        return json.loads(ARTWORK_META_FILE.read_text())
    return {"artwork": {}}


def _save_artwork_meta(data: dict) -> None:
    ARTWORK_META_FILE.write_text(json.dumps(data, indent=2))


@app.get("/api/artwork-meta")
async def get_artwork_meta():
    return _load_artwork_meta()


@app.put("/api/artwork-meta/{content_id}")
async def update_artwork_meta(content_id: str, body: dict):
    data = _load_artwork_meta()
    if content_id not in data["artwork"]:
        data["artwork"][content_id] = {}
    for key, value in body.items():
        if key == "title":
            value = value.strip() if isinstance(value, str) else value
            if not value:
                continue
        data["artwork"][content_id][key] = value
    _save_artwork_meta(data)
    return {"ok": True, "content_id": content_id, "meta": data["artwork"][content_id]}


# --- AI config ---

def _load_ai_config() -> dict:
    if AI_CONFIG_FILE.exists():
        return json.loads(AI_CONFIG_FILE.read_text())
    return {"provider": "claude", "auto_analyze": False, "claude": {"api_key": "", "model": "claude-sonnet-4-20250514"}}


def _save_ai_config(data: dict) -> None:
    AI_CONFIG_FILE.write_text(json.dumps(data, indent=2))


@app.get("/api/ai/config")
async def get_ai_config():
    config = _load_ai_config()
    masked = dict(config)
    if masked.get("claude", {}).get("api_key"):
        key = masked["claude"]["api_key"]
        masked["claude"] = dict(masked["claude"])
        masked["claude"]["api_key"] = "..." + key[-4:] if len(key) > 4 else "****"
    return masked


@app.put("/api/ai/config")
async def update_ai_config(body: dict):
    config = _load_ai_config()
    if "auto_analyze" in body:
        config["auto_analyze"] = bool(body["auto_analyze"])
    if "claude" in body:
        claude = body["claude"]
        if "api_key" in claude and claude["api_key"] and not claude["api_key"].startswith("..."):
            config.setdefault("claude", {})["api_key"] = claude["api_key"]
        if "model" in claude:
            config.setdefault("claude", {})["model"] = claude["model"]
    _save_ai_config(config)
    return {"ok": True}


# --- AI analysis ---

AI_PROMPT = """Examine this artwork image and provide metadata in JSON format:
{
  "artist": "Name or 'Unknown'",
  "year": "Year, approximate period, or 'Unknown'",
  "medium": "e.g., 'Oil on canvas', 'Photography', 'Digital art'",
  "school": "e.g., 'Impressionism', 'Minimalism', or 'N/A'",
  "vibes": ["adj1", "adj2", "adj3", "adj4", "adj5"],
  "description": "One-sentence description"
}
Provide exactly 5 vibe adjectives. Respond with ONLY the JSON."""


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
    claude_config = config.get("claude", {})
    api_key = claude_config.get("api_key", "")
    model = claude_config.get("model", "claude-sonnet-4-20250514")

    if not api_key:
        raise HTTPException(400, "Claude API key not configured")

    thumb_path = THUMB_DIR / f"{content_id}.jpg"
    if not thumb_path.exists():
        raise HTTPException(404, f"No thumbnail for {content_id}")

    image_data = base64.b64encode(thumb_path.read_bytes()).decode()

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 512,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
                        {"type": "text", "text": AI_PROMPT},
                    ],
                }],
            },
        )
        if resp.status_code != 200:
            raise HTTPException(502, f"Claude API error: {resp.status_code} — {resp.text[:200]}")

        result = resp.json()
        text = result.get("content", [{}])[0].get("text", "")
        parsed = _parse_ai_response(text)
        if not parsed:
            raise HTTPException(502, f"Could not parse AI response")

        ai_meta = {
            "artist": parsed.get("artist", "Unknown"),
            "year": parsed.get("year", "Unknown"),
            "medium": parsed.get("medium", "Unknown"),
            "school": parsed.get("school", "N/A"),
            "vibes": parsed.get("vibes", [])[:5],
            "description": parsed.get("description", ""),
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "provider": "claude",
            "model": model,
        }

        meta = _load_artwork_meta()
        if content_id not in meta["artwork"]:
            meta["artwork"][content_id] = {}
        meta["artwork"][content_id]["ai_meta"] = ai_meta
        _save_artwork_meta(meta)

        return ai_meta


@app.post("/api/ai/analyze/{content_id}")
async def analyze_artwork(content_id: str):
    ai_meta = await _analyze_artwork(content_id)
    return {"ok": True, "content_id": content_id, "ai_meta": ai_meta}


@app.post("/api/ai/analyze-batch")
async def analyze_batch(body: dict):
    content_ids = body.get("content_ids", [])
    meta = _load_artwork_meta()

    if not content_ids:
        content_ids = [
            cid for cid in {item["content_id"] for item in (_art_cache or [])}
            if cid not in {k for k, v in meta.get("artwork", {}).items() if v.get("ai_meta")}
        ]

    analyzed = 0
    skipped = 0
    failed = 0

    for cid in content_ids:
        existing = meta.get("artwork", {}).get(cid, {}).get("ai_meta")
        if existing:
            skipped += 1
            continue
        try:
            await _analyze_artwork(cid)
            analyzed += 1
            await asyncio.sleep(1)
        except Exception as e:
            log.warning("AI analysis failed for %s: %s", cid, e)
            failed += 1

    return {"analyzed": analyzed, "skipped": skipped, "failed": failed, "total": len(content_ids)}


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


# --- Google Drive sync ---

def _load_drive_sync() -> dict:
    if DRIVE_SYNC_FILE.exists():
        return json.loads(DRIVE_SYNC_FILE.read_text())
    return {"api_key": None, "syncs": []}


def _save_drive_sync(data: dict) -> None:
    DRIVE_SYNC_FILE.write_text(json.dumps(data, indent=2))


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
                raise HTTPException(502, f"Drive API error: {resp.status_code} — {resp.text[:200]}")
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
                original_name = Path(df["name"]).stem
                meta = _load_artwork_meta()
                meta["artwork"][content_id] = {
                    "title": original_name,
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
                    coll_data = _load_collections()
                    for c in coll_data["collections"]:
                        if c["id"] == sync["target"] and content_id not in c["content_ids"]:
                            c["content_ids"].append(content_id)
                    _save_collections(coll_data)

                ai_config = _load_ai_config()
                if ai_config.get("auto_analyze") and ai_config.get("claude", {}).get("api_key"):
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


def main():
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
