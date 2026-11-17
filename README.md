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
4. **Hot-reload `.env`**: If the server recycles sessions overnight, the tracker sends **one** `"SIS session expired"` alert and automatically resumes as soon as you save a fresh `SIS_SESSION_ID` in `.env` — no restart needed.

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

Make sure `.env` exists **before** starting Docker Compose so `./.env` mounts as a file rather than a directory:

```bash
cp .env.example .env
# Edit .env with SIS_SESSION_ID and NTFY_URL (or another notifier)
docker compose up -d --build
```

Useful commands:
```bash
docker compose logs -f                 # watch live keep-alive & scan logs
docker compose run --rm tracker python3 -u tracker.py --test-notify
docker compose restart                 # reload after replacing .env
```

State is persisted in `./data/state.json` (gitignored). Updating `SIS_SESSION_ID` in-place in `.env` (e.g., via `nano`) is also hot-reloaded automatically by the running container.

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

Events: `baseline_saved`, `marks_updated`, `resource_added`, `announcement_added`, `session_expired`, `test`.

---

## CLI Options

- `python3 tracker.py` — run the 24/7 keep-alive + fetch loop
- `python3 tracker.py --once` — run a single scan and exit
- `python3 tracker.py --dry-run` — scan without modifying `state.json`
- `python3 tracker.py --test-notify` — verify your configured notifiers
- `python3 probe.py` — diagnostic tool to inspect portal HTML endpoints

---

## Security & Privacy

- `.env`, `state.json`, and `snapshots/` are excluded via `.gitignore`.
- **Never commit `.env` or `state.json`** — they contain your live session cookie and course data.
- For `ntfy.sh`, treat your topic name like a password (or self-host `ntfy` with `NTFY_TOKEN`).
