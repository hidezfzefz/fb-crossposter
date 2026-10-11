#!/usr/bin/env python3
"""Local receiver for testing push notifications (not part of the worker).

Logs every POST (path, headers, body) as one JSON line to the file given with
``--log`` (default /tmp/mock_notify.log). Start it, point notify_webhook_url /
notify_ntfy_url / notify_telegram_api_base at it, and inspect the requests.
"""

from __future__ import annotations

import argparse
import http.server
import json
import time


class Handler(http.server.BaseHTTPRequestHandler):
    log_path = "/tmp/mock_notify.log"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        record = {
            "time": int(time.time()),
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence per-request logging
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=15300)
    parser.add_argument("--log", default="/tmp/mock_notify.log")
    args = parser.parse_args()
    Handler.log_path = args.log
    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[mock] listening on 127.0.0.1:{args.port}, logging to {args.log}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
