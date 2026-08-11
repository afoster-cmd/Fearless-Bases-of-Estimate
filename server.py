#!/usr/bin/env python3
"""
BOE Builder — local server + disk-backed storage
=================================================

Serves the Basis of Estimate builder (boe_builder_*.html) and gives it a real
storage backend on disk, replacing the browser's tiny localStorage quota.

Why this exists (the storage issue this fixes)
----------------------------------------------
The BOE builder runs in three environments:

  1. Inside Claude.ai as an artifact  -> Claude injects window.storage (~5 MB/key)
  2. Opened directly from disk        -> falls back to localStorage, which caps
     (file://)                           the WHOLE origin at ~5-10 MB TOTAL,
                                         shared across every uploaded file, every
                                         saved estimate, and the autosaved draft.
                                         Original file bytes over ~700 KB get
                                         skipped, and a few uploads exhaust it.
  3. Served by THIS script            -> the page detects /api/storage/ping and
     (http://127.0.0.1:8000)             talks to this server instead. Data is
                                         written to the boe_data/ folder next to
                                         this file. No browser quota at all;
                                         per-file cap rises to 25 MB.

The page probes for this server automatically — no configuration in the HTML.

Running it (PyCharm)
--------------------
  1. Put server.py and boe_builder_28.html in the same folder / PyCharm project.
  2. Right-click server.py -> Run 'server'.  (Pure standard library — nothing
     to pip install, no virtualenv packages needed.)
  3. Open http://127.0.0.1:8000 in your browser.

Running it (terminal)
---------------------
  python server.py                 # 127.0.0.1:8000, data in ./boe_data
  python server.py --port 9000
  python server.py --file boe_builder_28.html --data-dir /some/where

Storage API (mirrors the window.storage interface exactly)
----------------------------------------------------------
  GET  /api/storage/ping                        -> {ok, backend, dataDir, ...}
  GET  /api/storage/get?key=K&shared=0|1        -> {key, value, shared} | 404
  POST /api/storage/set    {key, value, shared} -> {key, value, shared}
  POST /api/storage/delete {key, shared}        -> {key, deleted, shared}
  GET  /api/storage/list?prefix=P&shared=0|1    -> {keys, prefix, shared}

Each key is stored as its own JSON file in boe_data/, with the key name
base64url-encoded into the filename — so any key string is safe (no path
traversal is possible) and a crash mid-write can't corrupt a save (writes go
to a temp file first, then an atomic os.replace).

Security note: binds to 127.0.0.1 by default, so only this machine can reach
it. Estimates can carry CUI / contractor-proprietary data — think twice before
exposing this on a network interface with --host.
"""

import argparse
import base64
import binascii
import errno
import json
import os
import re
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------------------
# Configuration (finalized in main() from command-line args)
# ----------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "boe_data")
HTML_PATH = None  # resolved in main()

MAX_KEY_CHARS = 500                    # window.storage spec keeps keys short; be generous
MAX_VALUE_BYTES = 100 * 1024 * 1024    # 100 MB per key — far above the client's 25 MB file cap
MAX_BODY_BYTES = MAX_VALUE_BYTES + (1 * 1024 * 1024)  # value + JSON envelope headroom

_write_lock = threading.Lock()  # serializes writes; reads are lock-free


# ----------------------------------------------------------------------------
# Key <-> filename mapping
#
# A storage key is arbitrary text (e.g. "boe:file:k3j9x2a"). Encoding it with
# URL-safe base64 (no padding) produces a filename made only of [A-Za-z0-9_-],
# which cannot escape DATA_DIR no matter what the key contains. The "p_"/"s_"
# prefix keeps the personal and shared namespaces separate, mirroring the
# `shared` flag in the window.storage interface.
# ----------------------------------------------------------------------------

_FNAME_RE = re.compile(r"^(p|s)_([A-Za-z0-9_-]+)\.json$")


def key_to_filename(key: str, shared: bool) -> str:
    encoded = base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")
    return ("s_" if shared else "p_") + encoded + ".json"


def filename_to_key(fname: str):
    """Return (key, shared) or None if the filename isn't one of ours."""
    m = _FNAME_RE.match(fname)
    if not m:
        return None
    scope, encoded = m.groups()
    pad = "=" * (-len(encoded) % 4)
    try:
        key = base64.urlsafe_b64decode(encoded + pad).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return key, (scope == "s")


# ----------------------------------------------------------------------------
# Storage operations
# ----------------------------------------------------------------------------

class QuotaError(Exception):
    """Raised when the value is too large or the disk is full.

    The message intentionally contains the word "quota" — the client's
    isQuotaError() helper matches /quota/i and fails fast instead of retrying
    (retrying a full disk can never succeed)."""


def storage_set(key: str, value: str, shared: bool) -> None:
    raw = value.encode("utf-8")
    if len(raw) > MAX_VALUE_BYTES:
        raise QuotaError(
            f"Value is {len(raw) / 1024 / 1024:.1f} MB — over the "
            f"{MAX_VALUE_BYTES // (1024 * 1024)} MB per-key quota."
        )
    record = json.dumps(
        {"key": key, "shared": shared, "value": value},
        ensure_ascii=False,
    ).encode("utf-8")
    final_path = os.path.join(DATA_DIR, key_to_filename(key, shared))
    with _write_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(record)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, final_path)  # atomic: a crash never corrupts a save
        except OSError as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            if e.errno == errno.ENOSPC:
                raise QuotaError("Disk is full — storage quota exceeded on the server.") from e
            raise


def storage_get(key: str, shared: bool):
    path = os.path.join(DATA_DIR, key_to_filename(key, shared))
    try:
        with open(path, "rb") as f:
            record = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None  # unreadable/corrupt record behaves like a missing key
    return record.get("value")


def storage_delete(key: str, shared: bool) -> bool:
    path = os.path.join(DATA_DIR, key_to_filename(key, shared))
    with _write_lock:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False


def storage_list(prefix: str, shared: bool):
    keys = []
    try:
        names = os.listdir(DATA_DIR)
    except FileNotFoundError:
        return keys
    for fname in names:
        decoded = filename_to_key(fname)
        if decoded is None:
            continue
        key, is_shared = decoded
        if is_shared == shared and key.startswith(prefix):
            keys.append(key)
    keys.sort()
    return keys


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BOEBuilder/1.0"
    protocol_version = "HTTP/1.1"

    # --- small helpers ------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json_body(self):
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length required.")
            return None
        try:
            length = int(length_header)
        except ValueError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Bad Content-Length.")
            return None
        if length > MAX_BODY_BYTES:
            # Read nothing; reject outright. "quota" keyword => client fails fast.
            self._send_error_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"Request body over the {MAX_VALUE_BYTES // (1024 * 1024)} MB "
                "per-key quota.",
            )
            self.close_connection = True
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Body must be valid JSON.")
            return None
        if not isinstance(body, dict):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Body must be a JSON object.")
            return None
        return body

    @staticmethod
    def _valid_key(key) -> bool:
        return (
            isinstance(key, str)
            and 0 < len(key) <= MAX_KEY_CHARS
            and not any(c in key for c in "\x00\r\n")
        )

    @staticmethod
    def _shared_from_query(qs) -> bool:
        return qs.get("shared", ["0"])[0] in ("1", "true", "True")

    # --- routing ------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/storage/ping":
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "backend": "server",
                "dataDir": DATA_DIR,
                "maxValueBytes": MAX_VALUE_BYTES,
            })
            return

        if path == "/api/storage/get":
            qs = parse_qs(parsed.query)
            key = qs.get("key", [None])[0]
            if not self._valid_key(key):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Missing or invalid 'key'.")
                return
            shared = self._shared_from_query(qs)
            value = storage_get(key, shared)
            if value is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "Key not found: " + key)
                return
            self._send_json(HTTPStatus.OK, {"key": key, "value": value, "shared": shared})
            return

        if path == "/api/storage/list":
            qs = parse_qs(parsed.query)
            prefix = qs.get("prefix", [""])[0]
            shared = self._shared_from_query(qs)
            keys = storage_list(prefix, shared)
            self._send_json(HTTPStatus.OK, {"keys": keys, "prefix": prefix, "shared": shared})
            return

        if path in ("/", "/index.html") or path == "/" + os.path.basename(HTML_PATH):
            self._serve_app()
            return

        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Everything else — including server.py itself and boe_data/ — is not served.
        self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/storage/set":
            body = self._read_json_body()
            if body is None:
                return
            key, value = body.get("key"), body.get("value")
            shared = bool(body.get("shared", False))
            if not self._valid_key(key):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Missing or invalid 'key'.")
                return
            if not isinstance(value, str):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "'value' must be a string.")
                return
            try:
                storage_set(key, value, shared)
            except QuotaError as e:
                self._send_error_json(HTTPStatus.INSUFFICIENT_STORAGE, str(e))
                return
            except OSError as e:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                      "Could not write to disk: " + str(e))
                return
            # Echo the value back — matches the shape window.storage.set returns.
            self._send_json(HTTPStatus.OK, {"key": key, "value": value, "shared": shared})
            return

        if path == "/api/storage/delete":
            body = self._read_json_body()
            if body is None:
                return
            key = body.get("key")
            shared = bool(body.get("shared", False))
            if not self._valid_key(key):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "Missing or invalid 'key'.")
                return
            deleted = storage_delete(key, shared)
            self._send_json(HTTPStatus.OK, {"key": key, "deleted": deleted, "shared": shared})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    # --- app file -----------------------------------------------------------

    def _serve_app(self):
        try:
            with open(HTML_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Could not read {os.path.basename(HTML_PATH)} — is it in the same "
                "folder as server.py?",
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # always pick up the latest edit
        self.end_headers()
        self.wfile.write(body)

    # --- logging ------------------------------------------------------------

    def log_message(self, fmt, *args):
        # One concise line per request; skip favicon noise.
        if "/favicon.ico" in (self.path or ""):
            return
        sys.stderr.write("  %s %s\n" % (self.command or "-", self.path or "-"))


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------

def find_html(explicit: str | None) -> str:
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(APP_DIR, explicit)
        if not os.path.isfile(path):
            sys.exit(f"error: --file {explicit!r} not found.")
        return path

    def version_of(name: str) -> int:
        m = re.search(r"(\d+)", name)
        return int(m.group(1)) if m else -1

    candidates = [f for f in os.listdir(APP_DIR)
                  if re.match(r"^boe_builder.*\.html$", f, re.IGNORECASE)]
    if not candidates:
        sys.exit(
            "error: no boe_builder*.html found next to server.py.\n"
            "Put the builder HTML in the same folder, or point at it with --file."
        )
    candidates.sort(key=lambda f: (version_of(f), f))
    return os.path.join(APP_DIR, candidates[-1])


def main():
    global DATA_DIR, HTML_PATH

    parser = argparse.ArgumentParser(description="BOE Builder local server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1 — this machine only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=DATA_DIR,
                        help="where estimate/file data is stored (default: ./boe_data)")
    parser.add_argument("--file", default=None,
                        help="which HTML file to serve (default: newest boe_builder*.html here)")
    args = parser.parse_args()

    DATA_DIR = os.path.abspath(args.data_dir)
    os.makedirs(DATA_DIR, exist_ok=True)
    HTML_PATH = find_html(args.file)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("BOE Builder server")
    print(f"  serving : {os.path.basename(HTML_PATH)}")
    print(f"  data    : {DATA_DIR}")
    print(f"  open    : http://{args.host}:{args.port}")
    print("  stop    : Ctrl+C (PyCharm: red stop button)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
