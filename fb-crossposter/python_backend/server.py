"""
FB Post Copier — Python Backend Server
=======================================

Runs the automation worker plus an optional dashboard:

  1. WebSocket server -> ws://localhost:8765 (asyncio, main thread) — the extension
  2. Flask dashboard  -> http://127.0.0.1:5000 (background thread, skip with --headless)

The browser extension does the Facebook work; this process decides WHAT and WHEN
to post, dedups, enforces the caps, and can run completely on its own:

    python server.py                          # dashboard + automation
    python server.py --headless --autostart   # no dashboard, farm starts on connect
    python fbctl.py status                    # control it from a terminal

Everything is configurable through environment variables so the same process
runs inside Docker:

    FB_COPIER_WS_HOST=0.0.0.0     FB_COPIER_WS_PORT=8765
    FB_COPIER_HTTP_HOST=0.0.0.0   FB_COPIER_HTTP_PORT=5000
    FB_COPIER_AUTOSTART=true

State is persisted in SQLite at ``data/posts.db``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from threading import Thread

import websockets
from flask import Flask, jsonify, request
from flask_cors import CORS

import notifier

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FB_COPIER_DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "posts.db")

# Backup format version (see export_state / import_state)
EXPORT_VERSION = 1

# Hosts / ports — all overridable via environment variables (used by Docker
# and for local testing). Change the defaults with server.py flags if needed.
WS_HOST = os.environ.get("FB_COPIER_WS_HOST", "localhost")
WS_PORT = int(os.environ.get("FB_COPIER_WS_PORT", "8765"))
HTTP_HOST = os.environ.get("FB_COPIER_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("FB_COPIER_HTTP_PORT", "5000"))

# ---------------------------------------------------------------------------
# Default settings (values are stored as strings for SQLite simplicity)
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "source_group_url": "https://www.facebook.com/groups/1144780033953810",
    "target_group_url": "https://www.facebook.com/groups/1770877160904592",
    "scan_interval_seconds": "60",
    # Adaptive scanning ceiling: while scans keep finding nothing the interval is
    # stretched (×1.5 per quiet cycle) up to this many seconds, and it snaps back to
    # scan_interval_seconds as soon as a scan finds new posts. 0 (or <= the base
    # interval) disables the backoff entirely.
    "scan_interval_max_seconds": "900",
    "max_posts_per_day": "20",
    "cooldown_seconds": "10",
    "batch_size": "3",
    "auto_mode": "false",
    # Secondary, image-independent dedup: skip a post whose text was already
    # stored in the last 30 days (>= 40 characters). Prevents duplicates when
    # Facebook serves a different image URL for the same post.
    "dedup_text_only": "true",
    # Randomize delays slightly so posting is not machine-clockwork.
    "humanize_timing": "true",
    # Posting window in local time (24h clock). 0/0 = always active;
    # start > end wraps over midnight (e.g. 22 -> 6).
    "active_hours_start": "0",
    "active_hours_end": "0",
    # Push notifications — see notifier.py. provider: none | ntfy | telegram | webhook | knock
    "notify_provider": "none",
    "notify_on_posted": "true",
    "notify_on_failed": "true",
    "notify_on_queued": "true",
    "notify_ntfy_url": "",
    "notify_ntfy_token": "",
    "notify_telegram_token": "",
    "notify_telegram_chat_id": "",
    "notify_telegram_api_base": "https://api.telegram.org",
    "notify_webhook_url": "",
    "notify_knock_api_key": "",
}

# Adaptive scan cadence (see adaptive_scan_interval).
MIN_SCAN_INTERVAL_SECONDS = 10      # hard floor so a bad setting cannot hammer Facebook
SCAN_BACKOFF_FACTOR = 1.5           # interval multiplier per quiet cycle
SCAN_QUIET_CYCLES_BEFORE_BACKOFF = 2  # re-check at the base interval this many times first

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None


def get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL;")
    return _db_conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column to an existing table (lightweight migration for older DBs)."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        print(f"[server] migrated: added {table}.{column}")


def init_db() -> None:
    conn = get_db()
    with _db_lock:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content_hash TEXT UNIQUE NOT NULL,
              text TEXT,
              link TEXT,
              author TEXT,
              permalink TEXT,
              images_count INTEGER DEFAULT 0,
              images_base64 TEXT,
              status TEXT DEFAULT 'new',
              found_at INTEGER,
              posted_at INTEGER,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT
            );
            """
        )
        # Secondary text-only dedup support (added in v9.2)
        _ensure_column(conn, "posts", "text_norm", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_text_norm ON posts(text_norm)")
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v)
            )
        conn.commit()


def get_all_settings() -> dict:
    conn = get_db()
    with _db_lock:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        out[r["key"]] = r["value"]
    return out


def set_setting(key: str, value) -> None:
    conn = get_db()
    with _db_lock:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def update_settings(d: dict) -> None:
    for k, v in d.items():
        if k not in DEFAULT_SETTINGS:
            continue  # ignore unknown keys
        if k == "auto_mode":
            v = "true" if v else "false"
        set_setting(k, v)


def import_extension_settings(ext: dict) -> None:
    """Adopt group URLs configured in the extension popup when the DB still has defaults."""
    cur = get_all_settings()
    changed = False
    for ext_key, db_key in (
        ("sourceGroupUrl", "source_group_url"),
        ("targetGroupUrl", "target_group_url"),
    ):
        val = str(ext.get(ext_key) or "").strip()
        if not val.startswith("http"):
            continue
        if val != DEFAULT_SETTINGS[db_key] and cur.get(db_key) == DEFAULT_SETTINGS[db_key]:
            set_setting(db_key, val)
            changed = True
    if changed:
        print("[server] imported group URLs from the extension popup")


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------
_SEE_MORE_RE = re.compile(r"\b(see more|show more)\b", re.IGNORECASE)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    t = text.strip().lower()
    t = _SEE_MORE_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def strip_query(url: str | None) -> str:
    if not url:
        return ""
    return url.split("?", 1)[0]


def compute_hash(post: dict) -> str:
    text = normalize_text(post.get("text"))
    images = post.get("images") or []
    first_img = strip_query(images[0]) if images else ""
    raw = f"{text}|{first_img}" if text else first_img
    if not raw:
        raw = "empty"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def text_norm(text: str | None) -> str:
    """Normalized text used by the secondary (image-independent) dedup."""
    return normalize_text(text)[:300]


def recent_text_exists(tn: str, window_days: int = 30) -> bool:
    """True if an identical normalized text was stored recently."""
    if not tn:
        return False
    conn = get_db()
    since = int(time.time()) - window_days * 86400
    with _db_lock:
        row = conn.execute(
            "SELECT 1 FROM posts WHERE text_norm=? AND found_at>=? LIMIT 1",
            (tn, since),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Posts CRUD
# ---------------------------------------------------------------------------
def post_exists(h: str):
    conn = get_db()
    with _db_lock:
        return conn.execute(
            "SELECT id, status FROM posts WHERE content_hash=?", (h,)
        ).fetchone()


def insert_post(post: dict, h: str) -> int | None:
    conn = get_db()
    images = post.get("images") or []
    images_base64 = post.get("imagesBase64") or post.get("images_base64") or []
    payload = {
        "content_hash": h,
        "text": post.get("text") or "",
        "text_norm": text_norm(post.get("text")),
        "link": post.get("link") or "",
        "author": post.get("author") or "",
        "permalink": post.get("permalink") or "",
        "images_count": len(images),
        "images_base64": json.dumps(images_base64),
        "status": "new",
        "found_at": int(time.time()),
        "posted_at": None,
        "error": None,
    }
    with _db_lock:
        try:
            cur = conn.execute(
                """
                INSERT INTO posts
                  (content_hash, text, text_norm, link, author, permalink,
                   images_count, images_base64, status, found_at, posted_at, error)
                VALUES
                  (:content_hash, :text, :text_norm, :link, :author, :permalink,
                   :images_count, :images_base64, :status, :found_at, :posted_at, :error)
                """,
                payload,
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def update_post_status(post_id: int, status: str, error: str | None = None) -> None:
    conn = get_db()
    posted_at = int(time.time()) if status == "posted" else None
    with _db_lock:
        conn.execute(
            "UPDATE posts SET status=?, error=?, posted_at=? WHERE id=?",
            (status, error, posted_at, post_id),
        )
        conn.commit()


def list_posts(limit: int = 1000) -> list[dict]:
    conn = get_db()
    with _db_lock:
        rows = conn.execute(
            "SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_posts() -> int:
    conn = get_db()
    with _db_lock:
        row = conn.execute("SELECT COUNT(*) AS c FROM posts").fetchone()
    return int(row["c"]) if row else 0


def count_posted_today() -> int:
    conn = get_db()
    start = int(time.time()) - 86400
    with _db_lock:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM posts WHERE status='posted' AND posted_at >= ?",
            (start,),
        ).fetchone()
    return int(row["c"]) if row else 0


def count_by_status(status: str) -> int:
    conn = get_db()
    with _db_lock:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM posts WHERE status=?", (status,)
        ).fetchone()
    return int(row["c"]) if row else 0


def reset_posts() -> None:
    conn = get_db()
    with _db_lock:
        conn.execute("DELETE FROM posts")
        conn.commit()


def export_state(include_images: bool = False) -> dict:
    """Everything needed to move the setup to another machine in one JSON file.

    Images (base64) are only kept for posts that still need to be posted, so a
    backup of a long history stays small.
    """
    conn = get_db()
    with _db_lock:
        rows = conn.execute(
            "SELECT content_hash, text, text_norm, link, author, permalink, "
            "images_count, images_base64, status, found_at, posted_at, error "
            "FROM posts ORDER BY id"
        ).fetchall()
    posts = []
    for r in rows:
        d = dict(r)
        keep_images = include_images or d.get("status") == "new"
        if not keep_images:
            d["images_base64"] = None
        posts.append(d)
    return {
        "version": EXPORT_VERSION,
        "exported_at": int(time.time()),
        "settings": get_all_settings(),
        "posts": posts,
    }


def import_state(data: dict, mode: str = "replace") -> dict:
    """Restore a backup. mode='replace' wipes history first, 'merge' keeps it."""
    if not isinstance(data, dict) or not isinstance(data.get("settings"), dict) \
            or not isinstance(data.get("posts"), list):
        raise ValueError("not a FB Post Copier backup (needs settings + posts)")
    if mode not in ("replace", "merge"):
        mode = "replace"

    settings_imported = 0
    for k, v in (data.get("settings") or {}).items():
        if k in DEFAULT_SETTINGS:
            set_setting(k, v)
            settings_imported += 1

    conn = get_db()
    inserted = skipped = 0
    with _db_lock:
        if mode == "replace":
            conn.execute("DELETE FROM posts")
        for p in data.get("posts") or []:
            if not isinstance(p, dict) or not p.get("content_hash"):
                skipped += 1
                continue
            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO posts
                      (content_hash, text, text_norm, link, author, permalink,
                       images_count, images_base64, status, found_at, posted_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p.get("content_hash"),
                        p.get("text") or "",
                        p.get("text_norm") or text_norm(p.get("text")),
                        p.get("link") or "",
                        p.get("author") or "",
                        p.get("permalink") or "",
                        int(p.get("images_count") or 0),
                        p.get("images_base64") or "[]",
                        p.get("status") or "new",
                        int(p.get("found_at") or time.time()),
                        p.get("posted_at"),
                        p.get("error"),
                    ),
                )
                inserted += cur.rowcount
            except (sqlite3.Error, TypeError, ValueError):
                skipped += 1
        conn.commit()
    print(
        f"[server] import ({mode}): {inserted} post(s) restored, {skipped} skipped, "
        f"{settings_imported} setting(s) applied"
    )
    return {
        "ok": True,
        "mode": mode,
        "settings_imported": settings_imported,
        "posts_inserted": inserted,
        "posts_skipped": skipped,
    }


def fetch_backlog(limit: int) -> list[dict]:
    """Oldest unposted rows first; 'new' only — never re-posts anything already handled."""
    conn = get_db()
    with _db_lock:
        rows = conn.execute(
            "SELECT * FROM posts WHERE status='new' ORDER BY found_at ASC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_post_row(post_id: int) -> dict | None:
    conn = get_db()
    with _db_lock:
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    return dict(row) if row else None


def recover_stale_posting() -> None:
    """A row left in 'posting' by a hard crash can't be trusted; surface it as failed."""
    conn = get_db()
    with _db_lock:
        cur = conn.execute(
            "UPDATE posts SET status='failed', error='Interrupted (server restart)' "
            "WHERE status='posting'"
        )
        conn.commit()
    if cur.rowcount:
        print(f"[server] recovered {cur.rowcount} interrupted post(s) -> failed")


def clear_post_images(post_id: int) -> None:
    """Drop stored base64 after a successful post to keep the database small."""
    conn = get_db()
    with _db_lock:
        conn.execute("UPDATE posts SET images_base64='[]' WHERE id=?", (post_id,))
        conn.commit()


def int_setting(settings: dict, key: str, default: int) -> int:
    try:
        return int(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def humanize(seconds: float, ratio: float = 0.25) -> float:
    """Randomize a delay by ±ratio so posting doesn't look like clockwork."""
    if seconds <= 0:
        return 0.0
    return max(1.0, seconds * (1.0 + random.uniform(-ratio, ratio)))


def adaptive_scan_interval(
    settings: dict, current: int, quiet_cycles: int, new_count: int
) -> tuple[int, int]:
    """Pick the next scan interval for the source group, plus its quiet-cycle streak.

    A group that keeps producing nothing does not need to be re-read every minute, and
    a group that just produced a batch usually has more coming. So:

    * a scan that stored posts → back to the base interval immediately (quiet streak 0)
    * the first SCAN_QUIET_CYCLES_BEFORE_BACKOFF empty scans → still the base interval
    * after that → ×SCAN_BACKOFF_FACTOR per empty scan, capped by
      scan_interval_max_seconds

    ``scan_interval_max_seconds`` of 0, or lower than the base interval, disables the
    backoff (the cadence then stays at the base interval). Never returns less than
    MIN_SCAN_INTERVAL_SECONDS.
    """
    base = max(MIN_SCAN_INTERVAL_SECONDS, int_setting(settings, "scan_interval_seconds", 60))
    ceiling = int_setting(settings, "scan_interval_max_seconds", 900)
    ceiling = max(base, ceiling)   # a ceiling below the base means "no backoff"

    if new_count > 0:
        return base, 0

    quiet_cycles += 1
    if quiet_cycles < SCAN_QUIET_CYCLES_BEFORE_BACKOFF or ceiling <= base:
        return max(base, min(current or base, ceiling)), quiet_cycles

    stretched = int(round(max(base, current or base) * SCAN_BACKOFF_FACTOR))
    return min(ceiling, max(base, stretched)), quiet_cycles


def within_active_hours(settings: dict) -> bool:
    """Posting window in local time; start == end (or 0/0) disables the check."""
    start = int_setting(settings, "active_hours_start", 0) % 24
    end = int_setting(settings, "active_hours_end", 0) % 24
    if start == end:
        return True
    hour = datetime.now().hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window crosses midnight


def row_payload(row: dict) -> dict:
    """Rebuild the post payload the extension expects for do_post."""
    try:
        images = json.loads(row.get("images_base64") or "[]")
    except (TypeError, ValueError):
        images = []
    return {
        "text": row.get("text") or "",
        "link": row.get("link") or "",
        "author": row.get("author") or "",
        "permalink": row.get("permalink") or "",
        "images": [],
        "imagesBase64": images,
    }


# ---------------------------------------------------------------------------
# Desktop notifications (Linux notify-send)
# ---------------------------------------------------------------------------
def notify(title: str, message: str) -> None:
    try:
        subprocess.Popen(
            ["notify-send", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # not on Linux or notify-send missing — silently ignore


# ---------------------------------------------------------------------------
# Server state (shared between Flask thread and asyncio loop)
# ---------------------------------------------------------------------------
class ServerState:
    def __init__(self) -> None:
        self.extension_ws = None  # set by async loop only
        self.auto_mode: bool = False
        self.scan_task: asyncio.Task | None = None
        self.post_futures: dict[str, asyncio.Future] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        # --- automation control ---
        self.scan_event: asyncio.Event = asyncio.Event()
        self.scan_busy: bool = False
        self.post_lock: asyncio.Lock = asyncio.Lock()
        self.posting: bool = False
        # --- stats (shown in the dashboard, fbctl and the extension popup) ---
        self.started_at: int = int(time.time())
        self.last_scan_at: int | None = None
        self.last_post_at: int | None = None
        self.last_error: str = ""
        # --- adaptive scan cadence (see adaptive_scan_interval) ---
        self.scan_interval_current: int = 0   # 0 = not started yet
        self.scan_quiet_cycles: int = 0       # consecutive scans that found nothing
        self.last_scan_new_count: int = 0     # new posts stored by the last scan

    @property
    def connected(self) -> bool:
        return self.extension_ws is not None


state = ServerState()


def build_stats() -> dict:
    """Everything the dashboard, fbctl and the extension popup need to know."""
    settings = get_all_settings()
    return {
        "connected": state.connected,
        "auto_mode": state.auto_mode,
        "posting": state.posting,
        "scan_busy": state.scan_busy,
        "posts_total": count_posts(),
        "posted_total": count_by_status("posted"),
        "failed_total": count_by_status("failed"),
        "new_queue": count_by_status("new"),
        "posted_today": count_posted_today(),
        "last_scan_at": state.last_scan_at,
        "last_post_at": state.last_post_at,
        "scan_interval_seconds": int_setting(settings, "scan_interval_seconds", 60),
        "scan_interval_current": state.scan_interval_current or int_setting(settings, "scan_interval_seconds", 60),
        "scan_quiet_cycles": state.scan_quiet_cycles,
        "last_error": state.last_error,
        "started_at": state.started_at,
        "uptime_seconds": int(time.time()) - state.started_at,
        "active_hours": f"{settings.get('active_hours_start', '0')}-{settings.get('active_hours_end', '0')}",
        "humanize_timing": str(settings.get("humanize_timing", "true")).lower() != "false",
        "dedup_text_only": str(settings.get("dedup_text_only", "true")).lower() != "false",
    }


# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)


def _run_coro(coro):
    """Schedule a coroutine on the main event loop from a sync context (Flask).

    Returns a concurrent.futures.Future; exceptions are logged via callback.
    """
    if state.loop is None:
        return None
    fut = asyncio.run_coroutine_threadsafe(coro, state.loop)

    def _check(f):
        try:
            f.result()
        except Exception as e:  # noqa: BLE001
            print(f"[server] coroutine error: {e!r}")

    fut.add_done_callback(_check)
    return fut


def ws_hint() -> str:
    """The URL to put in the extension popup, from the user's point of view.

    Inside Docker the container listens on WS_PORT but the browser has to use the
    *published* host port, which the container cannot know — so say so instead of
    showing a URL that would not answer.
    """
    if os.path.exists("/.dockerenv"):
        return f"ws://localhost:{WS_PORT} — in Docker use the published host port (see docker ps)"
    return f"ws://localhost:{WS_PORT}"


@app.route("/")
def dashboard():
    return DASHBOARD_HTML.replace("__WS_HINT__", ws_hint())


@app.route("/api/status")
def api_status():
    return jsonify(build_stats())


@app.route("/api/stats")
def api_stats():
    return jsonify(build_stats())


@app.route("/api/posts")
def api_posts():
    return jsonify(list_posts(1000))


@app.route("/api/settings")
def api_settings_get():
    return jsonify(get_all_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    data = request.get_json(force=True, silent=True) or {}
    update_settings(data)
    return jsonify({"ok": True, "settings": get_all_settings()})


@app.route("/api/start", methods=["POST"])
def api_start():
    set_setting("auto_mode", "true")
    _run_coro(start_auto_farm())
    _run_coro(send_settings())
    return jsonify({"ok": True, "auto_mode": True, "connected": state.connected})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    set_setting("auto_mode", "false")
    _run_coro(stop_auto_farm())
    _run_coro(send_settings())
    return jsonify({"ok": True, "auto_mode": False})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    _run_coro(run_scan_once())
    return jsonify({"ok": True, "connected": state.connected, "busy": state.scan_busy})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    reset_posts()
    _run_coro(send_ws_action({"action": "reset_dedup"}))  # also clear extension-side dedup
    return jsonify({"ok": True})


@app.route("/api/export")
def api_export():
    """Download a full backup (settings + history) as one JSON file."""
    include_images = request.args.get("include_images", "").lower() in ("1", "true", "yes")
    payload = export_state(include_images=include_images)
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    headers = {}
    if request.args.get("download", "").lower() in ("1", "true", "yes"):
        headers["Content-Disposition"] = 'attachment; filename="fb-copier-backup.json"'
    return app.response_class(body, mimetype="application/json", headers=headers)


@app.route("/api/import", methods=["POST"])
def api_import():
    """Restore a backup. ?mode=replace (default) wipes history first; merge keeps it."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid JSON body"}), 400
    mode = request.args.get("mode", "replace")
    try:
        result = import_state(data, mode=mode)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    # If the extension is connected, let it refresh its counters/dedup view.
    _run_coro(send_settings())
    _run_coro(push_stats())
    return jsonify(result)


@app.route("/api/notify-test", methods=["POST"])
def api_notify_test():
    """Send a test notification and report the provider result synchronously."""
    settings = get_all_settings()
    provider = str(settings.get("notify_provider") or "none")
    if state.loop is None:
        return jsonify({"ok": False, "provider": provider, "error": "event loop not ready yet"})
    fut = asyncio.run_coroutine_threadsafe(
        push_notify("test", "Test notification", "FB Post Copier notifications are working."),
        state.loop,
    )
    try:
        status = fut.result(timeout=20)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "provider": provider, "error": str(e)})
    return jsonify({"ok": status == "sent", "provider": provider, "status": status})


def run_flask() -> None:
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False, use_reloader=False, threaded=True)


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------
async def _send_json(ws, obj: dict) -> bool:
    try:
        await ws.send(json.dumps(obj))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[server] send failed: {e!r}")
        return False


async def send_ws_action(obj: dict) -> bool:
    """Send one message to the connected extension (False if not connected)."""
    ws = state.extension_ws
    if ws is None:
        return False
    return await _send_json(ws, obj)


async def send_scan() -> bool:
    if state.extension_ws is None:
        print("[server] scan requested but extension is not connected")
        return False
    return await _send_json(state.extension_ws, {"action": "scan"})


async def send_settings() -> bool:
    return await send_ws_action(
        {"action": "settings", "settings": get_all_settings()}
    )


async def push_stats() -> None:
    """Send the current counters to the extension so its popup stays up to date."""
    if state.extension_ws is None:
        return
    await _send_json(state.extension_ws, {"action": "stats", "stats": build_stats()})


async def push_notify(event: str, title: str, message: str, extra: dict | None = None) -> str:
    """Send a push notification without ever blocking or crashing the farm."""
    settings = get_all_settings()
    status = await asyncio.to_thread(notifier.send_push, event, title, message, settings, extra)
    if status not in ("off", "muted"):
        print(f"[server] notify [{event}]: {status}")
    return status


async def send_do_post(post_data: dict, post_id: int) -> dict | None:
    """Send a do_post to the extension and wait for the matching post_result."""
    ws = state.extension_ws
    if ws is None:
        return None

    pid_str = str(post_id)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    state.post_futures[pid_str] = fut

    payload = {"action": "do_post", "id": pid_str, "post": post_data}
    ok = await _send_json(ws, payload)
    if not ok:
        state.post_futures.pop(pid_str, None)
        return None

    try:
        return await asyncio.wait_for(fut, timeout=180)
    except asyncio.TimeoutError:
        print(f"[server] post_result timeout for id={pid_str}")
        return {"success": False, "error": "Timeout waiting for post_result"}
    finally:
        state.post_futures.pop(pid_str, None)


async def handle_post_result(msg: dict) -> None:
    pid_str = str(msg.get("id", ""))
    success = bool(msg.get("success", False))
    error = msg.get("error")
    busy = bool(msg.get("busy", False))

    # 1) Resolve any awaiting future
    fut = state.post_futures.get(pid_str)
    if fut and not fut.done():
        fut.set_result({"success": success, "error": error, "busy": busy})

    # 2) Extension busy (manual Post All running): requeue, never punish the post
    if busy:
        if pid_str.isdigit():
            update_post_status(int(pid_str), "new")
        print(f"[server] post #{pid_str} postponed: extension busy with manual posting")
        return

    # 3) Update DB if id maps to a known post row
    if pid_str.isdigit():
        post_id = int(pid_str)
        row = get_post_row(post_id)
        snippet = ((row or {}).get("text") or "").replace("\n", " ")[:80]
        status = "posted" if success else "failed"
        update_post_status(post_id, status, error)
        if success:
            state.last_post_at = int(time.time())
            clear_post_images(post_id)
            notify("FB Copier — Posted", f"Post #{post_id} posted successfully")
            await push_notify(
                "posted",
                "Post published",
                f"#{post_id}: {snippet}" if snippet else f"Post #{post_id} published",
                {"id": post_id},
            )
        else:
            state.last_error = str(error or "unknown")
            notify("FB Copier — Failed", f"Post #{post_id} failed: {error or 'unknown'}")
            await push_notify(
                "failed",
                "Post failed",
                f"#{post_id}: {error or 'unknown'}" + (f" — {snippet}" if snippet else ""),
                {"id": post_id, "error": str(error or "unknown")},
            )


async def handle_posts_found(msg: dict) -> None:
    posts = msg.get("posts") or []
    if not isinstance(posts, list):
        posts = []

    settings = get_all_settings()
    text_only = str(settings.get("dedup_text_only", "true")).lower() != "false"

    new_count = 0
    text_dupes = 0
    for p in posts:
        if not isinstance(p, dict):
            continue
        h = compute_hash(p)
        if post_exists(h):
            continue  # dedup: identical content is never stored (and never posted) twice
        if text_only:
            tn = text_norm(p.get("text"))
            # Secondary, image-independent dedup: the same text with a different
            # image URL is still the same post.
            if len(tn) >= 40 and recent_text_exists(tn):
                text_dupes += 1
                continue
        if insert_post(p, h) is not None:
            new_count += 1

    err = msg.get("error") or ""
    state.last_scan_at = int(time.time())
    state.last_error = err
    state.last_scan_new_count = new_count
    dup_note = f", {text_dupes} text-duplicate(s) skipped" if text_dupes else ""
    print(
        f"[server] posts_found: {len(posts)} received, {new_count} new{dup_note}"
        + (f" (scan error: {err})" if err else "")
    )
    # Wake the auto-farm loop, which is waiting for this scan to finish.
    state.scan_event.set()
    await push_stats()
    if new_count > 0:
        queue_now = count_by_status("new")
        await push_notify(
            "queued",
            "New posts found",
            f"{new_count} new post(s) queued (queue: {queue_now})",
            {"new": new_count, "queue": queue_now},
        )


async def process_backlog(trigger: str = "scan") -> int:
    """Post up to batch_size unposted rows, respecting cooldown and the daily cap.

    This is only called by the auto-farm loop, so posting never overlaps itself.
    It also drains posts that were found while the farm was stopped.
    """
    if state.post_lock.locked():
        print("[server] post cycle already running — skipping this round")
        return 0

    async with state.post_lock:
        if state.extension_ws is None:
            print("[server] skipping auto-post: extension disconnected")
            return 0

        settings = get_all_settings()
        batch = max(1, int_setting(settings, "batch_size", 3))
        cooldown = max(0, int_setting(settings, "cooldown_seconds", 10))
        max_per_day = max(0, int_setting(settings, "max_posts_per_day", 20))

        if not within_active_hours(settings):
            print(
                "[server] outside active hours "
                f"({settings.get('active_hours_start', '0')}-"
                f"{settings.get('active_hours_end', '0')}) — posts stay queued"
            )
            return 0

        remaining_day = max(0, max_per_day - count_posted_today())
        if remaining_day <= 0:
            print(f"[server] auto-post skipped: daily cap reached ({max_per_day})")
            return 0

        rows = fetch_backlog(min(batch, remaining_day))
        if not rows:
            return 0

        print(f"[server] auto-posting {len(rows)} post(s) [{trigger}]")
        state.posting = True
        posted = 0
        failed = 0
        postponed = False
        try:
            for row in rows:
                if state.extension_ws is None:
                    print("[server] extension disconnected mid-batch")
                    break
                if count_posted_today() >= max_per_day:
                    print("[server] daily cap reached mid-batch")
                    break
                pid = int(row["id"])
                update_post_status(pid, "posting")
                result = await send_do_post(row_payload(row), pid)
                if result is None:
                    failed += 1
                    update_post_status(pid, "failed", "Extension disconnected")
                    break
                if result.get("success"):
                    posted += 1
                elif result.get("busy"):
                    # Manual Post All is running (already logged) — requeue this post
                    # and leave it for the next cycle instead of marking it failed.
                    update_post_status(pid, "new")
                    postponed = True
                    break
                else:
                    failed += 1
                    # DB already updated by handle_post_result; keep it in sync anyway
                    update_post_status(pid, "failed", result.get("error") or "unknown error")
                if cooldown > 0:
                    delay = humanize(cooldown) if str(settings.get("humanize_timing", "true")).lower() != "false" else cooldown
                    await asyncio.sleep(delay)
        finally:
            state.posting = False

        summary = f"[server] post cycle done — {posted} posted, {failed} failed"
        if postponed:
            summary += " (postponed — retrying next cycle)"
        print(summary)
        await push_stats()
        return posted


# ---------------------------------------------------------------------------
# Auto-farm scan loop
# ---------------------------------------------------------------------------
async def run_scan_once(timeout: float = 180.0) -> bool:
    """Ask the extension for a scan and wait until it answers with posts_found."""
    ws = state.extension_ws
    if ws is None:
        print("[server] scan skipped: extension is not connected")
        return False
    if state.scan_busy:
        print("[server] scan skipped: previous scan still running")
        return False

    state.scan_busy = True
    try:
        state.scan_event.clear()
        if not await _send_json(ws, {"action": "scan"}):
            return False
        try:
            await asyncio.wait_for(state.scan_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            print(f"[server] scan timeout after {timeout:.0f}s (no posts_found)")
            return False
    finally:
        state.scan_busy = False


async def auto_scan_loop() -> None:
    try:
        while state.auto_mode:
            if state.extension_ws is None:
                # Lost the extension — pause until it reconnects.
                await asyncio.sleep(2)
                continue

            settings = get_all_settings()
            base = max(MIN_SCAN_INTERVAL_SECONDS, int_setting(settings, "scan_interval_seconds", 60))
            interval = state.scan_interval_current or base
            quiet = f", {state.scan_quiet_cycles} quiet cycle(s)" if state.scan_quiet_cycles else ""
            print(f"[server] auto-scan: scanning source group (interval={interval}s{quiet})")

            state.last_scan_new_count = 0
            answered = await run_scan_once()
            new_count = state.last_scan_new_count if answered else 0
            # Adapt to the group: slow down while it stays quiet, snap back to the base
            # interval as soon as a scan finds new posts (a batch usually means more).
            state.scan_interval_current, state.scan_quiet_cycles = adaptive_scan_interval(
                settings, interval, state.scan_quiet_cycles, new_count
            )
            if not state.auto_mode:
                break

            # Post new rows found by this scan (plus any backlog from earlier).
            await process_backlog("scan")
            if not state.auto_mode:
                break
            st = build_stats()
            cadence = f"next_scan~{state.scan_interval_current}s"
            if state.scan_quiet_cycles and state.scan_interval_current > base:
                cadence += f" (idle backoff, base {base}s)"
            print(
                f"[server] status: posted_today={st['posted_today']} total={st['posts_total']} "
                f"posted={st['posted_total']} failed={st['failed_total']} "
                f"queue={st['new_queue']} {cadence}"
            )
            delay = (
                humanize(state.scan_interval_current, 0.2)
                if str(settings.get("humanize_timing", "true")).lower() != "false"
                else state.scan_interval_current
            )
            await asyncio.sleep(max(0.0, delay))
    except asyncio.CancelledError:
        print("[server] auto-scan loop cancelled")
    except Exception as e:  # noqa: BLE001
        print(f"[server] auto-scan loop crashed: {e!r}")


async def start_auto_farm() -> None:
    if state.auto_mode and state.scan_task and not state.scan_task.done():
        return
    state.auto_mode = True
    # A fresh run starts fast: no inherited backoff from the previous session.
    state.scan_interval_current = 0
    state.scan_quiet_cycles = 0
    state.scan_task = asyncio.create_task(auto_scan_loop())
    print("[server] auto farm STARTED")


async def stop_auto_farm() -> None:
    state.auto_mode = False
    if state.scan_task and not state.scan_task.done():
        state.scan_task.cancel()
        try:
            await state.scan_task
        except asyncio.CancelledError:
            pass
    state.scan_task = None
    # No farm running means no cadence: drop the adaptive state so a stopped worker
    # reports the base interval (and the next run starts fast instead of inheriting a
    # backoff from a group that was quiet hours ago).
    state.scan_interval_current = 0
    state.scan_quiet_cycles = 0
    state.last_scan_new_count = 0
    print("[server] auto farm STOPPED")


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------
async def extension_handler(websocket, *args) -> None:
    """Handle a single extension connection (only one allowed at a time).

    ``*args`` keeps this working across websockets versions (older releases
    passed the request path as a second argument).
    """
    if state.extension_ws is not None:
        try:
            await websocket.close(
                code=1008, reason="Another extension is already connected"
            )
        except Exception:
            pass
        return

    state.extension_ws = websocket
    print("[server] extension connected")
    recover_stale_posting()

    # If auto_mode was enabled in settings, kick off the scan loop now
    if str(get_all_settings().get("auto_mode", "false")).lower() == "true":
        await start_auto_farm()

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[server] non-JSON message: {raw!r}")
                continue
            action = msg.get("action")
            if action == "hello":
                print("[server] <- hello")
                ext_settings = msg.get("settings")
                if isinstance(ext_settings, dict):
                    import_extension_settings(ext_settings)
                # Push the authoritative settings down (group URLs live in the
                # dashboard/SQLite) so the extension scans/posts the right groups.
                await _send_json(
                    websocket,
                    {"action": "settings", "settings": get_all_settings()},
                )
                await _send_json(websocket, {"action": "stats", "stats": build_stats()})
                if count_posts() == 0:
                    # Fresh/reset DB: forget the extension-side cursor so nothing
                    # stays permanently skipped after a reset or a DB wipe.
                    await _send_json(websocket, {"action": "reset_dedup"})
            elif action == "settings_update":
                data = msg.get("settings")
                if isinstance(data, dict):
                    update_settings(data)
                    print(f"[server] settings updated from extension: {sorted(data)}")
            elif action == "posts_found":
                await handle_posts_found(msg)
            elif action == "post_result":
                await handle_post_result(msg)
            elif action == "reset_dedup":
                reset_posts()
                print("[server] dedup history reset from extension")
            else:
                print(f"[server] unknown action: {action!r}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        state.extension_ws = None
        # Unblock any scan waiter so the loop reacts immediately
        state.scan_event.set()
        # Cancel any pending post futures so callers don't hang
        for fut in state.post_futures.values():
            if not fut.done():
                fut.set_result({"success": False, "error": "Extension disconnected"})
        state.post_futures.clear()
        # Stop the auto farm — extension is gone
        await stop_auto_farm()
        print("[server] extension disconnected")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main_async(autostart: bool = False) -> None:
    state.loop = asyncio.get_running_loop()
    init_db()
    recover_stale_posting()
    if autostart:
        set_setting("auto_mode", "true")
        print("[server] autostart: auto farm starts as soon as the extension connects")
    print("[server] SQLite ready at", DB_PATH)

    async with websockets.serve(
        extension_handler, WS_HOST, WS_PORT, max_size=None
    ):
        print(f"[server] WebSocket server listening on ws://{WS_HOST}:{WS_PORT}")
        await asyncio.Future()  # run forever


# ---------------------------------------------------------------------------
# Dashboard HTML (inline)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FB Post Copier — Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #f3f4f6; color: #111827; padding: 20px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  h2 { font-size: 16px; margin-bottom: 12px; color: #374151; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 16px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; max-width: 1400px; margin: 0 auto; }
  .card { background: white; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #f3f4f6; }
  .card.full { grid-column: 1 / -1; }
  .status { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; font-size: 14px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; }
  .dot.green { background: #16a34a; }
  .dot.red { background: #dc2626; }
  .dot.gray { background: #9ca3af; }
  .dot.yellow { background: #f59e0b; }
  .btn { background: #2563eb; color: white; border: none; padding: 8px 14px; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 13px; transition: background 0.15s; }
  .btn:hover { background: #1d4ed8; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn.stop { background: #dc2626; }
  .btn.stop:hover { background: #b91c1c; }
  .btn.gray { background: #6b7280; }
  .btn.gray:hover { background: #4b5563; }
  .row { display: flex; gap: 12px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
  label { display: block; font-size: 12px; color: #374151; margin-bottom: 4px; font-weight: 600; }
  input, select { width: 100%; padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 13px; }
  input:focus { outline: none; border-color: #2563eb; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #f3f4f6; vertical-align: top; }
  th { background: #f9fafb; font-weight: 600; color: #4b5563; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }
  tbody tr:hover { background: #f9fafb; }
  .badge { padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }
  .badge.new { background: #dbeafe; color: #1e40af; }
  .badge.posting { background: #fef3c7; color: #92400e; }
  .badge.posted { background: #dcfce7; color: #166534; }
  .badge.failed { background: #fee2e2; color: #991b1b; }
  .badge.gray { background: #f3f4f6; color: #4b5563; }
  .preview { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #4b5563; }
  .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .timestamp { color: #6b7280; font-size: 12px; white-space: nowrap; }
  .meta { color: #9ca3af; font-size: 12px; }
  #refreshBtn { margin-left: auto; }
  .empty { text-align: center; padding: 32px; color: #9ca3af; }
</style>
</head>
<body>
<h1>FB Post Copier</h1>
<div class="sub">Python backend &mdash; WebSocket <code>__WS_HINT__</code> &middot; Dashboard <code>http://localhost:5000</code></div>

<div class="grid">
  <div class="card">
    <h2>Connection</h2>
    <div class="row">
      <span class="status"><span id="conn-dot" class="dot gray"></span><span id="conn-text">checking…</span></span>
    </div>
    <div class="meta">Posts total: <span id="posts-total">0</span> &middot; Posted today: <span id="posted-today">0</span> &middot; Queue: <span id="queue-count">0</span> &middot; Failed: <span id="failed-count">0</span> &middot; Activity: <span id="activity">idle</span> &middot; Scan: <span id="scan-cadence">-</span></div>
  </div>

  <div class="card">
    <h2>Auto Farm</h2>
    <div class="row">
      <span class="status"><span id="auto-dot" class="dot gray"></span><span id="auto-text">stopped</span></span>
    </div>
    <div class="actions" style="margin-top:8px;">
      <button class="btn" id="btn-start" onclick="startFarm()">Start</button>
      <button class="btn stop" id="btn-stop" onclick="stopFarm()">Stop</button>
      <button class="btn gray" onclick="scanNow()">Scan Now</button>
      <button class="btn gray" onclick="resetHistory()">Reset</button>
    </div>
  </div>

  <div class="card full">
    <h2>Settings</h2>
    <div class="form-grid">
      <div><label>Source Group URL</label><input id="source_group_url" placeholder="https://www.facebook.com/groups/..."></div>
      <div><label>Target Group URL</label><input id="target_group_url" placeholder="https://www.facebook.com/groups/..."></div>
      <div><label>Scan Interval (seconds, min 60)</label><input id="scan_interval_seconds" type="number" min="60"><div class="meta">The extension never scans the feed faster than this — Facebook's feed does not change every few seconds, and scanning too often just re-reads the same posts. Hard floor: 60 s.</div></div>
      <div><label>Max Scan Interval (seconds — idle backoff)</label><input id="scan_interval_max_seconds" type="number" min="0"><div class="meta">While scan after scan finds nothing, the worker slows down (×1.5 per cycle) up to this value and snaps straight back to the base interval as soon as new posts appear. 0 turns the backoff off.</div></div>
      <div><label>Max Posts / 24h</label><input id="max_posts_per_day" type="number" min="0"></div>
      <div><label>Cooldown (seconds)</label><input id="cooldown_seconds" type="number" min="0"></div>
      <div><label>Batch Size</label><input id="batch_size" type="number" min="1"></div>
      <div><label>Timing Jitter (anti-clockwork)</label><select id="humanize_timing"><option value="true">on</option><option value="false">off</option></select></div>
      <div><label>Text-only Dedup</label><select id="dedup_text_only"><option value="true">on</option><option value="false">off</option></select></div>
      <div><label>Active Hours Start (0 = off)</label><input id="active_hours_start" type="number" min="0" max="23"></div>
      <div><label>Active Hours End</label><input id="active_hours_end" type="number" min="0" max="23"></div>
      <div><label>Notifications (push)</label><select id="notify_provider">
        <option value="none">off</option><option value="ntfy">ntfy</option>
        <option value="telegram">telegram</option><option value="webhook">webhook</option>
        <option value="knock">knock</option></select></div>
      <div><label>Notify when</label>
        <div style="display:flex;gap:6px">
          <select id="notify_on_posted"><option value="true">posted ✓</option><option value="false">posted ✗</option></select>
          <select id="notify_on_failed"><option value="true">failed ✓</option><option value="false">failed ✗</option></select>
          <select id="notify_on_queued"><option value="true">queued ✓</option><option value="false">queued ✗</option></select>
        </div></div>
      <div><label>ntfy URL</label><input id="notify_ntfy_url" placeholder="https://ntfy.sh/my-topic"></div>
      <div><label>Webhook URL (Discord / Slack / generic)</label><input id="notify_webhook_url" placeholder="https://…"></div>
      <div><label>Telegram chat ID (token via FB_COPIER_TELEGRAM_TOKEN)</label><input id="notify_telegram_chat_id" placeholder="123456789"></div>
    </div>
    <div class="row" style="margin-top:14px;">
      <button class="btn" onclick="saveSettings()">Save Settings</button>
      <button class="btn gray" onclick="testNotify()">🔔 Test notification</button>
      <span id="save-msg" class="meta"></span>
    </div>
  </div>

  <div class="card full">
    <h2>Backup &amp; Move</h2>
    <div class="meta" style="margin-bottom:10px;">One file holds every setting and the full post history (posted, failed, queue) — import it on another machine to pick up exactly where you left off.</div>
    <div class="actions">
      <button class="btn" onclick="exportBackup()">⬇ Export backup</button>
      <button class="btn gray" onclick="document.getElementById('import-file').click()">⬆ Import backup</button>
      <input id="import-file" type="file" accept=".json,application/json" style="display:none" onchange="importBackup(this)">
      <label style="display:flex;align-items:center;gap:6px;margin:0;font-weight:600;">
        <input type="checkbox" id="import-merge" style="width:auto;"> merge (keep current history)
      </label>
      <span id="backup-msg" class="meta"></span>
    </div>
  </div>

  <div class="card full">
    <div class="row">
      <h2 style="margin:0;">Posts</h2>
      <span id="posts-count" class="meta"></span>
      <button class="btn gray" id="refreshBtn" onclick="loadPosts()">Refresh</button>
    </div>
    <div style="overflow-x:auto; margin-top:10px;">
      <table>
        <thead><tr><th>ID</th><th>Text</th><th>Author</th><th>Link</th><th>Status</th><th>Images</th><th>Found</th><th>Posted</th></tr></thead>
        <tbody id="posts-body">
          <tr><td colspan="8" class="empty">Loading…</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}
function badge(status) {
  const ok = ['new','posting','posted','failed'].includes(status);
  return '<span class="badge ' + (ok ? status : 'gray') + '">' + (status || '-') + '</span>';
}
function esc(s) {
  return String(s || '').replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    const connDot = document.getElementById('conn-dot');
    const connText = document.getElementById('conn-text');
    connDot.className = 'dot ' + (s.connected ? 'green' : 'red');
    connText.textContent = s.connected ? 'connected' : 'disconnected';
    document.getElementById('posts-total').textContent = s.posts_total || 0;
    document.getElementById('posted-today').textContent = s.posted_today || 0;
    document.getElementById('queue-count').textContent = s.new_queue || 0;
    document.getElementById('failed-count').textContent = s.failed_total || 0;
    document.getElementById('activity').textContent = s.posting ? 'posting…' : (s.scan_busy ? 'scanning…' : 'idle');
    const cadence = document.getElementById('scan-cadence');
    if (cadence) {
      const base = s.scan_interval_seconds || 0;
      const cur = s.scan_interval_current || base;
      if (!s.auto_mode) cadence.textContent = 'stopped';
      else if (cur > base) cadence.textContent = 'every ~' + cur + 's (idle backoff from ' + base + 's)';
      else cadence.textContent = 'every ~' + cur + 's';
    }
    const autoDot = document.getElementById('auto-dot');
    const autoText = document.getElementById('auto-text');
    autoDot.className = 'dot ' + (s.auto_mode ? (s.connected ? 'green' : 'yellow') : 'gray');
    autoText.textContent = s.auto_mode ? (s.connected ? 'running' : 'waiting for extension') : 'stopped';
  } catch (e) { console.error('status:', e); }
}
async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    const s = await r.json();
    document.getElementById('source_group_url').value = s.source_group_url || '';
    document.getElementById('target_group_url').value = s.target_group_url || '';
    document.getElementById('scan_interval_seconds').value = s.scan_interval_seconds || 60;
    document.getElementById('scan_interval_max_seconds').value = (s.scan_interval_max_seconds === undefined || s.scan_interval_max_seconds === '') ? 900 : s.scan_interval_max_seconds;
    document.getElementById('max_posts_per_day').value = s.max_posts_per_day || 20;
    document.getElementById('cooldown_seconds').value = s.cooldown_seconds || 10;
    document.getElementById('batch_size').value = s.batch_size || 3;
    document.getElementById('humanize_timing').value = (s.humanize_timing === 'false') ? 'false' : 'true';
    document.getElementById('dedup_text_only').value = (s.dedup_text_only === 'false') ? 'false' : 'true';
    document.getElementById('active_hours_start').value = s.active_hours_start || 0;
    document.getElementById('active_hours_end').value = s.active_hours_end || 0;
    document.getElementById('notify_provider').value = s.notify_provider || 'none';
    document.getElementById('notify_on_posted').value = (s.notify_on_posted === 'false') ? 'false' : 'true';
    document.getElementById('notify_on_failed').value = (s.notify_on_failed === 'false') ? 'false' : 'true';
    document.getElementById('notify_on_queued').value = (s.notify_on_queued === 'false') ? 'false' : 'true';
    document.getElementById('notify_ntfy_url').value = s.notify_ntfy_url || '';
    document.getElementById('notify_webhook_url').value = s.notify_webhook_url || '';
    document.getElementById('notify_telegram_chat_id').value = s.notify_telegram_chat_id || '';
  } catch (e) { console.error('settings:', e); }
}
async function loadPosts() {
  try {
    const r = await fetch('/api/posts');
    const arr = await r.json();
    const body = document.getElementById('posts-body');
    document.getElementById('posts-count').textContent = (arr.length || 0) + ' rows';
    if (!arr.length) {
      body.innerHTML = '<tr><td colspan="8" class="empty">No posts yet — click "Scan Now" or start the auto farm.</td></tr>';
      return;
    }
    let html = '';
    for (const p of arr) {
      const txt = esc((p.text || '').substring(0, 120));
      const link = p.link ? '<a href="' + esc(p.link) + '" target="_blank" rel="noopener">link</a>' : '-';
      html += '<tr>'
        + '<td>' + p.id + '</td>'
        + '<td class="preview" title="' + esc(p.text || '') + '">' + (txt || '<span class="meta">—</span>') + '</td>'
        + '<td>' + esc(p.author || '-') + '</td>'
        + '<td>' + link + '</td>'
        + '<td>' + badge(p.status) + (p.error ? '<div class="meta">' + esc(p.error.substring(0,40)) + '</div>' : '') + '</td>'
        + '<td>' + (p.images_count || 0) + '</td>'
        + '<td class="timestamp">' + fmtTime(p.found_at) + '</td>'
        + '<td class="timestamp">' + fmtTime(p.posted_at) + '</td>'
        + '</tr>';
    }
    body.innerHTML = html;
  } catch (e) { console.error('posts:', e); }
}
async function saveSettings() {
  const data = {
    source_group_url: document.getElementById('source_group_url').value,
    target_group_url: document.getElementById('target_group_url').value,
    scan_interval_seconds: Math.max(60, parseInt(document.getElementById('scan_interval_seconds').value, 10) || 60),
    scan_interval_max_seconds: Math.max(0, parseInt(document.getElementById('scan_interval_max_seconds').value, 10) || 0),
    max_posts_per_day: document.getElementById('max_posts_per_day').value,
    cooldown_seconds: document.getElementById('cooldown_seconds').value,
    batch_size: document.getElementById('batch_size').value,
    humanize_timing: document.getElementById('humanize_timing').value,
    dedup_text_only: document.getElementById('dedup_text_only').value,
    active_hours_start: document.getElementById('active_hours_start').value,
    active_hours_end: document.getElementById('active_hours_end').value,
    notify_provider: document.getElementById('notify_provider').value,
    notify_on_posted: document.getElementById('notify_on_posted').value,
    notify_on_failed: document.getElementById('notify_on_failed').value,
    notify_on_queued: document.getElementById('notify_on_queued').value,
    notify_ntfy_url: document.getElementById('notify_ntfy_url').value,
    notify_webhook_url: document.getElementById('notify_webhook_url').value,
    notify_telegram_chat_id: document.getElementById('notify_telegram_chat_id').value,
  };
  const msg = document.getElementById('save-msg');
  msg.textContent = 'saving…';
  try {
    const r = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
    if (r.ok) { msg.textContent = 'saved ✓'; }
    else { msg.textContent = 'failed ✗'; }
  } catch (e) { msg.textContent = 'error: ' + e.message; }
  setTimeout(function(){ msg.textContent = ''; }, 3000);
}
async function testNotify() {
  try {
    const r = await fetch('/api/notify-test', {method:'POST'});
    const j = await r.json();
    alert(j.ok ? ('🔔 Test notification sent via ' + j.provider) : ('Notification failed: ' + JSON.stringify(j)));
  } catch (e) { alert('Notification test error: ' + e.message); }
}
async function startFarm() { await fetch('/api/start', {method:'POST'}); loadStatus(); }
async function stopFarm() { await fetch('/api/stop', {method:'POST'}); loadStatus(); }
async function scanNow() {
  const r = await fetch('/api/scan', {method:'POST'});
  const j = await r.json();
  if (!j.connected) { alert('Extension is not connected — load it in Firefox first.'); return; }
  setTimeout(loadPosts, 5000);
}
async function resetHistory() {
  if (!confirm('Clear all posts from the database?')) return;
  await fetch('/api/reset', {method:'POST'});
  loadPosts(); loadStatus();
}
async function exportBackup() {
  const msg = document.getElementById('backup-msg');
  msg.textContent = 'exporting…';
  try {
    const r = await fetch('/api/export?download=1');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'fb-copier-backup.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    msg.textContent = 'backup downloaded ✓';
  } catch (e) { msg.textContent = 'export error: ' + e.message; }
  setTimeout(function(){ msg.textContent = ''; }, 4000);
}
async function importBackup(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const merge = document.getElementById('import-merge').checked;
  const mode = merge ? 'merge' : 'replace';
  if (!merge && !confirm('Replace the current settings and history with the backup?')) { input.value = ''; return; }
  const msg = document.getElementById('backup-msg');
  msg.textContent = 'importing…';
  try {
    const text = await file.text();
    const r = await fetch('/api/import?mode=' + mode, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: text
    });
    const j = await r.json();
    msg.textContent = j.ok
      ? ('restored ' + j.posts_inserted + ' post(s), ' + j.settings_imported + ' setting(s) ✓')
      : ('import failed: ' + JSON.stringify(j));
    loadSettings(); loadPosts(); loadStatus();
  } catch (e) { msg.textContent = 'import error: ' + e.message; }
  input.value = '';
}

setInterval(loadStatus, 3000);
setInterval(loadPosts, 5000);
loadSettings();
loadStatus();
loadPosts();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point (must come AFTER DASHBOARD_HTML is defined at module scope)
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FB Post Copier backend — WebSocket worker for the Firefox extension "
        "plus an optional dashboard.",
    )
    parser.add_argument("--headless", action="store_true",
                        help="run without the Flask dashboard (WebSocket + automation only)")
    parser.add_argument("--autostart", action="store_true",
                        help="start the auto farm as soon as the extension connects")
    parser.add_argument("--ws-host", default=WS_HOST, help=f"WebSocket bind host (default: {WS_HOST})")
    parser.add_argument("--ws-port", type=int, default=WS_PORT, help=f"WebSocket port (default: {WS_PORT})")
    parser.add_argument("--http-host", default=HTTP_HOST, help=f"dashboard bind host (default: {HTTP_HOST})")
    parser.add_argument("--http-port", type=int, default=HTTP_PORT, help=f"dashboard port (default: {HTTP_PORT})")
    return parser.parse_args()


def main() -> None:
    global WS_HOST, WS_PORT, HTTP_HOST, HTTP_PORT
    args = parse_args()
    WS_HOST, WS_PORT = args.ws_host, args.ws_port
    HTTP_HOST, HTTP_PORT = args.http_host, args.http_port

    autostart = args.autostart or os.environ.get("FB_COPIER_AUTOSTART", "").lower() in ("1", "true", "yes")

    if args.headless:
        print("[server] headless mode: dashboard and fbctl are disabled")
    else:
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print(f"[server] dashboard: http://{HTTP_HOST}:{HTTP_PORT}  (terminal: python fbctl.py status)")

    try:
        asyncio.run(main_async(autostart=autostart))
    except KeyboardInterrupt:
        print("\n[server] shutting down")


if __name__ == "__main__":
    main()
