# COMSATS Portal Tracker

A lightweight, **zero-dependency** Python tracker for the COMSATS CUOnline Student Portal (`https://sis.comsats.edu.pk`).

It keeps your short-lived `ASP.NET_SessionId` session alive via sliding expiration, scans your enrolled courses on a configurable schedule, and sends open-standard push notifications whenever **new/updated marks** or **new learning resources** appear.

---

## How It Works

1. **Standard, non-hardcoded routes** — dynamically discovers your enrolled courses from `/Courses` (`/Courses/SetCourse/<id>`), then inspects `/MarksSummary/Index` and `/LearningResources/Index` (plus optional announcements). Works for any student account.
2. **Two-tier polling cadence**:
   - **Keep-alive (`KEEPALIVE_INTERVAL`, default `120s`)**: sends a single lightweight `GET /Courses` request every 2 minutes to prevent the 4–5 minute idle session timeout.
   - **Full scan (`FETCH_INTERVAL`, default `480s` / 8 min)**: loops through all enrolled courses with polite delays (`REQUEST_DELAY`) and diffs against `state.json`.
3. **Open-standard notifications**:
   - **[ntfy](https://ntfy.sh)** (recommended — works on `ntfy.sh` or any self-hosted server, mobile & desktop apps)
   - **Generic JSON Webhook** (Home Assistant, n8n, Slack, custom HTTP servers)
   - **Discord Webhook**
   - **Telegram Bot**
4. **Hot-reload `.env` & Watchdog**: If the server recycles sessions overnight, the tracker sends **one** `"SIS session expired"` alert, polls `.env` for a new `SIS_SESSION_ID` (detecting both in-place edits and atomic inode-replacing saves), and exits non-zero after `SESSION_EXPIRED_TIMEOUT` (default `300s`) so Docker/systemd can restart cleanly. A per-cycle deadline (`CYCLE_TIMEOUT`) and heartbeat (`--healthcheck`) catch stalled network or notifier calls.
5. **State Protection**: Snapshots are validated before saving (rejecting partial scans or sudden drops such as `>0` marks falling to `0`), protected with `state.json.lock`, and backed up in rolling `state.json.bak*` files.

For a simple breakdown of the polling flow, fallbacks, runtime files, and COMSATS portal quirks, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Requirements

- **Python 3.9+** (standard library only — no `pip install` required)
- An active `ASP.NET_SessionId` cookie from logging into `https://sis.comsats.edu.pk` (login itself uses Google reCAPTCHA, so initial login must be done manually in a browser)

---

## Quick Start

1. **Clone & configure**
   ```bash
   git clone https://github.com/Fe4rlessCloak/COMSATS-Portal-Tracker.git
   cd COMSATS-Portal-Tracker
   cp .env.example .env
   ```

2. **Get your session cookie**
   - Log into `https://sis.comsats.edu.pk/` in your browser.
   - Open **DevTools → Application → Cookies → `https://sis.comsats.edu.pk`**.
   - Copy the `ASP.NET_SessionId` value into `.env` as `SIS_SESSION_ID=...`.
   - *(Optional)* Copy `navigator.userAgent` from DevTools Console into `SIS_USER_AGENT=...`.

3. **Set up `ntfy.sh` (or another notifier)**
   - Install the **ntfy** app (iOS / Android / Desktop) and subscribe to a hard-to-guess topic name (e.g., `cui-sis-9f4k2x8m`).
   - Set in `.env`:
     ```env
     NTFY_URL=https://ntfy.sh/cui-sis-9f4k2x8m
     ```
   - Test your notification channel:
     ```bash
     python3 tracker.py --test-notify
     ```

4. **Run once to save your baseline, then start the tracker**
   ```bash
   python3 tracker.py --once
   python3 tracker.py
   ```
   The first run saves `state.json` without spamming alerts for old marks/files. Subsequent scans notify only on changes.

---

## Running 24/7 with Docker Compose (Recommended for Mini-PC)

The image is published to GitHub Container Registry, so a server **needs no source checkout** — just one directory with four things:

```
comsats/
├── docker-compose.yml            # copy from the repo
├── .env                          # your session cookie + notifier settings
├── data/                         # created on first run; state.json + heartbeat.json
└── deploy/systemd/               # only if you want the auto-updater (below)
    ├── comsats-tracker-update.sh
    └── comsats-tracker-update.service / .timer
```

Copy `docker-compose.yml` from this repository once, then:

```bash
cp .env.example .env
# Edit .env with SIS_SESSION_ID and NTFY_URL (or another notifier)
mkdir -p data
docker compose up -d
```

`docker-compose.yml` pulls the prebuilt `ghcr.io/fe4rlesscloak/comsats-portal-tracker:latest` image. The container keeps the name `comsats-portal-tracker`, `restart: unless-stopped`, the three `*_FILE` environment variables, the read-only `./:/app/config` mount and the read-write `./data:/app/data` mount — an existing setup that ran `docker compose up -d --build` upgrades with a plain `docker compose up -d`.

Useful commands:

```bash
docker compose logs -f                 # watch live keep-alive & scan logs
docker compose run --rm tracker python3 -u tracker.py --test-notify
docker compose ps                      # inspect container healthcheck status
docker compose pull && docker compose up -d   # update by hand (the timer below does this)
```

State is persisted in `./data/state.json` (with rolling `state.json.bak*` backups and `heartbeat.json`, all gitignored). The Compose file mounts the project directory read-only at `/app/config` (`ENV_FILE=/app/config/.env`) so updating `SIS_SESSION_ID` in `.env` — even with editors that replace the file inode — is detected and hot-reloaded automatically by the running container. The `Dockerfile` also defines a `HEALTHCHECK` backed by `python3 tracker.py --healthcheck`.

### First publish: make the GHCR package public

The repository is public, but GitHub initialises a new GHCR package as **private** depending on your account defaults. Do this once after the first `publish-image` run on `main`:

1. Open `https://github.com/Fe4rlessCloak?tab=packages` (or the repository's right-hand **Packages** sidebar).
2. Open `comsats-portal-tracker` → **Package settings** → **Danger Zone**.
3. **Change visibility** → **Public**, and confirm.

Only then can a server pull without a GitHub token — there is no login step in the setup above, by design, because no credential is stored on the host. Confirm from the server:

```bash
docker pull ghcr.io/fe4rlesscloak/comsats-portal-tracker:latest
```

A `denied` here means the package is still private, not that the image is missing.

### Automatic updates: systemd service + 15-minute timer

Ready-to-copy units live in [`deploy/systemd/`](deploy/systemd/): `comsats-tracker-update.service` (a oneshot) and `comsats-tracker-update.timer`. The service runs [`deploy/systemd/comsats-tracker-update.sh`](deploy/systemd/comsats-tracker-update.sh), which:

1. `docker compose pull`s the image;
2. compares the **image ID** the `:latest` tag now resolves to against the ID the running container was created from — the tag always reads `latest`, so only the ID can tell you whether anything changed — and runs `docker compose up -d` **only when they differ**;
3. deletes superseded tracker images, matching **only** images labelled `org.opencontainers.image.source=https://github.com/Fe4rlessCloak/COMSATS-Portal-Tracker`.

Step 3 never touches anything else on the host. `docker image prune` cannot filter by label, and pruning by name or tag prefix would match whatever else happens to share them, so the label is the entire scope — that is what keeps unrelated containers and images on a shared server safe. The currently-running image is never deleted, and nothing is ever removed with `--force`, so an image still in use is skipped with a warning and retried on the next run.

Install (the script lives with your stack so its default `STACK_DIR` resolves; the units go to `/etc/systemd/system`):

```bash
# from a checkout, onto the server's stack directory:
mkdir -p /path/to/your/comsats/deploy/systemd      # replace /path/to/your/comsats
cp deploy/systemd/comsats-tracker-update.sh /path/to/your/comsats/deploy/systemd/
sudo cp deploy/systemd/comsats-tracker-update.service \
        deploy/systemd/comsats-tracker-update.timer /etc/systemd/system/
sudo systemctl edit --full comsats-tracker-update.service   # set WorkingDirectory + ExecStart
sudo systemctl daemon-reload
sudo systemctl enable --now comsats-tracker-update.timer
```

The two lines to change are `WorkingDirectory=` and `ExecStart=`; both must point at the directory holding your `docker-compose.yml`. The units are templates shipping `/path/to/COMSATS-Portal-Tracker`, so no real host path is committed. If you would rather keep the script anywhere else, set `STACK_DIR=` explicitly in the unit and the script will use it instead of `../..` from its own location.

Check on it:

```bash
systemctl list-timers comsats-tracker-update.timer   # when the next check runs
journalctl -u comsats-tracker-update -f              # watch updates live
journalctl -u comsats-tracker-update -n 50           # last 50 lines
sudo systemctl start comsats-tracker-update.service  # run one check right now
```

A no-op run logs `unchanged: comsats-portal-tracker already runs sha256:...`. A real update logs the old and new IDs, then `removed superseded image sha256:...`. Run the script by hand at any time with `--dry-run` to see what it *would* remove without pulling, recreating or deleting anything:

```bash
STACK_DIR=/path/to/your/comsats bash deploy/systemd/comsats-tracker-update.sh --dry-run
```

### Rolling back to an earlier image

Every publish is also tagged `sha-<7>`, the first seven characters of the commit that passed CI. To pin a known-good commit, edit `docker-compose.yml` on the server:

```yaml
    image: ghcr.io/fe4rlesscloak/comsats-portal-tracker:sha-1a2b3c4
```

then `docker compose pull && docker compose up -d`. Disable the timer while you are pinned (`sudo systemctl disable --now comsats-tracker-update.timer`), or the next check will pull `latest` and move you forward again. Revert the line and re-enable the timer to resume automatic updates.

### Local development builds

The default file is pull-only. To build from a checkout, layer the override:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

`docker-compose.local.yml` adds only `build: .`, so the image it produces is tagged with the same `ghcr.io/fe4rlesscloak/comsats-portal-tracker:latest` reference the server pulls — no separate tag to keep in sync. Everything else (container name, restart policy, environment, mounts) still comes from `docker-compose.yml`, so a local container is shaped exactly like a server one. Do not use the override on a server: it has no checkout to build from.

---

## Running 24/7 on a Linux Mini-PC (`systemd`, no Docker)

Create `/etc/systemd/system/comsats-tracker.service`:

```ini
[Unit]
Description=COMSATS Portal Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/COMSATS-Portal-Tracker
ExecStart=/usr/bin/python3 -u /path/to/COMSATS-Portal-Tracker/tracker.py
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now comsats-tracker
journalctl -u comsats-tracker -f
```

---

## Generic Webhook JSON Payload

If `WEBHOOK_URL` is configured, `tracker.py` sends an HTTP `POST` with `Content-Type: application/json`:

```json
{
  "event": "marks_updated",
  "title": "Marks update: CSC101 - Intro to Computing",
  "message": "+ [Assignments] Assign1: 9.0 / 10.0",
  "priority": "high",
  "tags": ["mortar_board", "chart_with_upwards_trend"],
  "url": "https://sis.comsats.edu.pk/Courses/SetCourse/100001",
  "data": {
    "course_id": "100001",
    "course": "CSC101 — Intro to Computing",
    "items": []
  },
  "timestamp": "2026-11-17T18:40:00+00:00"
}
```

Events: `baseline_saved`, `semester_rollover`, `marks_updated`, `marks_removed`, `resource_added`, `resource_removed`, `announcement_added`, `announcement_removed`, `course_removed`, `scan_anomaly`, `scan_recovered`, `session_expired`, `test`.

---

## CLI Options

- `python3 tracker.py` — run the 24/7 keep-alive + fetch loop
- `python3 tracker.py --once` — run a single scan and exit
- `python3 tracker.py --dry-run` — scan without modifying `state.json`
- `python3 tracker.py --once --reset-state` — accept the current portal snapshot as a fresh baseline (for mid-semester drops)
- `python3 tracker.py --test-notify` — verify your configured notifiers
- `python3 tracker.py --healthcheck` — verify heartbeat freshness and status (used by Docker `HEALTHCHECK`)
- `python3 probe.py` — diagnostic tool to inspect portal HTML endpoints

---

## Security & Privacy

- `.env`, `state.json`, and `snapshots/` are excluded via `.gitignore`.
- **Never commit `.env` or `state.json`** — they contain your live session cookie and course data.
- For `ntfy.sh`, treat your topic name like a password (or self-host `ntfy` with `NTFY_TOKEN`).
