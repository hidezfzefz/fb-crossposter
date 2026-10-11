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
│   ├── server.py              WebSocket worker + SQLite + dashboard + backup API
│   ├── fbctl.py               terminal control (status/start/scan/settings/export/import)
│   ├── notifier.py            push notifications (ntfy / Telegram / webhook / Knock)
│   ├── simulate_extension.py  dry-run tool: fake extension, no Facebook needed
│   ├── run_scenarios.py       regression suite: replays every scenario, asserts the result
│   ├── mock_notify_receiver.py local receiver for testing notifications
│   ├── Dockerfile             self-contained image (autostart, healthcheck)
│   └── requirements.txt
├── docker-compose.yml         one-command deployment with persistence
├── .github/workflows/ci.yml   syntax + scenario suite (Linux/Windows) + Docker build
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

Install Docker Desktop (Windows/macOS) or `docker-ce` + the compose plugin
(Fedora), then from the folder that contains `docker-compose.yml`:

```bash
docker compose up -d --build     # first run builds the image, then starts it
docker compose logs -f           # watch the automation
```

On Linux the `docker` CLI needs root — running every command with `sudo` works,
but it's better to join the `docker` group once (log out/in afterwards, or use
`newgrp docker`):

```bash
sudo usermod -aG docker "$USER"
```

Otherwise you'll see `permission denied while trying to connect to the docker API
at unix:///var/run/docker.sock` (the container will still have been created).

The first build downloads `python:3.12-slim` and installs three dependencies
(`websockets`, `flask`, `flask-cors`) — it needs network access and takes a
minute or two. Later builds are cached. After any code change: `docker compose up -d --build`.

No compose? The same thing with plain Docker:

```bash
docker build -t fb-post-copier ./python_backend
docker run -d --name fb-post-copier --restart unless-stopped \
  -p 8766:8765 -p 5000:5000 \
  -v fb-copier-data:/app/data \
  -e FB_COPIER_AUTOSTART=true \
  fb-post-copier
```

Confirm it is actually running (the container reports `healthy` once
`/api/status` answers):

```bash
docker ps                                          # fb-post-copier  Up 2 minutes (healthy)
docker logs fb-post-copier | tail -5               # "[server] WebSocket server listening on ws://0.0.0.0:8765"
curl -s http://localhost:5000/api/status           # {"connected": false, ...} until Firefox connects
docker exec fb-post-copier python fbctl.py status  # same view from inside the container
```

**Check the `PORTS` column.** `docker ps` must show
`0.0.0.0:5000->5000/tcp, 0.0.0.0:8766->8765/tcp`. If `PORTS` is empty the
container is running but unreachable from your browser — that happens when a
chosen host port is already taken: Docker then starts the container *without any*
published ports, while the healthcheck (which runs inside the container) still
reports `healthy`. Pick a free port and recreate:

```bash
FB_COPIER_WS_PUBLISH=9000 docker compose up -d   # extension URL becomes ws://localhost:9000
docker compose down && docker compose up -d      # or just free the old port and re-up
```

That's it: the worker restarts with the machine (`restart: unless-stopped`),
auto-starts the farm (`FB_COPIER_AUTOSTART=true`), persists everything in the
`fb-copier-data` volume, and exposes:

* `ws://localhost:8766` — **the extension connects here** (the popup's default is
  `ws://localhost:8765`, so set it to the value `docker ps` shows on the left of
  `->8765`; override with `FB_COPIER_WS_PUBLISH`)
* `http://localhost:5000` — dashboard, and `docker compose exec fb-post-copier python fbctl.py status`

Set `TZ: "Europe/Berlin"` (or yours) in `docker-compose.yml` if you use active
hours. Remove `FB_COPIER_AUTOSTART` if you prefer to start the farm manually.
The image is checked by CI on every push (build → run → `/api/status` → `fbctl`
→ a scenario replay inside the container), so a broken Dockerfile fails the build
rather than your evening.

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
python3 server.py --ws-port 9000           # custom ports (popup URL must match)
python3 fbctl.py status                    # control from a terminal
```

If port 8765 is already used by something else on your machine, the worker exits
with `[Errno 98] Address already in use` — start it on another port
(`--ws-port 9000`) and put the same URL in the extension popup.

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
`scan`, `reset`, `settings [KEY=VALUE …]`, `export [file]`, `import FILE [--merge]`,
`notify-test`. Point it elsewhere with `--url http://host:5000` or
`FB_COPIER_HTTP_URL`.

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

## 6. Backup — move settings + history between machines

Everything lives in one JSON file, so switching PC (or recovering after a
reinstall) is: export on the old machine, import on the new one. Included:
**every setting** (group URLs, schedule, caps, notification config) and the
**complete post history** — statuses, timestamps, errors, the dedup hashes and
the normalized text, so the new machine never re-posts what the old one already
posted.

**Dashboard:** *Backup & Move* card → **⬇ Export backup** downloads
`fb-copier-backup.json`; **⬆ Import backup** restores it (tick *merge* to keep
whatever is already in the database, otherwise it replaces settings + history).

**Terminal:**

```bash
python3 fbctl.py export backup.json          # from a running dashboard-mode worker
python3 fbctl.py export backup.json --images # also embed pending images (bigger)
python3 fbctl.py import backup.json          # replace settings + history
python3 fbctl.py import backup.json --merge  # keep existing rows, skip duplicates
```

**Docker:**

```bash
docker exec fb-post-copier python fbctl.py export /app/data/backup.json
docker cp fb-post-copier:/app/data/backup.json ./fb-copier-backup.json
docker cp ./fb-copier-backup.json fb-post-copier:/app/data/backup.json
docker exec fb-post-copier python fbctl.py import /app/data/backup.json
```

The file is plain JSON (`{"version": 1, "settings": {…}, "posts": […]}`, with
base64 images only for posts that are still queued), so it is easy to inspect,
diff or keep in a private repo. Imports ignore unknown settings keys, skip
malformed rows instead of aborting, and are **idempotent**: importing the same
file twice adds nothing (verified in CI).

> **Treat the backup like a password file.** It is unencrypted and carries every
> setting stored in the database, which can include an ntfy bearer token, a Knock
> API key or a Telegram bot token if you saved one (env variables like
> `FB_COPIER_TELEGRAM_TOKEN` are *not* in the file — re-set them on the new
> machine). Delete the file from shared storage once the move is done.

## 7. Test without Facebook (dry run)

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

### The whole suite, automatically

`run_scenarios.py` boots a throwaway worker on free ports, drives it through the
same HTTP API the dashboard uses, and asserts the outcome of every scenario
above:

```bash
cd python_backend
python3 run_scenarios.py                  # ~2.5 min, prints ok/FAIL per check
python3 run_scenarios.py dedup textdup    # just some of them
```

Covered: `dedup` (the same post twice → stored once), `textdup` (same text, new
image URL → skipped), `backlog` (found while stopped → posted on start), `fail`
(a failing post is recorded, the rest still post), `busy` (manual *Post All* in
progress requeues instead of failing), `backup` (export → reset → import round
trip, plus a rejected garbage file) and `notify` (queued/failed/posted events
reach a mock receiver in order, and the mute toggles hold). Exit code 0 means
every assertion passed, so it is safe to wire into any pipeline.

## 8. Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and PR:

| Job | What it proves |
|---|---|
| **Syntax checks** | every Python module compiles, every extension script parses, `manifest.json` + `docker-compose.yml` are valid |
| **Scenario suite** (Ubuntu **and** Windows) | the regression suite above passes on both platforms — the “runs anywhere” claim is checked, not assumed |
| **Docker** | the image builds, a real `docker run` serves `/api/status`, `fbctl` works inside the container, and the suite passes in there too |

If you push this folder as its own repository, the workflow is already in place
(the file must sit at the repository root — move the `.github/` folder up one
level if you keep the app inside a parent directory).

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Popup: backend not connected | Worker not running, wrong WebSocket URL (popup field), or port not mapped (Docker: `-p 8766:8765`, so the popup needs `ws://localhost:8766`). |
| Dashboard unreachable | Plain Python binds `127.0.0.1` by default — use `FB_COPIER_HTTP_HOST=0.0.0.0` (Docker does this automatically). |
| Scan finds nothing | Source tab must be logged in and able to scroll; try Scan Now / `fbctl scan`. |
| Posts queued but not posted | Check `active_hours`, the daily cap (`fbctl status` shows queue + today), and the cooldown. |
| Post All stops after one post | Fixed in v9.1 (30 s-per-tab stall). The popup result line shows the first error. |
| Post All posted something twice | Only possible if you retried an item marked ⚠ (status unknown). Verify on Facebook first; v9.1 blocks overlapping runs and duplicate content keys. |
| Docker: container healthy but nothing posts | The extension must be loaded in Firefox on the machine that reaches the published WebSocket port (`docker ps` → `0.0.0.0:8766->8765/tcp`), with that same URL in the popup. `docker compose logs -f` shows each cycle. |
| No notifications arriving | Check `notify_provider`, run `fbctl notify-test`, and confirm the container can reach the provider (outbound network). Telegram needs the token env var; the dashboard only stores the chat id. |
| `docker compose up` fails while building | The first build needs network for `pip install`. Behind a proxy set `HTTP_PROXY`/`HTTPS_PROXY` for the build; offline, build on a machine that has network and move the image with `docker save`/`docker load`. |
| Linux: `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock` | Your user isn't in the `docker` group: `sudo usermod -aG docker "$USER"`, then log out/in (or `newgrp docker`). Until then use `sudo docker …`. |
| `docker ps` shows the container `(healthy)` but nothing answers on `localhost:5000` (and `PORTS` is empty) | The chosen host port was already in use, so Docker published none of them. Free the port or remap it (`FB_COPIER_WS_PUBLISH=9000 docker compose up -d`) and update the popup URL. `sudo ss -ltnp \| grep 8765` shows what is holding it. |
| Import says “not a FB Post Copier backup” | You picked the wrong file — it needs the JSON produced by Export (with `settings` and `posts` keys). The dashboard shows the count of restored posts/settings on success. |
| Timezone wrong for active hours | Set `TZ` in `docker-compose.yml` (Docker) or the OS timezone (plain Python). |

## 10. Important notes

* Browser automation of Facebook may violate its Terms of Service and can get
  an account restricted. Keep rates conservative (small batch, long cooldown,
  modest daily cap) and post only where reposting is allowed.
* Never store Facebook credentials in this project — the extension uses the
  browser's logged-in session.
* Desktop notifications use Linux `notify-send`; on Windows they are skipped
  silently (dashboard/CLI/popup still show status).
* `fbctl reset` / dashboard Reset clears the post history and the extension-side
  dedup cursor.
