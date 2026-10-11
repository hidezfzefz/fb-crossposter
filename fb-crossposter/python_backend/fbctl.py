#!/usr/bin/env python3
"""
fbctl — control the FB Post Copier backend from a terminal (no dashboard needed)
================================================================================

Talks to the backend's HTTP API (dashboard mode, default port 5000):

    python fbctl.py status
    python fbctl.py stats
    python fbctl.py posts --limit 20
    python fbctl.py start
    python fbctl.py stop
    python fbctl.py scan
    python fbctl.py reset
    python fbctl.py settings
    python fbctl.py settings scan_interval_seconds=30 max_posts_per_day=10
    python fbctl.py export backup.json          # settings + history -> one file
    python fbctl.py import backup.json --merge  # restore (default mode: replace)

Use another host/port with ``--url`` or ``FB_COPIER_HTTP_URL``.
Headless mode has no HTTP API — read the server log instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def base_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url.rstrip("/")
    env = os.environ.get("FB_COPIER_HTTP_URL")
    if env:
        return env.rstrip("/")
    host = os.environ.get("FB_COPIER_HTTP_HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = os.environ.get("FB_COPIER_HTTP_PORT", "5000")
    return f"http://{host}:{port}"


def api(args: argparse.Namespace, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    url = base_url(args) + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
        return json.loads(body) if body else {}
    except urllib.error.URLError as e:
        print(f"error: cannot reach {url} ({e.reason})")
        print("Is the backend running in dashboard mode? Headless mode has no HTTP API.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"error: unexpected response from {url}")
        sys.exit(1)


def fmt_uptime(seconds) -> str:
    if not seconds:
        return "-"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {sec}s" if m else f"{sec}s"


def cmd_status(args: argparse.Namespace) -> None:
    s = api(args, "/api/status")
    connected = bool(s.get("connected"))
    print(f"{'●' if connected else '○'} extension: {'connected' if connected else 'NOT connected'}")
    state = "RUNNING" if s.get("auto_mode") else "stopped"
    if s.get("posting"):
        state += " (posting…)"
    elif s.get("scan_busy"):
        state += " (scanning…)"
    print(f"  auto farm:  {state}")
    print(f"  posts:      total={s.get('posts_total')}  posted={s.get('posted_total')}  "
          f"failed={s.get('failed_total')}  queue={s.get('new_queue')}")
    print(f"  today:      {s.get('posted_today')} posted (rolling 24h)")
    cur = s.get("scan_interval_current") or s.get("scan_interval_seconds") or 0
    base = s.get("scan_interval_seconds") or 0
    quiet = s.get("scan_quiet_cycles") or 0
    cadence = f"every {cur}s" if cur else "-"
    if cur and base and cur > base:
        cadence += f" (idle backoff from {base}s after {quiet} quiet cycle(s))"
    print(f"  scanning:   {cadence}")
    print(f"  extras:     active_hours={s.get('active_hours')}  "
          f"jitter={'on' if s.get('humanize_timing') else 'off'}  "
          f"text_dedup={'on' if s.get('dedup_text_only') else 'off'}")
    print(f"  uptime:     {fmt_uptime(s.get('uptime_seconds'))}")
    if s.get("last_error"):
        print(f"  last error: {s['last_error']}")


def cmd_stats(args: argparse.Namespace) -> None:
    for k, v in sorted(api(args, "/api/stats").items()):
        print(f"{k:18s} {v}")


def cmd_posts(args: argparse.Namespace) -> None:
    posts = api(args, "/api/posts")[: args.limit]
    if not posts:
        print("no posts yet")
        return
    for p in posts:
        text = (p.get("text") or "").replace("\n", " ")[:50]
        err = f"  ({p['error']})" if p.get("error") else ""
        print(f"#{p['id']:<5} {p['status']:<7} {text}{err}")


def cmd_action(args: argparse.Namespace, path: str) -> None:
    print("ok: " + json.dumps(api(args, path, method="POST")))


def cmd_export(args: argparse.Namespace) -> None:
    path = args.path or f"fb-copier-backup-{int(time.time())}.json"
    query = "?include_images=1" if args.images else ""
    data = api(args, "/api/export" + query)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    posts = data.get("posts") or []
    settings = data.get("settings") or {}
    print(f"ok: exported {len(posts)} post(s) + {len(settings)} setting(s) -> {path}")


def cmd_import(args: argparse.Namespace) -> None:
    try:
        with open(args.path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as e:
        print(f"error: cannot read {args.path} ({e})")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"error: {args.path} is not valid JSON ({e})")
        sys.exit(1)
    result = api(args, f"/api/import?mode={args.mode}", method="POST", payload=data)
    if result.get("ok"):
        print(
            f"ok: {result['mode']} import — {result['posts_inserted']} post(s) restored, "
            f"{result['posts_skipped']} skipped, {result['settings_imported']} setting(s) applied"
        )
    else:
        print("import failed: " + json.dumps(result))
        sys.exit(1)


def cmd_settings(args: argparse.Namespace) -> None:
    if not args.pairs:
        for k, v in sorted(api(args, "/api/settings").items()):
            print(f"{k:24s} {v}")
        return
    payload = {}
    for pair in args.pairs:
        if "=" not in pair:
            print(f"error: expected KEY=VALUE, got {pair!r}")
            sys.exit(2)
        key, value = pair.split("=", 1)
        payload[key.strip()] = value.strip()
    api(args, "/api/settings", method="POST", payload=payload)
    print(f"ok: updated {sorted(payload)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Control the FB Post Copier backend.")
    parser.add_argument("--url", default="", help="backend base URL (default http://127.0.0.1:5000)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="connection state + counters")
    sub.add_parser("stats", help="raw stats dictionary")
    p_posts = sub.add_parser("posts", help="recent posts")
    p_posts.add_argument("--limit", type=int, default=20)
    sub.add_parser("start", help="start the auto farm")
    sub.add_parser("stop", help="stop the auto farm")
    sub.add_parser("scan", help="trigger one scan now")
    sub.add_parser("reset", help="clear the post/dedup database")
    sub.add_parser("notify-test", help="send a test notification via the configured provider")
    p_exp = sub.add_parser("export", help="write settings + history to a backup file")
    p_exp.add_argument("path", nargs="?", default="", help="output file (default fb-copier-backup-<ts>.json)")
    p_exp.add_argument("--images", action="store_true",
                       help="also include pending images (larger file)")
    p_imp = sub.add_parser("import", help="restore a backup file")
    p_imp.add_argument("path", help="backup file to restore")
    p_imp.add_argument("--merge", dest="mode", action="store_const", const="merge",
                       default="replace", help="keep existing history (default: replace it)")
    p_set = sub.add_parser("settings", help="show or update settings")
    p_set.add_argument("pairs", nargs="*", help="KEY=VALUE pairs to update")

    args = parser.parse_args()
    if args.command == "status":
        cmd_status(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "posts":
        cmd_posts(args)
    elif args.command == "start":
        cmd_action(args, "/api/start")
    elif args.command == "stop":
        cmd_action(args, "/api/stop")
    elif args.command == "scan":
        cmd_action(args, "/api/scan")
    elif args.command == "reset":
        cmd_action(args, "/api/reset")
    elif args.command == "notify-test":
        result = api(args, "/api/notify-test", method="POST")
        if result.get("ok"):
            print(f"🔔 test notification sent via {result.get('provider')}")
        else:
            print(f"notification failed: {json.dumps(result)}")
            sys.exit(1)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "import":
        cmd_import(args)
    elif args.command == "settings":
        cmd_settings(args)


if __name__ == "__main__":
    main()
