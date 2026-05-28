# Frame Art Manager

A local web interface for managing custom artwork on your Samsung Frame TV. Gallery-inspired design with a light museum palette, serif headings, and curated motion.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

## Features

- **Browse artwork** on your Frame with a thumbnail grid (lazy-loaded as you scroll)
- **Upload images** via drag-and-drop with multi-file preview, progress bar, and auto-rename
- **Display** any artwork on the Frame with one click
- **Delete** artwork individually or in bulk
- **Collections** — organize artwork into named groups
- **Change mattes** — shadow box, modern thin, and more in various colors
- **Image compliance** — automatic 16:9 check with a built-in crop editor
- **AI art analysis** — two-stage identification and analysis pipeline:
  - **Stage 1: Google Vision** — reverse-image searches museum databases, Wikipedia, and art sites to identify the painting (free for 1,000 images/month)
  - **Stage 2: Claude or OpenAI** — analyzes the artwork for artist, title, year, medium, art movement, mood, and description, using Vision's identification as strong context
  - Supports **Claude** (Sonnet, Haiku, Opus) and **OpenAI** (GPT-4.1, GPT-4.1 Mini, GPT-4.1 Nano, GPT-4o) as interchangeable analysis providers
  - **Opus fallback** — optionally re-analyzes with Claude Opus when Sonnet returns low confidence
  - **Stored originals** — saves high-resolution copies of uploads for better AI analysis
  - **Filename hints** — extracts artist/title from descriptive filenames to guide the AI
  - **Provenance tracking** — each artwork shows which tools contributed to its analysis (e.g., "ID: Google Vision · Analysis: GPT-4.1")
- **Atmosphere** — weather-aware artwork recommendations: reads local weather, matches it to your gallery's mood, and suggests a piece with a poetic curator's note
- **Google Drive sync** — import artwork from a shared Drive folder
- **Toggle Art Mode** on/off
- **Adjust brightness and color temperature**
- **Slideshow control** — set duration, shuffle, and category
- **Lightbox** — full-screen artwork preview from the detail panel
- **Keyboard shortcuts** — Escape, arrow keys, `S` for select, `R` for refresh, Delete for bulk remove
- **API usage tracking** — per-model token counts and estimated costs, visible in Settings

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Samsung Frame TV (Tizen OS, 2016+) on the same local network

## Setup

1. Find your Frame TV's IP address (Settings > General > Network > Network Status on the TV, or check your router's device list).

2. Edit `server.py` and set your TV's IP:
   ```python
   TV_IP = "192.168.1.36"  # <-- your Frame's IP
   ```

3. Start the server:
   ```bash
   cd ~/projects/frame-art-manager
   uv run uvicorn server:app --host 0.0.0.0 --port 8000
   ```

4. Open **http://localhost:8000** in your browser.

On first connection, your TV may display a prompt asking you to allow the remote connection — accept it on the TV. A token is saved to `.tv-token` so you won't need to re-authorize.

## Usage

- **Upload**: Drag and drop images onto the upload zone, choose a matte style, and click "Upload to Frame."
- **Display**: Click any artwork in the grid to open its detail panel, then click "Display on Frame."
- **Delete**: Click "Select" in the toolbar, check the artworks you want to remove, and click "Delete Selected."
- **Collections**: Use the dropdown to create and switch between collections. Select artwork and assign them to a collection.
- **Mattes**: Open an artwork's detail panel and click a matte style to apply it.
- **Crop editor**: If an image isn't 16:9, a crop tool lets you adjust the frame before uploading.
- **AI analysis**: Click the gear icon to configure your API keys and provider. Artwork is automatically analyzed on upload (if enabled). You can also click "Analyze" in the detail panel or batch-analyze all artwork from Settings.
- **Atmosphere**: Click the cloud icon in the header. The app reads your local weather (via browser geolocation and Open-Meteo) and asks the AI to pick the artwork whose mood best matches the moment, complete with a short curator's note. Click "Try Again" for a different pick.
- **Drive sync**: Click "Sync" in the header, add a Google Drive folder URL and API key, then import images directly to the Frame.
- **Lightbox**: Click the artwork image in the detail panel for a full-screen view. Navigate with arrow keys.
- **Settings**: Click the gear icon in the header to configure AI providers, API keys, Google Vision, and view usage stats.
- **Art Mode**: Toggle the "Art Mode" button in the header to switch the TV between art display and screen-off.

## AI Configuration

The Settings modal (gear icon) is organized into three sections:

### Identification (Google Vision)
- **Google Cloud Vision** uses web detection to reverse-image search your artwork against museum databases, Wikipedia, and art marketplaces. It identifies the painting's title and artist from page titles and web entities.
- Free for the first 1,000 images/month. Requires a [Google Cloud Vision API key](https://console.cloud.google.com/apis/library/vision.googleapis.com).
- Can be toggled on/off independently. When enabled, Vision results are passed as context hints to the analysis provider — the AI confirms and enriches rather than guessing blind.

### Analysis (Claude or OpenAI)
- Choose between **Claude (Anthropic)** and **GPT (OpenAI)** as your analysis provider.
- Each provider has its own API key and model selector.
- Claude models: Sonnet 4, Haiku 4.5, Opus 4 (with optional Opus fallback for low-confidence results).
- OpenAI models: GPT-4.1, GPT-4.1 Mini, GPT-4.1 Nano, GPT-4o.
- The active provider is used for both artwork analysis and Atmosphere curation.

### Automation
- **Auto-analyze new uploads** — automatically runs the full pipeline (Vision + AI) on each new upload or Drive sync import.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Escape` | Close lightbox / crop editor / settings / detail panel (innermost first) |
| `Left` / `Right` | Navigate between artworks in the detail panel or lightbox |
| `S` | Toggle selection mode |
| `R` | Refresh artwork grid |
| `Delete` / `Backspace` | Delete selected artwork (when in selection mode) |

## Project Structure

```
frame-art-manager/
  server.py          # FastAPI backend — TV WebSocket API, AI pipeline, Drive sync
  index.html         # Single-page frontend (vanilla HTML/CSS/JS)
  tests/             # Pytest suite (62 integration tests, run via pre-commit hook)
  pyproject.toml     # Dependencies and project metadata
  .tv-token          # Auto-generated auth token (git-ignored)
  ai_config.json     # Multi-provider AI settings (git-ignored, auto-created)
  api_usage.json     # Per-model token/cost tracking (git-ignored, auto-created)
  drive_sync.json    # Drive sync configurations (git-ignored, auto-created)
  artwork_meta.json  # Artwork metadata and AI analysis results (auto-created)
  collections.json   # Collection definitions (auto-created)
```

## AI Costs

AI analysis costs depend on the provider and model. Google Vision web detection is free for the first 1,000 calls/month.

### Analysis provider costs

| Provider | Model | Input / Output per MTok | Est. cost per image |
|----------|-------|------------------------|---------------------|
| Claude | Sonnet 4 (default) | $3 / $15 | ~0.2¢ |
| Claude | Haiku 4.5 | $0.80 / $4 | ~0.06¢ |
| Claude | Opus 4 | $15 / $75 | ~1¢ |
| OpenAI | GPT-4.1 | $2 / $8 | ~0.1¢ |
| OpenAI | GPT-4.1 Mini | $0.40 / $1.60 | ~0.02¢ |
| OpenAI | GPT-4.1 Nano | $0.10 / $0.40 | ~0.005¢ |
| OpenAI | GPT-4o | $2.50 / $10 | ~0.13¢ |

### Feature breakdown

| Feature | What it does | Est. cost per call |
|---------|-------------|-------------------|
| **Art analysis** | Vision identification (free) + AI analysis of thumbnail | See table above |
| **Auto-analyze on upload** | Same pipeline, triggered automatically | Same |
| **Batch analyze** | Runs pipeline sequentially for all unanalyzed artwork | Cost × N images |
| **Atmosphere** | Text-only prompt with weather + vibes, returns recommendation | ~0.1–0.3¢ |

**Rough monthly examples (Sonnet 4):**
- Upload 20 images with auto-analyze: **~4¢**
- Click Atmosphere once a day for a month: **~6–18¢**
- One-time batch analyze of 100 existing images: **~20¢**

The weather API (Open-Meteo) and geocoding are completely free with no API key required. Image costs for art analysis depend on thumbnail resolution — Samsung Frame thumbnails are typically small (320–640px wide), keeping token counts low. Usage stats (calls, tokens, estimated cost) are displayed in Settings.

## Credits and Dependencies

This project is built on top of:

- **[samsung-tv-ws-api](https://github.com/xchwarze/samsung-tv-ws-api)** by [xchwarze](https://github.com/xchwarze) (LGPL-3.0) — Python library for communicating with Samsung Smart TVs over WebSocket, including full Art Mode support for Frame TVs. This is the core dependency that makes everything work.
- **[FastAPI](https://github.com/fastapi/fastapi)** by [Sebasti&aacute;n Ram&iacute;rez](https://github.com/tiangolo) (MIT) — Web framework for the backend API.
- **[Uvicorn](https://github.com/encode/uvicorn)** by [Encode](https://github.com/encode) (BSD-3-Clause) — ASGI server.
- **[Pillow](https://github.com/python-pillow/Pillow)** (HPND) — Image format conversion and compliance checking for uploads.
- **[python-multipart](https://github.com/Kludex/python-multipart)** (Apache-2.0) — File upload parsing for FastAPI.
- **[httpx](https://github.com/encode/httpx)** by [Encode](https://github.com/encode) (BSD-3-Clause) — HTTP client for AI provider APIs and Google Drive integration.

AI art identification uses the [Google Cloud Vision API](https://cloud.google.com/vision/docs/detecting-web) for web detection. Art analysis supports the [Claude Messages API](https://docs.anthropic.com/en/api/messages) and [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat). Bring your own API keys.

The initial idea for Frame TV art management via a local interface was inspired by the **[ha-samsungtv-smart](https://github.com/TheFab21/ha-samsungtv-smart)** Home Assistant integration by [TheFab21](https://github.com/TheFab21), which demonstrated the full scope of Samsung's Art Mode API.

## License

MIT
