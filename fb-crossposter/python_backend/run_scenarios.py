#!/usr/bin/env python3
"""
run_scenarios.py — replay every simulator scenario against a real backend and assert the result
==============================================================================================

This is the regression net for the parts that are easy to break silently:

  dedup     the same post served twice is stored and posted once
  textdup   the same text with a different image URL is skipped (30-day text dedup)
  backlog   posts found while the farm is stopped are queued, then posted when it starts
  fail      a failing post is recorded as failed, the rest still post
  busy      a "manual Post All in progress" answer requeues instead of failing
  backup    settings + history survive an export → reset → import round trip
  notify    push events fire for queued/failed/posted and the mute toggles work
  adaptive  the scan interval backs off on a quiet feed and snaps back on new posts

It boots the backend in a throwaway data directory on free ports, drives it through
the same HTTP API the dashboard uses, and runs the fake extension from
``simulate_extension.py`` for the farm scenarios.

    python3 run_scenarios.py                 # everything (~2 min)
    python3 run_scenarios.py dedup textdup   # a subset

Exit code 0 = all assertions held, 1 = at least one failed. CI runs this on every push.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_SOURCE = "https://www.facebook.com/groups/1144780033953810"
DEFAULT_TARGET = "https://www.facebook.com/groups/1770877160904592"

# Safe, fast settings for every scenario: no jitter, no cooldown, generous caps.
BASE_SETTINGS = {
    "scan_interval_seconds": "10",
    "cooldown_seconds": "0",
    "batch_size": "5",
    "max_posts_per_day": "50",
    "humanize_timing": "false",
    "dedup_text_only": "true",
    "active_hours_start": "0",
    "active_hours_end": "0",
    "notify_provider": "none",
}


# ---------------------------------------------------------------------------
# Tiny assertion helper
# ---------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checks = 0

    def check(self, label: str, got, expected) -> None:
        self.checks += 1
        if got == expected:
            print(f"    ok   {label}: {got!r}")
        else:
            print(f"    FAIL {label}: got {got!r}, expected {expected!r}")
            self.failures.append(f"{label}: got {got!r}, expected {expected!r}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Backend under test
# ---------------------------------------------------------------------------
class Backend:
    def __init__(self) -> None:
        self.ws_port = free_port()
        self.http_port = free_port()
        self.tmpdir = tempfile.mkdtemp(prefix="fb-copier-ci-")
        self.proc: subprocess.Popen | None = None
        self.log_path = os.path.join(self.tmpdir, "server.log")
        self._log = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}"

    def api(self, path: str, method: str = "GET", payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode()
        return json.loads(body) if body else {}

    def try_api(self, path: str) -> dict | None:
        try:
            return self.api(path)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None

    def start(self) -> None:
        env = dict(os.environ)
        env.update({
            "FB_COPIER_DATA_DIR": self.tmpdir,
            "FB_COPIER_WS_HOST": "127.0.0.1",
            "FB_COPIER_WS_PORT": str(self.ws_port),
            "FB_COPIER_HTTP_HOST": "127.0.0.1",
            "FB_COPIER_HTTP_PORT": str(self.http_port),
            "PYTHONUNBUFFERED": "1",
        })
        env.pop("FB_COPIER_AUTOSTART", None)
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=HERE, env=env, stdout=self._log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.time() + 40
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited early:\n" + self.tail())
            if self.try_api("/api/status") is not None:
                print(f"[ci] backend up on {self.base} (ws {self.ws_url}, data {self.tmpdir})")
                return
            time.sleep(0.4)
        raise RuntimeError("server did not answer /api/status in time:\n" + self.tail())

    def tail(self, lines: int = 60) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return "\n".join(fh.read().splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self._log:
            self._log.close()

    def wait_for(self, label: str, predicate, timeout: float = 75.0) -> dict:
        """Poll /api/stats until predicate(stats) is true; return the last stats."""
        deadline = time.time() + timeout
        stats = {}
        while time.time() < deadline:
            stats = self.api("/api/stats")
            if predicate(stats):
                return stats
            time.sleep(1.0)
        raise TimeoutError(f"timed out waiting for {label} (last stats: {stats})")

    def wait_connected(self, timeout: float = 30.0) -> None:
        """The fake extension connects asynchronously — wait before driving it."""
        self.wait_for("extension connect", lambda s: s["connected"], timeout=timeout)

    def reset(self) -> None:
        self.api("/api/reset", method="POST")
        self.api("/api/settings", method="POST", payload=dict(BASE_SETTINGS))
        stats = self.api("/api/stats")
        if stats.get("posts_total"):
            raise RuntimeError(f"reset left {stats['posts_total']} rows behind")


# ---------------------------------------------------------------------------
# Local notification receiver (reuses the project's mock_notify_receiver.py)
# ---------------------------------------------------------------------------
class MockReceiver:
    def __init__(self, backend: Backend) -> None:
        self.port = free_port()
        self.log = os.path.join(backend.tmpdir, "notify.log")
        self.proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def start(self) -> None:
        open(self.log, "w").close()
        self.proc = subprocess.Popen(
            [sys.executable, "mock_notify_receiver.py", "--port", str(self.port), "--log", self.log],
            cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    print(f"[ci] mock notify receiver on 127.0.0.1:{self.port}")
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("mock notify receiver did not start")

    def events(self) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self.log, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        body = json.loads(record.get("body") or "{}")
                    except json.JSONDecodeError:
                        continue
                    out.append({
                        "event": body.get("event"),
                        "path": record.get("path"),
                        "body": body,
                        "headers": record.get("headers") or {},
                    })
        except OSError:
            pass
        return out

    def clear(self) -> None:
        open(self.log, "w").close()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Fake extension
# ---------------------------------------------------------------------------
class Sim:
    """Runs simulate_extension in a thread so the harness can keep polling."""

    def __init__(self, backend: Backend, mode: str, linger: float = 12.0,
                 max_seconds: float = 0.0, **kw) -> None:
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.args = argparse.Namespace(
            url=backend.ws_url, mode=mode, linger=linger, post_delay=0.0,
            max_seconds=max_seconds,
            fail_first=kw.get("fail_first", 0), busy_first=kw.get("busy_first", 0),
            source=DEFAULT_SOURCE, target=DEFAULT_TARGET,
            update_target=kw.get("update_target", ""),
        )

    def start(self) -> None:
        import simulate_extension

        def runner() -> None:
            try:
                asyncio.run(simulate_extension.run(self.args))
            except BaseException as e:  # pragma: no cover - surfaced by join()
                self.error = e

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def join(self, timeout: float = 90.0) -> None:
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise RuntimeError(
                    f"simulator ({self.args.mode}) still running after {timeout:.0f}s"
                )
        if self.error:
            raise RuntimeError(f"simulator ({self.args.mode}) crashed: {self.error!r}")


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
def scenario_dedup(b: Backend, r: Report) -> None:
    """3 distinct posts across 2 scan rounds while one repeats → 3 rows, all posted."""
    b.reset()
    sim = Sim(b, "dedup", linger=6.0, max_seconds=75.0)
    sim.start()
    try:
        b.wait_connected()
        b.api("/api/start", method="POST")
        stats = b.wait_for("dedup posts", lambda s: s["posted_total"] >= 3 and s["new_queue"] == 0)
        r.check("posts_total", stats["posts_total"], 3)
        r.check("posted_total", stats["posted_total"], 3)
        r.check("failed_total", stats["failed_total"], 0)
        texts = sorted((p.get("text") or "") for p in b.api("/api/posts"))
        r.check("stored texts", len(texts), 3)
        r.check("no duplicate Beta", sum("Beta" in t for t in texts), 1)
    finally:
        sim.join()
        b.api("/api/stop", method="POST")


def scenario_textdup(b: Backend, r: Report) -> None:
    """Same 69-char text re-served with a different image URL → stored once."""
    b.reset()
    sim = Sim(b, "textdup", linger=6.0, max_seconds=60.0)
    sim.start()
    try:
        b.wait_connected()
        b.api("/api/start", method="POST")
        b.wait_for("textdup posted", lambda s: s["posted_total"] >= 1)
        # Sleep past the next scan interval so the second round (same text, new image
        # URL) reaches the backend before we assert that it was skipped.
        time.sleep(14)
        stats = b.api("/api/stats")
        r.check("posts_total", stats["posts_total"], 1)
        r.check("posted_total", stats["posted_total"], 1)
        r.check("failed_total", stats["failed_total"], 0)
    finally:
        sim.join()
        b.api("/api/stop", method="POST")


def scenario_backlog(b: Backend, r: Report) -> None:
    """Found while stopped → queued; starting the farm drains it."""
    b.reset()
    sim = Sim(b, "backlog", linger=6.0, max_seconds=60.0)
    sim.start()
    try:
        b.wait_connected()
        # Farm is stopped: one manual scan queues the post.
        b.api("/api/scan", method="POST")
        stats = b.wait_for("backlog queued", lambda s: s["new_queue"] >= 1, timeout=45)
        r.check("queued while stopped", stats["new_queue"], 1)
        r.check("nothing posted yet", stats["posted_total"], 0)
        b.api("/api/start", method="POST")
        stats = b.wait_for("backlog posted", lambda s: s["posted_total"] >= 1 and s["new_queue"] == 0)
        r.check("posted after start", stats["posted_total"], 1)
        r.check("failed_total", stats["failed_total"], 0)
    finally:
        sim.join()
        b.api("/api/stop", method="POST")


def scenario_fail(b: Backend, r: Report) -> None:
    """The first do_post fails: recorded as failed, the others still post."""
    b.reset()
    sim = Sim(b, "fail", linger=6.0, max_seconds=60.0, fail_first=1)
    sim.start()
    try:
        b.wait_connected()
        b.api("/api/start", method="POST")
        stats = b.wait_for("fail scenario", lambda s: s["failed_total"] >= 1 and s["posted_total"] >= 2)
        r.check("posts_total", stats["posts_total"], 3)
        r.check("posted_total", stats["posted_total"], 2)
        r.check("failed_total", stats["failed_total"], 1)
        r.check("queue empty", stats["new_queue"], 0)
        errs = [p.get("error") for p in b.api("/api/posts") if p["status"] == "failed"]
        r.check("failure reason stored", bool(errs and errs[0]), True)
    finally:
        sim.join()
        b.api("/api/stop", method="POST")


def scenario_busy(b: Backend, r: Report) -> None:
    """A busy answer (manual Post All running) requeues instead of failing."""
    b.reset()
    sim = Sim(b, "backlog", linger=6.0, max_seconds=60.0, busy_first=1)
    sim.start()
    try:
        b.wait_connected()
        b.api("/api/start", method="POST")
        stats = b.wait_for("busy then posted", lambda s: s["posted_total"] >= 1 and s["new_queue"] == 0)
        r.check("posts_total", stats["posts_total"], 1)
        r.check("posted_total", stats["posted_total"], 1)
        r.check("failed_total (busy is not a failure)", stats["failed_total"], 0)
    finally:
        sim.join()
        b.api("/api/stop", method="POST")


def scenario_backup(b: Backend, r: Report) -> None:
    """Export → reset → import restores settings, history and dedup state."""
    b.reset()
    marker = "https://www.facebook.com/groups/999000111222333"
    b.api("/api/settings", method="POST", payload={
        "source_group_url": marker,
        "scan_interval_seconds": "77",
    })

    # One queued row so the history part of the backup has something to carry.
    sim = Sim(b, "backlog", linger=6.0, max_seconds=15.0)
    sim.start()
    try:
        b.wait_connected()
        b.api("/api/scan", method="POST")
        b.wait_for("backup seed", lambda s: s["new_queue"] >= 1, timeout=45)
    finally:
        sim.join(timeout=45)
        b.api("/api/stop", method="POST")

    before = b.api("/api/stats")
    backup = b.api("/api/export")
    r.check("export version", backup.get("version"), 1)
    r.check("export settings present", isinstance(backup.get("settings"), dict), True)
    r.check("export post count", len(backup.get("posts") or []), before["posts_total"])
    r.check("export keeps text_norm for dedup",
            all(p["text_norm"] for p in backup["posts"] if p.get("text")), True)

    # Wipe everything, then change a setting so we can prove it gets restored.
    b.api("/api/reset", method="POST")
    b.api("/api/settings", method="POST", payload={
        "source_group_url": DEFAULT_SOURCE,
        "scan_interval_seconds": "5",
    })
    r.check("wiped before import", b.api("/api/stats")["posts_total"], 0)
    r.check("setting changed", b.api("/api/settings")["source_group_url"], DEFAULT_SOURCE)

    # The dashboard posts the raw file body, so send the exact export back.
    restored = b.api("/api/import?mode=replace", method="POST", payload=backup)
    r.check("import ok", restored.get("ok"), True)
    r.check("rows restored", restored.get("posts_inserted"), before["posts_total"])
    r.check("settings restored", b.api("/api/settings")["source_group_url"], marker)
    r.check("scan interval restored", b.api("/api/settings")["scan_interval_seconds"], "77")
    r.check("posts_total after import", b.api("/api/stats")["posts_total"], before["posts_total"])

    # Importing twice must not duplicate anything (merge = dedup-aware).
    again = b.api("/api/import?mode=merge", method="POST", payload=backup)
    r.check("merge import inserts nothing new", again.get("posts_inserted"), 0)
    r.check("merge import keeps the total", b.api("/api/stats")["posts_total"], before["posts_total"])

    # A bad file must be rejected cleanly, not crash the dashboard.
    try:
        b.api("/api/import?mode=replace", method="POST", payload={"nonsense": True})
        r.check("garbage import rejected", "no error", "HTTP 400")
    except urllib.error.HTTPError as e:
        r.check("garbage import rejected", e.code, 400)


def scenario_adaptive(b: Backend, r: Report) -> None:
    """The scan cadence follows the group: stretch while nothing appears, snap back on a
    batch of new posts. The interval logic is a pure function, so this asserts the real
    sequence the loop walks through instead of waiting out minutes of timing."""
    import server as worker

    def step(seconds: str, current: int, quiet: int, new: int, ceiling: str = "900"):
        settings = {"scan_interval_seconds": seconds, "scan_interval_max_seconds": ceiling}
        return worker.adaptive_scan_interval(settings, current, quiet, new)

    # base 60s, ceiling 900s: two quiet cycles at base, then ×1.5 each quiet cycle.
    r.check("a fresh run scans at the base interval", step("60", 0, 0, 5), (60, 0))
    r.check("first quiet cycle: still at base", step("60", 60, 0, 0), (60, 1))
    r.check("second quiet cycle: stretches to ×1.5", step("60", 60, 1, 0), (90, 2))
    r.check("keeps stretching", step("60", 90, 2, 0), (135, 3))
    r.check("…", step("60", 135, 3, 0), (202, 4))
    r.check("…", step("60", 202, 4, 0), (303, 5))
    r.check("…", step("60", 303, 5, 0), (454, 6))
    r.check("…", step("60", 454, 6, 0), (681, 7))
    r.check("capped at the ceiling", step("60", 681, 7, 0), (900, 8))
    r.check("and stays there", step("60", 900, 8, 0), (900, 9))
    r.check("one new post snaps it back to base", step("60", 900, 9, 1), (60, 0))
    r.check("a batch snaps it back too", step("60", 305, 5, 7), (60, 0))
    r.check("the base interval has a hard floor",
            step("3", 0, 0, 0), (worker.MIN_SCAN_INTERVAL_SECONDS, 1))
    r.check("0 disables the backoff", step("60", 60, 5, 0, ceiling="0"), (60, 6))
    r.check("a ceiling below the base disables it too",
            step("120", 120, 4, 0, ceiling="30"), (120, 5))
    r.check("a big burst after a long lull is immediate", step("60", 900, 20, 3), (60, 0))

    # The running worker exposes the cadence (dashboard, fbctl and the popup read it).
    b.reset()
    settings = b.api("/api/settings")
    r.check("backoff ceiling is a settable value", settings.get("scan_interval_max_seconds"), "900")
    stats = b.api("/api/stats")
    r.check("stats expose the interval", stats.get("scan_interval_seconds"), 10)
    r.check("stats expose the current cadence", stats.get("scan_interval_current"), 10)
    r.check("stats expose the quiet streak", stats.get("scan_quiet_cycles"), 0)
    b.api("/api/settings", method="POST", payload={"scan_interval_max_seconds": "450"})
    r.check("ceiling is stored", b.api("/api/settings")["scan_interval_max_seconds"], "450")
    # Restore the default so a later scenario (or a rerun) starts from a clean state.
    b.api("/api/settings", method="POST", payload={"scan_interval_max_seconds": "900"})


def scenario_notify(b: Backend, r: Report) -> None:
    """Push events fire for queued/failed/posted, and the mute toggles are honoured."""
    b.reset()
    recv = MockReceiver(b)
    recv.start()
    try:
        b.api("/api/settings", method="POST", payload={
            "notify_provider": "webhook",
            "notify_webhook_url": recv.url,
            "notify_on_posted": "true",
            "notify_on_failed": "true",
            "notify_on_queued": "true",
        })

        # 1) The dashboard/CLI test button sends one synchronous event.
        result = b.api("/api/notify-test", method="POST")
        r.check("test notification sent", result.get("ok"), True)
        events = recv.events()
        r.check("test event delivered", [e["event"] for e in events], ["test"])
        r.check("body has Discord-style content", bool(events[0]["body"].get("content")) if events else None, True)
        r.check("body has Slack-style text", bool(events[0]["body"].get("text")) if events else None, True)
        r.check("posted to the configured path", events[0]["path"] if events else None, "/hook")

        # 2) A real farm run: queued, failed and posted all reach the receiver.
        recv.clear()
        sim = Sim(b, "fail", linger=6.0, max_seconds=60.0, fail_first=1)
        sim.start()
        try:
            b.wait_connected()
            b.api("/api/start", method="POST")
            b.wait_for("notify run", lambda s: s["failed_total"] >= 1 and s["posted_total"] >= 2)
        finally:
            sim.join(timeout=75)
            b.api("/api/stop", method="POST")

        names = [e["event"] for e in recv.events()]
        r.check("queued event sent", names.count("queued") >= 1, True)
        r.check("failed event sent", names.count("failed"), 1)
        r.check("posted events sent", names.count("posted"), 2)
        r.check("queued before failed before posted",
                names.index("queued") < names.index("failed") < names.index("posted"), True)

        # 3) Mute queued events; posted events must keep flowing.
        b.reset()
        b.api("/api/settings", method="POST", payload={
            "notify_provider": "webhook",
            "notify_webhook_url": recv.url,
            "notify_on_queued": "false",
        })
        recv.clear()
        sim = Sim(b, "backlog", linger=6.0, max_seconds=45.0)
        sim.start()
        try:
            b.wait_connected()
            b.api("/api/start", method="POST")
            b.wait_for("muted notify run", lambda s: s["posted_total"] >= 1, timeout=60)
        finally:
            sim.join(timeout=60)
            b.api("/api/stop", method="POST")

        names = [e["event"] for e in recv.events()]
        r.check("muted queued events suppressed", names.count("queued"), 0)
        r.check("posted still delivered while muted", names.count("posted"), 1)
    finally:
        recv.stop()


SCENARIOS = {
    "dedup": scenario_dedup,
    "textdup": scenario_textdup,
    "backlog": scenario_backlog,
    "fail": scenario_fail,
    "busy": scenario_busy,
    "backup": scenario_backup,
    "notify": scenario_notify,
    "adaptive": scenario_adaptive,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("scenarios", nargs="*", choices=sorted(SCENARIOS.keys()),
                        default=[], help="subset to run (default: all)")
    args = parser.parse_args()
    names = args.scenarios or list(SCENARIOS.keys())

    backend = Backend()
    report = Report()
    started = time.time()
    try:
        backend.start()
        for name in names:
            print(f"\n[ci] === scenario: {name} ===")
            try:
                SCENARIOS[name](backend, report)
            except Exception as e:  # noqa: BLE001 — one scenario must not hide the others
                print(f"    FAIL {name} raised: {e!r}")
                report.failures.append(f"{name}: {e!r}")
        print("\n[ci] --- backend log (last 40 lines) ---")
        print(backend.tail(40))
    finally:
        backend.stop()

    elapsed = int(time.time() - started)
    print(f"\n[ci] {report.checks - len(report.failures)}/{report.checks} checks passed in {elapsed}s")
    if report.failures:
        print(f"[ci] FAILED ({len(report.failures)}):")
        for f in report.failures:
            print(f"  - {f}")
        return 1
    print("[ci] ALL SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
