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

The session-renewal page (below) builds the same way — add its profile to the same command:

```bash
docker compose --profile session-ui -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

### Optional: session-renewal web page

The tracker normally recovers from an expired `ASP.NET_SessionId` by waiting for you to edit `.env`. That is awkward from a phone. The optional **session-renewal page** shows the tracker's current status and gives you one input box for a fresh cookie: it tests the paste against the portal first, and only writes the `SIS_SESSION_ID` line of `.env` if the portal accepts it. The running tracker notices the change and resumes — no restart, no SSH.

It is **off by default**. A plain `docker compose up -d` starts only the `tracker` service and does not even create the page's container; the `session-ui` Compose profile is what turns it on:

```bash
docker compose --profile session-ui up -d
```

That starts a second container, `comsats-session-ui`, from the same `ghcr.io/fe4rlesscloak/comsats-portal-tracker:latest` image — so the auto-updater below keeps it current too — and publishes it on the host's loopback only (`127.0.0.1:8765`). It cannot reach the Docker daemon, cannot restart anything, mounts `./data` **read-only** (it only reads `heartbeat.json`), and is the only container allowed to write `./` (`/app/config`), because rewriting `.env` is its whole job. The tracker keeps its read-only mount.

Set the page's settings in `.env` beside `docker-compose.yml` (all five are listed in [`.env.example`](.env.example)):

```env
# Only this Tailscale account may load the page or submit a cookie.
# Exact match, case-insensitive. Strongly recommended on a shared server.
SESSION_UI_ALLOWED_USER=you@example.com

# Optional: change the port the page is published on.
SESSION_UI_PORT=8765
```

Compose reads that same `.env` to fill in the service's environment and port mapping, so the container's listener, the published host port, and the settings the page itself enforces all come from one place — and a bare `python3 session_ui.py` run reads the same keys from `.env`, with the process environment winning where both are set. Only the page's own settings are passed through — `SIS_SESSION_ID` and your notifier tokens stay in the mounted file and never enter the container's environment.

With `SESSION_UI_ALLOWED_USER` set, every request without a matching `Tailscale-User-Login` header gets `403 Forbidden` — including one that reaches the loopback port by some other route, which is why it must be paired with the loopback-only publish below rather than trusted on its own.

To stop and remove only the page (returning the host to tracker-only updates), run `docker compose rm -sf session-ui`. Avoid `docker compose --profile session-ui down` — that stops and removes `tracker` too — and note that plain `docker compose stop session-ui` leaves the container in place, so the auto-updater will recreate and start it again on the next image update. While the `comsats-session-ui` container exists, the auto-updater includes the profile automatically and keeps both containers on the latest image.

#### Publishing it privately with Tailscale Serve

The page listens on the server's loopback, so nothing off the machine can reach it yet. [`tailscale serve`](https://tailscale.com/kb/1242/tailscale-serve) puts it on your tailnet over HTTPS — no ports opened, no certificates to manage, and no exposure to the LAN or the public internet.

1. **Install and log in to Tailscale on the server** (once):

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

2. **Confirm the page is up locally first.** From the server itself:

   ```bash
   curl -sS http://127.0.0.1:8765/ | head -n 5
   ```

   A `403` here is expected when `SESSION_UI_ALLOWED_USER` is set — you are not arriving through `tailscale serve`, so there is no `Tailscale-User-Login` header. HTML (or that `403`) means the container is serving; connection refused means it is not running.

3. **Serve it on your tailnet.** This proxies HTTPS on port 443 of your tailnet name to the loopback port, and keeps running across reboots once you enable the service:

   ```bash
   sudo tailscale serve --bg 8765
   sudo tailscale serve status          # prints the https://<machine>.<tailnet>.ts.net URL
   ```

   Open that printed `https://<machine>.<tailnet>.ts.net` URL on any device signed in to your tailnet. Requests arriving this way carry `Tailscale-User-Login`, so the allowlist can do its job.

4. **Restrict it to your own devices if the tailnet is shared.** `tailscale serve` exposes the page to every member of the tailnet by default. Either keep `SESSION_UI_ALLOWED_USER` set (the page then refuses everyone else), and/or put an ACL rule on the tailnet so only your devices may reach that port — see [Tailscale's ACL docs](https://tailscale.com/kb/1018/acls).

5. **Turn it off** with `sudo tailscale serve --https=443 off`, or `sudo tailscale serve reset` to clear every serve mapping on the host.

Two caveats worth knowing before you rely on it:

- `tailscale serve` is the *only* supported exposure for this page. Do not forward the port on your router, and do not change the Compose mapping to `8765:8765` — that publishes on every interface and hands the page to the whole network.
- **Header delivery through `tailscale serve` is verified against a live tailnet**, on a server shared with other Tailscale users. Confirmed end to end through the `*.ts.net` URL: `Tailscale-User-Login` reaches the page on both `GET /` and `POST /`, the proxy forwards the browser's `Host` unchanged rather than rewriting it to the loopback backend (so the same-origin `Origin`/`Host` comparison in `guard_request` matches), and a real cookie submitted from a browser is verified against the portal and saved — the page logs `POST / -> 200`. If the allowlist ever rejects you over the `*.ts.net` URL, check `tailscale serve status` and the page's logs (`docker compose logs session-ui`) before assuming the allowlist is wrong.

If `SESSION_UI_ALLOWED_USER` rejects you after an image update, reload the page
rather than resubmitting an already-open tab. The page's `Referrer-Policy` is
fixed when the document is parsed, so a tab loaded before the update keeps
submitting under the old policy until it is reloaded.

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

## Development

Three commands, all stdlib-only. CI runs them on every pull request and on every
push to `main`:

```bash
python3 scripts/check.py                      # static guardrails
python3 scripts/probe_guardrails.py           # proves check.py fails when it should
python3 -m unittest discover -s tests -v      # the test suite
```

### Run the guardrails before every commit

The repo ships an opt-in `pre-commit` hook that runs `scripts/check.py` — the
same check CI runs — so a broken commit fails in about a second instead of after
a push. Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

That setting is local to your clone, which is why the hook is opt-in rather than
automatic: git will not run a hook you have not pointed it at.

Two things worth knowing:

- **It checks the index, not your working tree.** The staged content is what
  gets committed, so the hook exports it to a temporary directory and checks
  that. A partially-staged file is judged as staged, and unrelated edits you have
  not staged are left alone.
- **It takes over the whole hooks directory.** While `core.hooksPath` points at
  `.githooks`, anything in `.git/hooks` — `pre-push`, `commit-msg`, a husky
  install — stops running. Point `core.hooksPath` back at `.git/hooks` if you
  need those.

The hook does not run the test suite (too slow to sit on every commit); CI still
runs the full suite.

To skip the hook for one deliberate work-in-progress commit, use
`git commit --no-verify`. Note this is only caught by CI once the branch is in a
pull request — a push to a branch with no PR runs no CI at all.

---

## Security & Privacy

- `.env`, `state.json`, and `snapshots/` are excluded via `.gitignore`.
- **Never commit `.env` or `state.json`** — they contain your live session cookie and course data.
- For `ntfy.sh`, treat your topic name like a password (or self-host `ntfy` with `NTFY_TOKEN`).
