"""
notifier.py — push notifications for the FB Post Copier worker
==============================================================

Sends posted / failed / queued / test events to the provider configured in the
settings. Standard library only (urllib), so it works from Docker and any OS.

Providers (``notify_provider`` setting, or ``FB_COPIER_NOTIFY_PROVIDER`` env):

* ``ntfy``     — POST to a topic URL. ntfy.sh or a self-hosted server.
                 ``notify_ntfy_url``        e.g. https://ntfy.sh/my-fb-topic
                 ``notify_ntfy_token``      optional bearer token
* ``telegram`` — bot API sendMessage.
                 ``notify_telegram_token``  (env FB_COPIER_TELEGRAM_TOKEN preferred)
                 ``notify_telegram_chat_id``
                 ``notify_telegram_api_base`` default https://api.telegram.org
* ``webhook``  — one JSON POST, compatible with Discord/Slack/generic endpoints.
                 ``notify_webhook_url``
* ``knock``    — Knock workflow trigger (https://knock.app), API key from
                 ``KNOCK_API_KEY`` env or ``notify_knock_api_key``.
                 Create a workflow with the trigger ``fb-copier`` in Knock.

Event toggles: ``notify_on_posted`` / ``notify_on_failed`` / ``notify_on_queued``
(``true``/``false``, default true).

Everything here is blocking by design — the server calls it via
``asyncio.to_thread`` so a slow notification can never stall the farm.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 10

# ntfy-ish presentation per event (kept ASCII — HTTP header values must be)
_EVENT_STYLE = {
    "posted": {"priority": "3", "tags": "white_check_mark"},
    "failed": {"priority": "5", "tags": "x"},
    "queued": {"priority": "2", "tags": "inbox_tray"},
    "test": {"priority": "3", "tags": "test_tube"},
}


def _value(settings: dict, key: str, env: str, default: str = "") -> str:
    """Env var wins over the stored setting (keeps secrets out of the database)."""
    return (os.environ.get(env) or str(settings.get(key) or default)).strip()


def send_push(event: str, title: str, message: str, settings: dict,
              extra: dict | None = None) -> str:
    """Blocking send. Returns a short status string for logging ('sent', 'off', ...)."""
    provider = _value(settings, "notify_provider", "FB_COPIER_NOTIFY_PROVIDER", "none").lower()
    if provider in ("", "none", "off", "false", "disabled"):
        return "off"
    if str(settings.get(f"notify_on_{event}", "true")).lower() == "false":
        return "muted"

    payload = {
        "event": event,
        "title": title,
        "message": message,
        "timestamp": int(time.time()),
    }
    if extra:
        payload.update(extra)

    try:
        if provider == "ntfy":
            return _send_ntfy(event, title, message, settings)
        if provider == "telegram":
            return _send_telegram(event, title, message, settings)
        if provider == "webhook":
            return _send_webhook(title, message, payload, settings)
        if provider == "knock":
            return _send_knock(event, payload, settings)
        return f"unknown provider {provider!r}"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return f"error: {e}"
    except Exception as e:  # noqa: BLE001 — never let a notification break the farm
        return f"error: {e!r}"


def _post(url: str, data: bytes, headers: dict) -> str:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
        resp.read()
    return "sent"


def _send_ntfy(event: str, title: str, message: str, settings: dict) -> str:
    url = _value(settings, "notify_ntfy_url", "FB_COPIER_NTFY_URL")
    if not url:
        return "error: notify_ntfy_url is not set"
    style = _EVENT_STYLE.get(event, _EVENT_STYLE["test"])
    headers = {
        "Title": title.encode("ascii", "ignore").decode() or "FB Post Copier",
        "Priority": style["priority"],
        "Tags": style["tags"],
        "Content-Type": "text/plain; charset=utf-8",
    }
    token = _value(settings, "notify_ntfy_token", "FB_COPIER_NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _post(url, message.encode("utf-8"), headers)


def _send_telegram(event: str, title: str, message: str, settings: dict) -> str:
    token = _value(settings, "notify_telegram_token", "FB_COPIER_TELEGRAM_TOKEN")
    chat_id = _value(settings, "notify_telegram_chat_id", "FB_COPIER_TELEGRAM_CHAT_ID")
    base = _value(settings, "notify_telegram_api_base", "FB_COPIER_TELEGRAM_API_BASE",
                  "https://api.telegram.org").rstrip("/")
    if not token or not chat_id:
        return "error: telegram token/chat_id not set"
    url = f"{base}/bot{token}/sendMessage"
    prefix = {"posted": "✅", "failed": "❌", "queued": "📥", "test": "🔔"}.get(event, "🔔")
    data = json.dumps({
        "chat_id": chat_id,
        "text": f"{prefix} {title}\n{message}",
        "disable_notification": False,
    }).encode("utf-8")
    return _post(url, data, {"Content-Type": "application/json"})


def _send_webhook(title: str, message: str, payload: dict, settings: dict) -> str:
    url = _value(settings, "notify_webhook_url", "FB_COPIER_WEBHOOK_URL")
    if not url:
        return "error: notify_webhook_url is not set"
    # `content` (Discord) + `text` (Slack) + structured fields — everyone ignores
    # the keys they don't know.
    body = dict(payload)
    body["content"] = f"{title} — {message}"
    body["text"] = f"{title} — {message}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return _post(url, data, {"Content-Type": "application/json"})


def _send_knock(event: str, payload: dict, settings: dict) -> str:
    api_key = _value(settings, "notify_knock_api_key", "KNOCK_API_KEY")
    if not api_key:
        return "error: KNOCK_API_KEY not set"
    url = _value(settings, "notify_knock_url", "FB_COPIER_KNOCK_URL",
                 "https://api.knock.app/v1/notify")
    data = json.dumps({
        "trigger": f"fb-copier.{event}",
        "data": payload,
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return _post(url, data, headers)
