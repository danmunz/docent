# Frame Art Manager

A local web interface for managing custom artwork on your Samsung Frame TV.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

## Features

- **Browse artwork** on your Frame with a thumbnail grid (lazy-loaded as you scroll)
- **Upload images** via drag-and-drop with multi-file preview, progress bar, and auto-rename
- **Display** any artwork on the Frame with one click
- **Delete** artwork individually or in bulk
- **Collections** — organize artwork into named groups
- **Change mattes** — shadow box, modern thin, and more in various colors
- **Image compliance** — automatic 16:9 check with a built-in crop editor
- **AI art analysis** — identify artist, year, medium, art movement, and mood using Claude's vision API
- **Google Drive sync** — import artwork from a shared Drive folder
- **Toggle Art Mode** on/off
- **Adjust brightness and color temperature**
- **Slideshow control** — set duration, shuffle, and category
- **Lightbox** — full-screen artwork preview from the detail panel
- **Keyboard shortcuts** — Escape, arrow keys, `S` for select, `R` for refresh, Delete for bulk remove

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
- **AI analysis**: Click the gear icon to configure your Claude API key, then artwork is automatically analyzed on upload. You can also click "Analyze" in the detail panel or batch-analyze all artwork from settings.
- **Drive sync**: Click "Sync" in the header, add a Google Drive folder URL and API key, then import images directly to the Frame.
- **Lightbox**: Click the artwork image in the detail panel for a full-screen view. Navigate with arrow keys.
- **Settings**: Click the gear icon in the header to adjust brightness, color temperature, and AI settings.
- **Art Mode**: Toggle the "Art Mode" button in the header to switch the TV between art display and screen-off.

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
  server.py          # FastAPI backend — TV WebSocket API, AI analysis, Drive sync
  index.html         # Single-page frontend (vanilla HTML/CSS/JS)
  pyproject.toml     # Dependencies and project metadata
  .tv-token          # Auto-generated auth token (git-ignored)
  ai_config.json     # Claude API settings (git-ignored, auto-created)
  drive_sync.json    # Drive sync configurations (git-ignored, auto-created)
  artwork_meta.json  # Artwork metadata and AI analysis results (auto-created)
  collections.json   # Collection definitions (auto-created)
```

## Credits and Dependencies

This project is built on top of:

- **[samsung-tv-ws-api](https://github.com/xchwarze/samsung-tv-ws-api)** by [xchwarze](https://github.com/xchwarze) (LGPL-3.0) — Python library for communicating with Samsung Smart TVs over WebSocket, including full Art Mode support for Frame TVs. This is the core dependency that makes everything work.
- **[FastAPI](https://github.com/fastapi/fastapi)** by [Sebasti&aacute;n Ram&iacute;rez](https://github.com/tiangolo) (MIT) — Web framework for the backend API.
- **[Uvicorn](https://github.com/encode/uvicorn)** by [Encode](https://github.com/encode) (BSD-3-Clause) — ASGI server.
- **[Pillow](https://github.com/python-pillow/Pillow)** (HPND) — Image format conversion and compliance checking for uploads.
- **[python-multipart](https://github.com/Kludex/python-multipart)** (Apache-2.0) — File upload parsing for FastAPI.
- **[httpx](https://github.com/encode/httpx)** by [Encode](https://github.com/encode) (BSD-3-Clause) — HTTP client for Claude API and Google Drive API integration.

AI art analysis uses the [Claude Messages API](https://docs.anthropic.com/en/api/messages) with vision capabilities. Bring your own API key.

The initial idea for Frame TV art management via a local interface was inspired by the **[ha-samsungtv-smart](https://github.com/TheFab21/ha-samsungtv-smart)** Home Assistant integration by [TheFab21](https://github.com/TheFab21), which demonstrated the full scope of Samsung's Art Mode API.

## License

MIT
