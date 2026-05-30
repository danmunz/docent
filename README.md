# Docent

Send any image to your Samsung Frame TV with one click.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Docent is a local web app that puts art on your TV. Drop an image in, press **"Display on Frame,"** and it appears on your screen — no Samsung app, no USB drive, no cloud upload. It also manages your full gallery with a museum-quality interface: browse, organize into collections, crop to 16:9, and swap what's showing anytime. Add AI art identification, weather-aware recommendations, and Google Drive import.

<p align="center">
  <img src="assets/images/docent_working.jpeg" alt="A laptop running Docent in front of a Samsung Frame TV displaying the selected artwork" width="700">
</p>

## Features

- **One-click display** — browse your gallery and press "Display on Frame" to instantly show any artwork on your TV. No Samsung app needed
- Drag-and-drop upload with 16:9 compliance checking and built-in crop editor — images go straight to your TV's storage
- Dense gallery grid with hover title overlays, lazy-loaded thumbnails, lightbox preview, and keyboard navigation
- Automatic year grouping with adaptive bucketing and a sticky group navigation bar
- Matte selection (shadow box, modern, panoramic) and bulk operations
- Collections for organizing artwork into named groups
- Dynamic favicon that recolors to match the currently displayed artwork
- AI art analysis — two-stage pipeline using Google Vision for identification and Claude or OpenAI for rich analysis (artist, title, year, medium, movement, mood)
- Wikipedia integration — every metadata field (artist, school, medium, year, title) links to Wikipedia; group headers when sorted by artist or movement become entry points too
- Atmosphere — weather-aware recommendations that match artwork mood to local conditions, with a poetic curator's note
- Google Drive sync — import artwork from a shared folder
- Art Mode toggle, brightness/color temperature controls, and slideshow configuration
- API usage tracking with per-model token counts and cost estimates
- Museum gallery design with light palette, serif typography, and curated motion
- One-click `Docent.command` launcher with guided setup for non-technical users

## Quick Start

**Prerequisites:** A Samsung Frame TV (2016+) on your local network. Python 3.10+ and [uv](https://docs.astral.sh/uv/) are installed automatically if needed.

> **Linux users:** Pillow may need system libraries. On Debian/Ubuntu: `sudo apt install libjpeg-dev zlib1g-dev`

### Option A: Double-click (macOS only)

Double-click **`Docent.command`** in Finder. On first run, a guided setup wizard walks you through:

1. **TV connection** — enter your Frame TV's IP address (found at TV Settings → General → Network → Network Status)
2. **AI art analysis** — optionally configure Claude or OpenAI for automatic artwork identification
3. **Reverse image search** — optionally add a Google Vision API key for web-based identification

All steps are skippable — you can configure them later in Settings. The script installs `uv` if needed, creates your `.env` and `ai_config.json`, starts the server, and opens your browser.

### Option B: Command line

```bash
# Clone and enter the project
git clone https://github.com/danmunz/docent.git
cd docent

# Configure your TV's IP address
cp .env.example .env
# Edit .env and set DOCENT_TV_IP to your Frame's IP
# (find it at: TV Settings > General > Network > Network Status)

# Install and run
uv run python3 server.py
```

Alternatively, if you prefer `pip`:

```bash
pip install -r requirements.txt
python3 server.py
```

Open **http://localhost:8000** in your browser.

On first connection, your TV will prompt you to allow the remote connection — accept it on the TV. A token is saved locally so you won't need to re-authorize.

A sample artwork is included in `assets/sample/` — drag it into the upload zone to get started. It's not 16:9, so the crop editor will open automatically; this is a good way to see how it works.

## AI Art Analysis

Docent's analysis pipeline has two stages:

1. **Google Vision** (optional) — reverse-image searches museum databases and art sites to identify the painting. Free for 1,000 images/month. Results are passed as context to Stage 2.
2. **Claude or OpenAI** — analyzes the artwork image (with Vision hints when available) and returns artist, title, year, medium, art movement, mood, and a description.

Configure everything through the **Settings** modal (gear icon in the header):

- **Identification** — toggle Google Vision on/off and enter your [Cloud Vision API key](https://console.cloud.google.com/apis/library/vision.googleapis.com)
- **Analysis** — choose Claude or OpenAI, select a model, and enter your API key
- **Automation** — enable auto-analysis on upload

Artwork is analyzed on upload (if auto-analyze is enabled), or manually via the "Analyze" button in the detail panel. Batch analysis is available in Settings.

### Models and Costs

| Provider | Model | Input / Output per MTok | Est. per image |
|----------|-------|------------------------|----------------|
| Claude | Sonnet 4 (default) | $3 / $15 | ~0.2c |
| Claude | Haiku 4.5 | $0.80 / $4 | ~0.06c |
| Claude | Opus 4 | $15 / $75 | ~1c |
| OpenAI | GPT-4.1 | $2 / $8 | ~0.1c |
| OpenAI | GPT-4.1 Mini | $0.40 / $1.60 | ~0.02c |
| OpenAI | GPT-4.1 Nano | $0.10 / $0.40 | ~0.005c |
| OpenAI | GPT-4o | $2.50 / $10 | ~0.13c |

Rough monthly example with Sonnet 4: upload 20 images (~4c), daily Atmosphere for a month (~6-18c), batch-analyze 100 images (~20c). The weather APIs (NWS and Open-Meteo) are free with no key required.

## Atmosphere

Click the cloud icon in the header. Docent reads your local weather via browser geolocation and the National Weather Service API (with Open-Meteo as fallback), matches it against your gallery's mood metadata, and suggests a piece with a short curator's note. The artwork title links to Wikipedia so you can keep reading. Click "Try Again" for a different recommendation.

## Learn More Links

Once artwork is analyzed, every metadata field in the detail panel becomes a quiet Wikipedia link — hover to reveal the ↗ indicator, click to open in a new tab. Artist names link to biographies, art movements to their Wikipedia articles, years to "YYYY in art" pages, and titles to search results.

When you sort the gallery by Artist, School, or Medium, the group headers become links too. The links are invisible until you hover — they reward curiosity without adding visual noise. No API keys required; all URLs are constructed client-side from existing AI metadata.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Escape` | Close lightbox / crop editor / settings / detail panel (innermost first) |
| `Left` / `Right` | Navigate between artworks in the detail panel or lightbox |
| `S` | Toggle selection mode |
| `R` | Refresh artwork grid |
| `Delete` / `Backspace` | Delete selected artwork (when in selection mode) |

## Configuration

Docent uses environment variables for server configuration and a browser-based Settings UI for AI keys and preferences.

### Environment Variables

Copy `.env.example` to `.env` and edit:

| Variable | Default | Description |
|----------|---------|-------------|
| `DOCENT_TV_IP` | *(required)* | Your Samsung Frame TV's IP address |
| `DOCENT_TV_PORT` | `8001` | TV WebSocket port |
| `DOCENT_TV_TIMEOUT` | `15` | Connection timeout in seconds |
| `DOCENT_HOST` | `0.0.0.0` | Server bind address |
| `DOCENT_PORT` | `8000` | Server port |
| `DOCENT_DATA_DIR` | project root | Directory for data files (`.tv-token`, caches, JSON config) |

### AI and Sync Configuration

API keys for Claude, OpenAI, and Google Vision are managed through the Settings modal in the browser — not via environment variables or config files. Settings are stored locally in `ai_config.json` (auto-created, git-ignored).

## Project Structure

```
docent/
  Docent.command     # Double-click launcher with first-run setup wizard
  server.py          # FastAPI backend — TV control, AI pipeline, Drive sync
  index.html         # Single-page frontend (vanilla HTML/CSS/JS)
  assets/            # Logo, fonts, and sample artwork
  tests/             # 73 integration tests, run via pre-commit hook
  pyproject.toml     # Dependencies and project metadata
  .env.example       # Environment variable template
```

Data files are auto-created on first run and git-ignored: `.tv-token`, `ai_config.json`, `api_usage.json`, `drive_sync.json`, `artwork_meta.json`, `collections.json`.

## Development

```bash
# Run the server
uv run python3 server.py

# Run tests
uv run python3 -m pytest

# Install with test dependencies
uv pip install -e ".[test]"
```

A pre-commit hook runs the full test suite and blocks commits that contain secret files (`.env`, `ai_config.json`, `.tv-token`) or API key patterns.

## Credits

Built on:

- **[samsung-tv-ws-api](https://github.com/xchwarze/samsung-tv-ws-api)** by [xchwarze](https://github.com/xchwarze) (LGPL-3.0) — Samsung Smart TV WebSocket communication with Art Mode support. The core dependency.
- **[FastAPI](https://github.com/fastapi/fastapi)** (MIT), **[Uvicorn](https://github.com/encode/uvicorn)** (BSD-3-Clause), **[Pillow](https://github.com/python-pillow/Pillow)** (HPND), **[httpx](https://github.com/encode/httpx)** (BSD-3-Clause), **[python-multipart](https://github.com/Kludex/python-multipart)** (Apache-2.0)

AI identification uses the [Google Cloud Vision API](https://cloud.google.com/vision/docs/detecting-web). Analysis supports the [Claude Messages API](https://docs.anthropic.com/en/api/messages) and [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat).

Sample artwork: Vincent van Gogh, *The Starry Night* (1889). Public domain, sourced from the [Museum of Modern Art](https://www.moma.org/collection/works/79802) via [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Van_Gogh_-_Starry_Night_-_Google_Art_Project.jpg).

Inspired by **[ha-samsungtv-smart](https://github.com/TheFab21/ha-samsungtv-smart)** by [TheFab21](https://github.com/TheFab21), which demonstrated the scope of Samsung's Art Mode API.

## License

[MIT](LICENSE)
