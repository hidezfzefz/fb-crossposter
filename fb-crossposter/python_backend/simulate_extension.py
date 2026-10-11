#!/usr/bin/env python3
"""
simulate_extension.py — fake Firefox extension for testing the Python backend
=============================================================================

Connects to the backend like the real extension does (hello → scan → posts_found,
do_post → post_result) so you can verify the whole auto-farm pipeline without
touching Facebook.

Usage examples::

    # Dedup + auto-post scenario (start the farm first in the dashboard)
    python3 simulate_extension.py --mode dedup

    # A post that already exists in the DB is never stored twice (check /api/posts)

    # Backlog scenario: keep the farm stopped, hit "Scan Now", then Start
    python3 simulate_extension.py --mode backlog

    # Failure handling: the first do_post answers "failed"
    python3 simulate_extension.py --mode fail --fail-first 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import websockets

TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _post(text: str, img: str) -> dict:
    return {
        "text": text,
        "link": "",
        "author": "Simulator",
        "permalink": "https://www.facebook.com/groups/0/posts/" + str(abs(hash(text)) % 10**10),
        "images": [img] if img else [],
        "imagesBase64": (
            [{"data": TINY_PNG, "type": "image/png", "name": "sim.png"}] if img else []
        ),
    }


# Round 2 repeats the "Beta post" from round 1 on purpose: the backend must dedup it.
MODES = {
    "dedup": {
        1: [
            _post("Alpha post - test content one", "https://scontent.fbcdn.net/v/t1/alpha.jpg?a=1"),
            _post("Beta post - test content two", "https://scontent.fbcdn.net/v/t1/beta.jpg?b=2"),
        ],
        2: [
            _post("Beta post - test content two", "https://scontent.fbcdn.net/v/t1/beta.jpg?b=2"),
            _post("Gamma post - test content three", "https://scontent.fbcdn.net/v/t1/gamma.jpg?c=3"),
        ],
        3: [],
    },
    "backlog": {
        1: [
            _post("Delta post - found while stopped", "https://scontent.fbcdn.net/v/t1/delta.jpg?d=4"),
        ],
        2: [],
        3: [],
    },
    "fail": {
        1: [
            _post("Epsilon post - will fail", "https://scontent.fbcdn.net/v/t1/epsilon.jpg?e=5"),
            _post("Zeta post - should succeed", "https://scontent.fbcdn.net/v/t1/zeta.jpg?f=6"),
        ],
        2: [
            _post("Eta post - should succeed", "https://scontent.fbcdn.net/v/t1/eta.jpg?g=7"),
        ],
        3: [],
    },
    # Same text, different image URLs → the secondary text-only dedup must skip it.
    "textdup": {
        1: [
            _post(
                "Same announcement text that is definitely longer than forty characters",
                "https://scontent.fbcdn.net/v/t1/dup_a.jpg?a=1",
            ),
        ],
        2: [
            _post(
                "Same announcement text that is definitely longer than forty characters",
                "https://scontent.fbcdn.net/v/t1/dup_b.jpg?b=2",
            ),
        ],
        3: [],
    },
}


def now() -> str:
    return time.strftime("%H:%M:%S")


async def run(args: argparse.Namespace) -> None:
    rounds = MODES[args.mode]
    max_round = max(rounds.keys())

    print(f"[sim {now()}] connecting to {args.url} (mode={args.mode})")
    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "action": "hello",
                    "settings": {
                        "sourceGroupUrl": args.source,
                        "targetGroupUrl": args.target,
                    },
                }
            )
        )
        if args.update_target:
            # Mimic the popup pushing edited group URLs to the backend.
            await ws.send(
                json.dumps(
                    {
                        "action": "settings_update",
                        "settings": {"target_group_url": args.update_target},
                    }
                )
            )
            print(f"[sim {now()}] -> settings_update target={args.update_target}")

        round_no = 0
        do_posts = 0
        auto_mode = None
        linger_until = None
        hard_stop = time.time() + args.max_seconds if args.max_seconds > 0 else None

        while True:
            if hard_stop is not None and time.time() > hard_stop:
                print(f"[sim {now()}] max-seconds reached — disconnecting")
                break
            if linger_until is not None and time.time() > linger_until:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                print(f"[sim {now()}] backend closed the connection")
                break

            msg = json.loads(raw)
            action = msg.get("action")

            if action == "settings":
                s = msg.get("settings") or {}
                auto_mode = str(s.get("auto_mode", "false")).lower() == "true"
                print(f"[sim {now()}] <- settings (auto_mode={auto_mode})")

            elif action == "scan":
                round_no += 1
                posts = rounds.get(round_no, [])
                print(f"[sim {now()}] <- scan  -> sending {len(posts)} post(s) (round {round_no})")
                await ws.send(json.dumps({"action": "posts_found", "posts": posts}))
                if round_no >= max_round:
                    linger_until = time.time() + args.linger

            elif action == "do_post":
                do_posts += 1
                await asyncio.sleep(args.post_delay)
                if args.busy_first > 0 and do_posts <= args.busy_first:
                    # Mimic the real extension refusing while a manual Post All runs.
                    print(f"[sim {now()}] <- do_post id={msg.get('id')} -> BUSY")
                    await ws.send(
                        json.dumps(
                            {
                                "action": "post_result",
                                "id": msg.get("id"),
                                "success": False,
                                "busy": True,
                                "error": "Manual Post All in progress",
                            }
                        )
                    )
                    continue
                fail = args.fail_first > 0 and do_posts <= args.fail_first
                print(
                    f"[sim {now()}] <- do_post id={msg.get('id')} "
                    f"({(msg.get('post') or {}).get('text', '')[:30]!r}) -> "
                    + ("FAIL" if fail else "ok")
                )
                await ws.send(
                    json.dumps(
                        {
                            "action": "post_result",
                            "id": msg.get("id"),
                            "success": not fail,
                            "error": "simulated failure" if fail else "",
                        }
                    )
                )

            elif action == "reset_dedup":
                print(f"[sim {now()}] <- reset_dedup")

            else:
                print(f"[sim {now()}] <- unknown action: {action!r}")

        print(f"[sim {now()}] done: rounds={round_no}, do_posts={do_posts}")
        # Give the backend a moment to process the last post_result.
        await asyncio.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://localhost:8765", help="backend WebSocket URL")
    parser.add_argument("--mode", choices=sorted(MODES.keys()), default="dedup")
    parser.add_argument("--linger", type=float, default=15.0,
                        help="seconds to stay connected after the last scan round")
    parser.add_argument("--max-seconds", type=float, default=0.0,
                        help="hard exit after N seconds even if no scan arrives (0 = never)")
    parser.add_argument("--post-delay", type=float, default=1.0,
                        help="simulated posting time per do_post")
    parser.add_argument("--fail-first", type=int, default=0,
                        help="fail the first N do_post requests")
    parser.add_argument("--busy-first", type=int, default=0,
                        help="answer the first N do_post requests with busy=true (manual posting)")
    parser.add_argument("--source",
                        default="https://www.facebook.com/groups/1144780033953810")
    parser.add_argument("--target",
                        default="https://www.facebook.com/groups/1770877160904592")
    parser.add_argument("--update-target", default="",
                        help="send a settings_update with this target group URL after hello")
    run_args = parser.parse_args()
    asyncio.run(run(run_args))


if __name__ == "__main__":
    main()
