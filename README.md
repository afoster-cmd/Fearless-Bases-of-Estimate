# BOE Builder — Local Server (storage fix)

Two files, no dependencies:

| File | What it is |
|---|---|
| `server.py` | Python 3 server (standard library only — nothing to `pip install`) that serves the app and stores its data on disk |
| `boe_builder_30.html` | The builder — three-tier storage layer that auto-detects the server, one-page layout (no separate sidebar scrollbar), Fearless brand palette (plum #5C3977, coral #EE5341, lavender #CBC4D5, off-white #F4F2EE), and Raleway type with Segoe UI/system fallbacks — never Arial, even offline |

## Run it in PyCharm

1. **File → Open** the folder containing `server.py` and `boe_builder_30.html` (they must sit in the same folder).
2. Right-click `server.py` → **Run 'server'**.
3. The Run panel prints the address — open **http://127.0.0.1:8000** in Edge/Chrome.
4. That's it. The page detects the server automatically and the save-status area in the sidebar shows **"Server mode: saved to the boe_data folder next to server.py."**

Stop it with the red stop button (or Ctrl+C in a terminal).

From a plain terminal instead: `python server.py` (add `--port 9000` to change the port; in PyCharm put `--port 9000` in Run → Edit Configurations → Script parameters).

## What the storage issue was, and how this fixes it

The v27 file had two storage modes:

- **Inside Claude.ai** — Claude injects a real `window.storage` (~5 MB per key). Fine.
- **Downloaded and opened from disk** — no `window.storage` exists, so it fell back to `localStorage`. That's the problem: localStorage caps the **whole origin at ~5–10 MB total**, shared across every uploaded file, every saved estimate, and the autosaved draft. Original file bytes over ~700 KB were skipped, and two or three uploads could exhaust the quota entirely (silent `QuotaExceededError`s).

`server.py` adds a third mode. When the page is served from it, the page's `window.storage` calls go over HTTP to the server, which writes each key as a JSON file in `boe_data/` next to `server.py`:

- **No browser quota at all** — disk is the limit.
- Per-file byte cap rises from **700 KB → 25 MB** (server backstop: 100 MB per key).
- Data survives cache clears, browser switches, and "clear site data" — it's plain files you can back up by copying the folder.
- Writes are atomic (temp file + rename), so a crash mid-save can't corrupt an estimate.

Detection order on page load: Claude's native `window.storage` → ping `/api/storage/ping` for this server → `localStorage` fallback. The same single HTML file still works in all three environments with identical behavior — nothing about the Claude-artifact or standalone modes changed.

## Where the data lives

`boe_data/` (created automatically next to `server.py`). Each key — draft, saved estimates, stored file bytes — is one JSON file with a base64-encoded name. Back up or move your work by copying that folder. Custom location: `--data-dir /path/to/wherever`.

## Storage API (what the page calls)

Mirrors the `window.storage` interface exactly, same shapes, same error semantics (404 on missing key; 413/507 with "quota" wording on oversize/disk-full so the page's fail-fast quota handling still works):

```
GET  /api/storage/ping
GET  /api/storage/get?key=K&shared=0|1
POST /api/storage/set        {key, value, shared}
POST /api/storage/delete     {key, shared}
GET  /api/storage/list?prefix=P&shared=0|1
```

## Security notes

- Binds to `127.0.0.1` by default — only your machine can reach it. Estimates can carry CUI / contractor-proprietary content, so think twice before using `--host 0.0.0.0` on a shared network; there is no authentication.
- The server only serves the builder HTML and the storage API. It will not serve `server.py`, `boe_data/`, or anything else on disk, and storage keys are filename-encoded so no key can escape the data folder.
