# FB Post Copier — Firefox extension + self-contained Python worker

Copies posts (text + images + links) from one Facebook group to another, with
two-level deduplication, cooldowns, a daily cap, optional active hours and a
dashboard/CLI. Runs on Windows, Fedora and anywhere Docker runs.

```
fb-crossposter/
├── extension/                 ← REQUIRED (Firefox, Manifest V2)
│   ├── manifest.json
│   ├── background.js          coordinator: inventory, posting, backend bridge
│   ├── content.js             runs on facebook.com: buttons + scan + composer
│   ├── popup.html / popup.js  settings, inventory, backend URL + live counters
│   └── styles.css
├── python_backend/            ← the automation worker
│   ├── server.py              WebSocket worker + SQLite + dashboard + stats
│   ├── fbctl.py               terminal control (status/start/stop/scan/settings)
│   ├── simulate_extension.py  dry-run tool: fake extension, no Facebook needed
│   ├── Dockerfile             self-contained image (autostart, healthcheck)
│   └── requirements.txt
├── docker-compose.yml         one-command deployment with persistence
└── fb-post-copier.xpi         packaged extension
```

## 1. Install the extension (required)

1. Firefox → `about:debugging` → **This Firefox** → **Load Temporary Add-on…**
   → select `extension/manifest.json` (or `fb-post-copier.xpi`).
2. Temporary add-ons are unloaded when Firefox restarts — load it again
   (3 clicks). Permanent install needs signing via addons.mozilla.org.
3. Recommended `about:config` settings (background tab throttling):

| Setting | Value |
|---|---|
| `dom.min_background_timeout_value` | `0` |
| `dom.min_background_timeout_value_without_budget` | `0` |
| `dom.background_timeout_throttling` | `false` |
| `dom.suspend_inactive.enabled` | `false` |
| `browser.tabs.unloadOnLowMemory` | `false` |

## 2. Run the automation worker

### Option A — Docker (any PC, recommended for “set and forget”)

```bash
docker compose up -d --build     # from the project root
docker compose logs -f           # watch the automation
```

That's it: the worker restarts with the machine (`restart: unless-stopped`),
auto-starts the farm (`FB_COPIER_AUTOSTART=true`), persists everything in the
`fb-copier-data` volume, and exposes:

* `ws://localhost:8765` — the extension connects here
* `http://localhost:5000` — dashboard, and `docker compose exec fb-post-copier python fbctl.py status`

Set `TZ: "Europe/Berlin"` (or yours) in `docker-compose.yml` if you use active
hours. Remove `FB_COPIER_AUTOSTART` if you prefer to start the farm manually.

### Option B — plain Python (Windows / Fedora)

```bash
cd python_backend
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate.bat
pip install -r requirements.txt
python3 server.py                    # dashboard + worker
```

Useful variants:

```bash
python3 server.py --headless --autostart   # no dashboard at all, farm on connect
python3 server.py --ws-port 9000           # custom ports
python3 fbctl.py status                    # control from a terminal
```

Environment variables (used by Docker, handy locally too):
`FB_COPIER_WS_HOST`, `FB_COPIER_WS_PORT`, `FB_COPIER_HTTP_HOST`,
`FB_COPIER_HTTP_PORT`, `FB_COPIER_AUTOSTART`.

## 3. Control it — three ways

| Interface | What it gives you |
|---|---|
| Dashboard `http://localhost:5000` | settings, start/stop/scan, posts table, live counters |
| `python3 fbctl.py status` | connection state, posted today/total, queue, failed, uptime — no browser needed |
| `python3 fbctl.py settings max_posts_per_day=5` | change any setting from the terminal |
| Extension popup | group URLs, inventory, backend URL, live counters (posted today / total / queue / failed) |
| Push updates | the backend pushes settings + counters to the popup after every cycle — no polling needed |

`fbctl` commands: `status`, `stats`, `posts [--limit N]`, `start`, `stop`,
`scan`, `reset`, `settings [KEY=VALUE …]`. Point it elsewhere with
`--url http://host:5000` or `FB_COPIER_HTTP_URL`.

> Headless mode intentionally has no HTTP API — use `start`/`stop`/`settings`
> before going headless, or read the log (it prints a status line every cycle).

## 4. How the automation works

```
every scan_interval seconds (with ±20% jitter)
  1. worker -> extension: {action:"scan"}
  2. extension re-anchors the source feed to the top, expands "See more",
     extracts new posts (cursor-style: known posts are skipped), downloads images
  3. extension -> worker: {action:"posts_found", posts:[...]}
  4. worker dedups:
       a. SHA-256 content hash   (normalized text + first image URL)
       b. text-only check        (same text with a different image URL, 30-day window)
     only genuinely new posts are stored                       ← never posted twice
  5. worker posts up to batch_size new posts, one at a time:
       {action:"do_post", id, post} → extension opens a visible target tab,
       opens the composer, types, uploads images, clicks Post, waits for it to close
     → {action:"post_result", id, success, error}
     cooldown_seconds (with jitter) between posts, rolling-24h daily cap
  6. counters are pushed to the extension popup and printed to the log
```

Guarantees:

* **Backlog drain** — posts found while stopped are queued and posted later.
* **Single-flight** — posting never overlaps itself; manual *Post All* and the
  farm can't double-post each other (the farm gets a `busy` reply and retries
  the post next cycle).
* **Crash recovery** — rows stuck in `posting` are marked `failed` on restart.
* **Active hours** — outside the window posts stay queued (scans continue).
* **Text-only dedup** — catches the same post re-served with a new image URL
  (disable with `dedup_text_only=false` if you must repost identical text).
* **Human-ish timing** — delays are jittered by ±25% (`humanize_timing=false`
  to disable).

### Push notifications (posted / failed / queued)

Set `notify_provider` in the dashboard (or `fbctl settings notify_provider=…`)
and press **🔔 Test notification** / run `python3 fbctl.py notify-test`:

| Provider | Setup |
|---|---|
| `ntfy` | `notify_ntfy_url` = your topic, e.g. `https://ntfy.sh/my-fb-topic` (or a self-hosted ntfy URL). Optional `notify_ntfy_token` for protected topics. |
| `telegram` | Create a bot with @BotFather, put the token in `FB_COPIER_TELEGRAM_TOKEN` (env preferred so it stays out of the DB), set `notify_telegram_chat_id` in the dashboard. |
| `webhook` | `notify_webhook_url` — Discord or Slack incoming webhooks work as-is (the payload carries `content`/`text` plus structured fields). |
| `knock` | Managed option (free tier): create a workflow with triggers `fb-copier.posted` / `.failed` / `.queued`, then set `KNOCK_API_KEY`. |

Only the events you enable are sent (`notify_on_posted`, `notify_on_failed`,
`notify_on_queued`). Notifications are sent **from the worker** (so they work in
Docker and headless mode), never block the farm, and failures are logged as
`notify [event]: error: …` without affecting posting.

## 5. Settings reference

| Setting | Default | Meaning |
|---|---|---|
| source_group_url | (yours) | group to read from |
| target_group_url | (yours) | group to post to |
| scan_interval_seconds | 60 | pause between scan cycles (min 10) |
| batch_size | 3 | max posts per cycle |
| cooldown_seconds | 10 | pause between two posts |
| max_posts_per_day | 20 | rolling 24 h posting cap |
| auto_mode | false | farm running state |
| dedup_text_only | true | secondary image-independent dedup |
| humanize_timing | true | ±25% jitter on delays |
| active_hours_start / end | 0 / 0 | local-time posting window (0/0 = always) |
| notify_provider | none | none / ntfy / telegram / webhook / knock |
| notify_on_posted / failed / queued | true | which events trigger a notification |
| notify_ntfy_url | (empty) | ntfy topic URL (self-hosted or ntfy.sh) |
| notify_webhook_url | (empty) | Discord/Slack/generic webhook |
| notify_telegram_chat_id | (empty) | Telegram chat id (token via `FB_COPIER_TELEGRAM_TOKEN`) |

## 6. Test without Facebook (dry run)

`simulate_extension.py` impersonates the extension:

```bash
cd python_backend
python3 simulate_extension.py --mode dedup      # dedup + auto-post
python3 simulate_extension.py --mode textdup    # same text, new image → skipped
python3 simulate_extension.py --mode backlog    # found while stopped → posted on start
python3 simulate_extension.py --mode fail --fail-first 1
python3 simulate_extension.py --mode dedup --busy-first 1   # manual/auto collision
```

To test notifications without a real provider, point them at the bundled mock:

```bash
python3 mock_notify_receiver.py --port 15300          # logs POSTs to /tmp/mock_notify.log
python3 fbctl.py settings notify_provider=webhook notify_webhook_url=http://127.0.0.1:15300/hook
python3 fbctl.py notify-test
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Popup: backend not connected | Worker not running, wrong WebSocket URL (popup field), or port not mapped (Docker: `-p 8765:8765`). |
| Dashboard unreachable | Plain Python binds `127.0.0.1` by default — use `FB_COPIER_HTTP_HOST=0.0.0.0` (Docker does this automatically). |
| Scan finds nothing | Source tab must be logged in and able to scroll; try Scan Now / `fbctl scan`. |
| Posts queued but not posted | Check `active_hours`, the daily cap (`fbctl status` shows queue + today), and the cooldown. |
| Post All stops after one post | Fixed in v9.1 (30 s-per-tab stall). The popup result line shows the first error. |
| Post All posted something twice | Only possible if you retried an item marked ⚠ (status unknown). Verify on Facebook first; v9.1 blocks overlapping runs and duplicate content keys. |
| Docker: container healthy but nothing posts | The extension must be loaded in Firefox on the machine that reaches port 8765. `docker compose logs -f` shows each cycle. |
| No notifications arriving | Check `notify_provider`, run `fbctl notify-test`, and confirm the container can reach the provider (outbound network). Telegram needs the token env var; the dashboard only stores the chat id. |
| Timezone wrong for active hours | Set `TZ` in `docker-compose.yml` (Docker) or the OS timezone (plain Python). |

## 8. Important notes

* Browser automation of Facebook may violate its Terms of Service and can get
  an account restricted. Keep rates conservative (small batch, long cooldown,
  modest daily cap) and post only where reposting is allowed.
* Never store Facebook credentials in this project — the extension uses the
  browser's logged-in session.
* Desktop notifications use Linux `notify-send`; on Windows they are skipped
  silently (dashboard/CLI/popup still show status).
* `fbctl reset` / dashboard Reset clears the post history and the extension-side
  dedup cursor.
