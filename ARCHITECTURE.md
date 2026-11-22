# COMSATS Portal Tracker — Architecture & Fallbacks

A simple, high-level guide to how the tracker works, why things are built the way they are, and what safety fallbacks protect your data and uptime. Intended for both humans and future agent sessions.

---

## 1. File Map

Zero external packages — **Python 3.9+ standard library only** (`urllib`, `html.parser`, `fcntl`, `threading`, `json`).

| File | Role |
| :--- | :--- |
| `config.py` | **Leaf** config module (no project imports): `.env` byte decoding (UTF-8/16/32 BOMs + cp1252 fallback), text parsing (`export`, quotes/escapes, inline `#`), `SIS_SESSION_ID` normalization + placeholder rejection, secret-free missing-session diagnostics, process-env vs. file precedence with per-key provenance, single-read `.env` fingerprint watching, path resolution (`ENV_FILE`/`STATE_FILE`/`HEARTBEAT_FILE`), typed `TrackerConfig`, and the one canonical `NOTIFIER_TIMEOUT` rule. Every entry point takes an optional `environ` mapping. |
| `tracker.py` | Main daemon loop, snapshot validation, diffing, watchdog timeouts, and the CLI (`--once`, `--healthcheck`, ...). Imports env/config/watch behaviour from `config.py`, consumes only *typed* Portal results (never raw HTML), and persists everything through `RuntimeStore`. |
| `runtime_store.py` | **Runtime Store** — single owner of all eight `data/` runtime files: exclusive `fcntl.flock` locking, atomic state saves with rolling backups + corrupt fallback, heartbeat writing/`--healthcheck` evaluation, SHA-256 session-expiry marker, anomaly marker, and scrubbed anomaly diagnostics. One atomic temp-file+`os.replace` writer with guaranteed temp cleanup. |
| `sis_client.py` | **Portal module** — HTTP client (`SisClient`) behind a single injectable HTTP transport adapter (`PortalTransport`, production `UrllibTransport`), with `ASP.NET_SessionId` + authenticated `Set-Cookie` handling, explicit `reconfigure()` for connection settings, login/expiry detection, and two typed operations that own the ASP.NET quirks: `fetch_roster() -> RosterResult` and `fetch_course_snapshot(course, include_announcements) -> CourseSnapshotResult`. Diagnostic HTML is scrubbed *inside* the module before it is returned. |
| `notifiers.py` | Pluggable push backends (`ConsoleNotifier`, `NtfyNotifier`, `WebhookNotifier`, `DiscordNotifier`, `TelegramNotifier`) with threaded, timeout-bounded `dispatch_all`. `build_notifiers()` takes the unified timeout from `config.py`. |
| `probe.py` | Standalone diagnostic CLI (respects `ENV_FILE`) to inspect raw portal HTML endpoints and test keep-alive timing. Imports env loading + session normalization from `config`, never from `tracker`. |
| `Dockerfile` | `python:3.13-slim` container running `python3 -u tracker.py` with `HEALTHCHECK` calling `python3 tracker.py --healthcheck`. Copies every top-level module (`config.py`, `sis_client.py`, `notifiers.py`, `runtime_store.py`, `tracker.py`, `probe.py`). |
| `docker-compose.yml` | Runs `comsats-portal-tracker` with `restart: unless-stopped`, directory-mounted config (`./:/app/config:ro`), and persistent `./data:/app/data`. |
| `tests/test_tracker.py` | Offline unit tests (`python3 -m unittest discover -s tests -v`) covering parsers, diffing, real HTML snapshots, daemon behaviour, and every failure fallback. |
| `tests/test_config.py` | Offline tests for the leaf Config module: encodings, parsing, session normalization, precedence/provenance, diagnostics, unified `NOTIFIER_TIMEOUT`, single-read watching, notifier-change detection, and prober loading — all via injected `environ` mappings and real temp `.env` files. |
| `tests/test_runtime_store.py` | Behavioral tests for the Runtime Store against real temp dirs: real `flock` contention/timeout, atomic writes + temp cleanup on simulated disk failure, backup rotation/recovery, markers, diagnostics, bound heartbeat, healthcheck. |
| `tests/test_portal.py` | Portal transport-seam tests: every `SisClient` protocol behavior (redirect safety, cookie lifecycle, login detection, active-course verification, cancellation, header sanitization, error translation, explicit reconfiguration) driven through one stub transport adapter, plus the typed `fetch_roster()` / `fetch_course_snapshot()` contract and behavioral proof that password inputs, `synopsisForm`, and session-cookie *values* never cross the seam. |
| `tests/support_stub_transport.py` | Reusable in-memory `PortalTransport` stub (`StubTransport` / `StubResponse`) serving canned responses, redirect sequences, multi-value `Set-Cookie`, and transport errors while recording outbound requests. Not shipped in the image. |
| `tests/support_portal_pages.py` | In-memory COMSATS portal *simulator* (`SimulatedPortal`) built on the stub transport: generates dashboard / `SetCourse` / `MarksSummary` / `LearningResources` HTML per request so daemon and cycle tests drive the real `SisClient` instead of hand-rolled fake clients. Not shipped in the image. |

---

## 2. Main Loop & Two-Tier Polling Flow

The COMSATS ASP.NET portal kills idle sessions after ~4–5 minutes. Because scraping every course every 2 minutes is unnecessarily heavy, `run_daemon` uses two cadences:

1. **Keep-alive tick (`KEEPALIVE_INTERVAL`, default `120s`)**: sends a single `GET /Courses` request to refresh the sliding session cookie.
2. **Full scan (`FETCH_INTERVAL`, default `480s`)**: scrapes every enrolled course, validates the snapshot, saves `state.json`, and notifies on changes.

```text
  startup (load_config, EnvWatcher, initial heartbeat)
     │
     ▼
 ┌─▶ 1. Hot-reload check: EnvWatcher.check() — ONE read of .env per tick
 │      ├─ same bytes used for the fingerprint (inode/mtime/size/sha256) AND parsing
 │      ├─ returns (changed, description, freshly parsed TrackerConfig)
 │      ├─ .env non-empty values override process_env
 │      ├─ New SIS_SESSION_ID? -> swap cookie in memory, clear expiry state, force immediate scan
 │      └─ Notifier instances rebuilt ONLY when cfg.notifier_key changes
 │         (NOTIFIER_TIMEOUT / notifier URLs / tokens), never on plain ticks
 │   2. Waiting on expired session? (expired_since set or empty SIS_SESSION_ID)
 │      ├─ Write heartbeat status="waiting_for_session" (healthcheck fails)
 │      ├─ Log rate-limited wait status
 │      ├─ Elapsed >= SESSION_EXPIRED_TIMEOUT (300s)? -> write "exiting_session_expired" & exit(1)
 │      └─ Sleep EXPIRED_POLL_INTERVAL (15s) ───────────────────────────────┐
 │                                                                        │
 │   3. Bounded tick via run_with_timeout(CYCLE_TIMEOUT = 120s):          │
 │      ├─ client.fetch_roster()  (/Courses + marker + semester label)    │
 │      ├─ If due (>= FETCH_INTERVAL or first scan):                      │
 │      │    run_cycle() under RuntimeStore.lock() (state.json.lock):     │
 │      │      store.load_state() -> scan_all_courses()                    │
 │      │        (per course: client.fetch_course_snapshot)                │
 │      │      -> validate_snapshot() -> store.save_state() (.bak)        │
 │      │      -> diff_states()                                           │
 │      │      -> dispatch_all() (bounded by NOTIFIER_TIMEOUT)            │
 │      └─ Else: Keep-alive OK                                            │
 │                                                                        │
 │   4. Update heartbeat.json ("ok" / "anomaly") via store.write_heartbeat │
 │      └─ Sleep KEEPALIVE_INTERVAL (refreshing heartbeat every 15s) ─────┘
```

---

## 3. Portal Scraping & Known COMSATS Quirks

Future sessions editing `sis_client.py` should keep these portal behaviors in mind:

- **Two typed operations own every ASP.NET quirk**:
  Callers never sequence portal requests themselves. The module exposes exactly two
  operations, each returning a dataclass with defaults so callers (and tests) can build
  partial expectations:

  ```python
  fetch_roster() -> RosterResult
      courses: List[Course]          # parsed enrolled courses
      semester: str                  # extracted label, "" when the dashboard omits it
      set_cookie_names: Tuple[str, ...]

  fetch_course_snapshot(course: Course, include_announcements: bool = False) -> CourseSnapshotResult
      marks: List[MarkItem]          # parsed, already attributed to `course`
      resources: List[ResourceItem]
      announcements: Optional[List[AnnouncementItem]]   # None unless requested
      set_cookie_names: Tuple[str, ...]
      diagnostic_html: str           # ALREADY scrubbed; only when marks == []
      diagnostic_preview: str        # plain-text snippet; only when marks == []
  ```

  `fetch_course_snapshot()` internally performs, in this order: `SetCourse` -> active-course
  verification on the switch response -> `/MarksSummary/Index` fetch + marker check +
  verification + parse -> `/LearningResources/Index` fetch + marker check + verification +
  parse -> optional announcement parsing of the `SetCourse` body. `fetch_roster()` owns the
  dashboard marker check, the semester extraction, and the "0 courses is a parse error" rule.

- **Stateful server-side course switching**:
  `/MarksSummary/Index` and `/LearningResources/Index` do **not** take a course ID in the URL or query string. Instead, the portal stores the active course inside the server-side ASP.NET session when you call `/Courses/SetCourse/<course_id>`.
  - **Consequence**: If `SetCourse` fails silently or the session glitches, `/MarksSummary/Index` and `/LearningResources/Index` return data for whichever course was previously active in the session (e.g., attributing everything to one course).
  - **Safeguard**: Course pages do **not** echo the numeric `course_id`, so `parse_active_course()` reads `<div class="page_title_container">` (`<h3>` name and `<h5>Course Code : ...</h5>`) and `fetch_course_snapshot()` calls `verify_active_course()` on the `SetCourse` body **and** on both follow-up pages, raising `CourseMismatchError` before any mark or resource can be mis-attributed.

- **Real HTML traps in login detection**:
  Verified against the saved pages in `snapshots/*.html`, authenticated portal pages **contain** strings that look like login markers if matched naively:
  - Inline `<style>` has `body.sis-portal-body:not(.sis-login-page)`.
  - Inline `<script>` has `var imageUrl = "/Login/GetBase64Image";`.
  - Mobile nav includes `<form id="synopsisForm" action="https://ww5.comsats.edu.pk/efs/StudentLogin.aspx">` with a hidden `<input type="password">`.
  - **Safeguard**: `detect_login_page()` strips `<script>`/`<style>` blocks before inspecting `<form>` tags and shell markers, ignores `id="synopsisForm"`, checks `<body class="...sis-login-page...">` specifically on `<body>`, checks `id="LoginSubmit"` / `id="CaptchaMount"`, checks meta-refresh and JS redirects, checks final redirect URLs (`is_login_url()`), and rejects any response missing authenticated shell markers (`has_authenticated_shell()`: `header.sis-portal-header`, `/Login/Sign_out`, `id="Sign_Out"`, `id="Dash_Board"`, or `.sis-welcome`).

- **Pure parsers stay module-level callables (deliberately)**:
  `parse_courses`, `parse_marks_summary`, `parse_learning_resources`, `parse_announcements`,
  `parse_semester`, `parse_active_course`, `verify_active_course`, `detect_login_page`,
  `has_authenticated_shell`, `is_login_url`, `has_*_page_marker`, `scrub_diagnostic_html`, and
  `extract_diagnostic_text` are pure functions, not client methods. The client operations call
  them internally, and tests/`snapshots/*.html` verification can call them directly on captured
  markup (snapshot tests skip gracefully when `snapshots/` is absent — the directory is
  gitignored).

- **Raw HTML never crosses the seam**:
  There are **no** mutable `last_*` side-channel attributes on `SisClient` (the old
  `last_semester`, `last_set_cookie_names`, `last_marks_html`, plus the write-only
  `last_resources_html` / `last_response_info`, are gone). Everything a caller needs rides on
  the typed result or the exception:
  - Normal path: `RosterResult.set_cookie_names` / `CourseSnapshotResult.set_cookie_names`.
  - Failure path: `SessionExpiredError.set_cookie_names` / `PortalHttpError.set_cookie_names`,
    which `fetch_course_snapshot()` widens with every name observed *earlier* in the same
    snapshot, so a mid-sequence failure still reports the full picture.
  - The only HTML that can leave the module is `diagnostic_html`, already passed through
    `scrub_diagnostic_html()` (drops the `synopsisForm` block, redacts every
    `<input type="password">`, trims header/footer, bounds the size) plus
    `diagnostic_preview`, its credential-free text extraction. `tracker.scan_all_courses()`
    copies those two pre-scrubbed strings into `ScanResult` and never touches a page body.

- **HTTP transport adapter seam**:
  `SisClient` reaches the network only through `client.transport.fetch(url, headers, timeout) -> TransportResponse(status, headers, body, final_url)`. The default is `UrllibTransport` (stdlib `urllib` with `_NoRedirect` installed, so redirects come back as 3xx `TransportResponse`s instead of being followed by urllib). Adapters must return a response for every HTTP status and raise only for genuine transport failures. All protocol logic — header sanitization, cookie-header construction, cookie staging across hops, same-origin/no-downgrade redirect checks, login detection, cancellation, error translation — stays inside `SisClient`, so tests injecting a stub still exercise the real protocol. Tests use `tests/support_stub_transport.py` (`StubTransport`, `StubResponse`); never patch a private opener.

- **Observed `Set-Cookie` names travel on data, not side channels**:
  `fetch_page()` returns `PortalResponse(url, final_url, status, body, set_cookie_names)`; `fetch_roster()` and `fetch_course_snapshot()` surface those names on `RosterResult` / `CourseSnapshotResult`; and `SessionExpiredError` / `PortalHttpError` carry the same `set_cookie_names` tuple (widened across the snapshot sequence) so mid-scan failures still feed anomaly diagnostics. Both exceptions remain constructible with a single message argument.

- **Explicit reconfiguration instead of field poking**:
  `session_id`/`base_url`/`user_agent`/`request_delay` are read-only properties. `SisClient.reconfigure(session_id=..., base_url=..., user_agent=..., request_delay=..., timeout=..., cancel_event=..., reset_cookies=False)` updates settings in one call and resets the cookie jar only when the session identifier actually changes or `reset_cookies=True`; it returns whether the jar was reset. `run_daemon` uses it for hot-reload, and passes `reset_cookies=True` on the expired-session `.env`-change path.

- **ASP.NET cookie reuse & `Set-Cookie` handling**:
  If a session expires and the user logs back into `https://sis.comsats.edu.pk` in the **same browser** without clearing cookies, ASP.NET often re-authenticates the **exact same `ASP.NET_SessionId` value** rather than issuing a new string.
  - When `run_daemon` is in the `waiting_for_session` state and detects any `.env` change (`EnvWatcher`), even if `SIS_SESSION_ID` is unchanged, it calls `client.reconfigure(session_id=..., reset_cookies=True)` to drop the cookie jar and immediately re-tests the session rather than waiting for `SESSION_EXPIRED_TIMEOUT`.
  - On authenticated responses (after `detect_login_page()` passes), `SisClient` adopts any non-login `Set-Cookie` additions/rotations (including auxiliary cookies and `ASP.NET_SessionId` rotations, logging cookie names only) and drops unauthenticated cookies on login redirects.

---

## 4. Fallbacks, Guardrails & Failure Recovery

| Failure Mode | What Happens (Safeguard / Fallback) |
| :--- | :--- |
| **One course fails mid-scan** (`PortalHttpError`, `PortalParseError`, `CourseMismatchError`) | `scan_all_courses()` calls `client.fetch_course_snapshot(course, ...)` per course; a per-course failure is logged `[WARN]` with that course's `set_cookie_names`, the course's entry is carried forward from `previous_state`, the remaining courses keep scanning, and `partial=True` is set. `validate_snapshot()` then refuses to overwrite `state.json` and triggers deduplicated `scan_anomaly` handling. |
| **Session expires mid-scan** (`SessionExpiredError`) | `scan_all_courses()` stops making further course requests immediately, preserves already-fetched + carried-forward courses in `ScanResult`, and `run_cycle()` raises `SessionExpiredError` **without** touching `state.json`. The expiry's `set_cookie_names` (widened with names from earlier successful hops in the same snapshot) still reach `ScanResult.set_cookie_names`. |
| **Degenerate / empty scan, semester rollover, & anomaly alert spam** (0 courses, `>0 -> 0` marks, or `>50%` drop when baseline has `>=3` courses or `>=5` marks/resources) | The semester label comes from `fetch_roster().semester` (or the `SIS_SEMESTER` override, `cfg.semester or roster.semester`) and is recorded in `state.json`. When the semester changes (`detect_semester_rollover()`, including when retaken courses share course IDs across semesters, or for legacy `state.json` files with 0% course ID overlap), `run_cycle()` backs up the old semester, saves the new baseline, and emits one `semester_rollover` notification. Within the same semester, `validate_snapshot()` rejects anomalous drops (unless `--once --reset-state` is passed), persists the anomaly key/count in `data/.anomaly_alerted` (sending **one** `scan_anomaly` alert per unique anomaly reason until recovery), writes diagnostics to `data/last_anomaly.json` and `data/last_anomaly_marks.html` from the snapshot's **already-scrubbed** `diagnostic_html` / `diagnostic_preview` (the Portal module stripped `synopsisForm` and password inputs before this point), and sends a `scan_recovered` alert once normal scans resume. Keep-alives preserve `"status": "anomaly"` in `heartbeat.json` until a full scan recovers. |
| **Silent deletions on the portal** | `diff_states()` compares both directions (`new - old` and `old - new`), emitting `marks_removed`, `resource_removed`, `announcement_removed`, and `course_removed`. Legitimate small removals (e.g. `2 -> 1` marks) pass plausibility checks, update state, and notify. |
| **Corrupt `state.json` or concurrent `--once` + daemon race** | `RuntimeStore.lock()` acquires an exclusive `fcntl.flock` on `data/state.json.lock` **before** `load_state()` so old state is always read prior to scanning. `save_state()` writes a PID/thread-scoped temp file (cleaning up temps on any error), skips rotating `.bak*` if `state.json` on disk is corrupt JSON, and otherwise atomically rotates `state.json.bak`..`.bak.5` before `os.replace`ing `state.json`. If `state.json` is corrupt JSON, `load_state()` falls back across `state.json.bak` through `state.json.bak.N`. |
| **Session expiry & restart alert spam** | `run_daemon()` hashes the expired cookie into `data/.session_alerted` so it sends **one** `session_expired` notification (even across container restarts while still expired). It polls `.env` every `EXPIRED_POLL_INTERVAL` (`15s`) and exits with code `1` once `SESSION_EXPIRED_TIMEOUT` (`300s`) elapses so Docker's `restart: unless-stopped` recycles the container. Any `.env` save while waiting immediately re-tests the session, and any successful `fetch_roster()` clears `.session_alerted` immediately (even if the subsequent full scan hits an anomaly). |
| **`.env` edits with inode replacement, BOM/shell formats, or `env_file:` overrides** | `docker-compose.yml` mounts `./:/app/config:ro` (`ENV_FILE=/app/config/.env`) instead of a single-file `.env` mount, so atomic-save editors (`vim`, VS Code, `sed -i`) are visible immediately. `config.py` supports UTF-8/UTF-16/UTF-32 BOMs (with cp1252 fallback), POSIX `export KEY=VALUE` lines, single/double quotes with escape handling, inline `#` comments (both unquoted and after quoted values), and `Cookie:` / `ASP.NET_SessionId=` normalization (`normalize_session_id()` also rejects the placeholder examples). `load_env_with_sources()` lets non-empty `.env` keys override the environment mapping (falling back to it if `.env` leaves a key blank) and records the winning source per key. `config.EnvWatcher.check()` reads `.env` **at most once per check** and reuses those bytes for both the fingerprint (existence, inode, mtime, size, SHA-256) and the newly parsed `TrackerConfig`, returning change status, description, and config together. Every config entry point takes an optional `environ` mapping (defaulting to the process environment) so callers and tests never mutate global environment state. |
| **Hung HTTP request, unsafe redirect, header error, or shutdown mid-scan** | `SisClient.fetch_page()` sanitizes `User-Agent`/`Referer`, translates transport failures (`TransportError`, `URLError`, `TimeoutError`, `http.client.HTTPException`, `OSError`, `ValueError`/`UnicodeEncodeError`) into `PortalHttpError`, refuses cross-host, cross-port, or HTTPS-to-HTTP downgrade redirects (`_is_safe_redirect_target()` in `sis_client.py` and `probe.py`), bounds redirect chains at 4 hops, and supports cooperative `cancel_event` during both throttling and redirect hops. `dispatch_all()` sends each non-console notifier in a daemon thread bounded by `NOTIFIER_TIMEOUT` (one rule in `config.py`: default `20s`, minimum `1s`, passed into `build_notifiers()`; per-notifier HTTP default is also `20s`). The whole daemon tick runs inside `run_with_timeout(CYCLE_TIMEOUT = 120s)` with `stop_event` support so `SIGTERM`/`SIGINT` cancels in-flight scans cleanly without waiting for Docker `SIGKILL`; if a thread stays wedged past the 2s grace window (or times out 3 times in a row), `run_daemon` exits `1`. |
| **Silent daemon stall / invisible hang** | `run_daemon` writes `data/heartbeat.json` on the main loop thread (during startup, scan progress, after ticks, and every `15s` while sleeping). Docker's `HEALTHCHECK` runs `python3 tracker.py --healthcheck` every `30s`, failing if `heartbeat.json` is missing, stale (`> max(KEEPALIVE_INTERVAL, 60)` or `HEALTHCHECK_MAX_AGE`), or in `waiting_for_session` / `exiting_session_expired` / `wedged_exiting`. |
| **`--once` network/HTTP failure** | `main()` wraps `--once` in `run_with_timeout` and catches `SessionExpiredError`, `PortalHttpError`, `CycleTimeoutError`, `StateLockError`, and `ValueError`, printing a one-line `[ERROR]` message and exiting `1` cleanly. |

---

## 5. Runtime Files (`./data/`)

All runtime files live under `./data/` (mounted to `/app/data` in Docker) and are gitignored. **Every one of them is owned and written exclusively by `runtime_store.RuntimeStore`** — no module performs its own `open(...,'w')` on a runtime file. All writes go through one atomic temp-file+`os.replace` writer that creates parent directories and deletes the temp file on any failure (including `BaseException`), so an interrupted write can neither half-write a file nor leave `*.tmp.*` litter behind.

| File | Purpose | `RuntimeStore` interface |
| :--- | :--- | :--- |
| `data/state.json` | Current known courses, marks, resources, and optional announcements. | `load_state()`, `save_state(state, max_backups=...)` |
| `data/state.json.bak`, `.bak.2`..`.bak.N` | Rolling backups rotated before every state overwrite (bounded by `MAX_STATE_BACKUPS`). | `save_state()` / `backup_paths()` |
| `data/state.json.lock` | `fcntl.flock` lock file serializing `load -> scan -> validate -> save -> diff/dispatch`. | `lock(timeout=...)` |
| `data/heartbeat.json` | Main-loop liveness timestamp, PID, bound intervals, and status read by `--healthcheck`. | `write_heartbeat(status, detail=None, last_ok=None)`, `bind_heartbeat(...)`, `check_healthcheck(max_age_override=None)` |
| `data/.session_alerted` | SHA-256 hash of the cookie that already triggered a `session_expired` notification. | `was_session_alerted(sid)`, `mark_session_alerted(sid)`, `clear_session_alerted()` |
| `data/.anomaly_alerted` | Active anomaly key, first/last seen timestamps, and rejected scan count for deduplication + recovery alerts. | `load_anomaly()`, `save_anomaly(payload)`, `clear_anomaly()` |
| `data/last_anomaly.json` | Latest rejected scan summary (counts, `Set-Cookie` names observed, failed courses, plain-text page preview). | `write_anomaly_diagnostics(...)` |
| `data/last_anomaly_marks.html` | Credential-scrubbed `MarksSummary` HTML captured from the latest rejected 0-mark scan. | `write_anomaly_diagnostics(...)` |

**Heartbeat timing is bound once.** `RuntimeStore` is constructed (and re-bound via `bind_heartbeat()` on every `.env` reload) with `keepalive_interval`, `fetch_interval`, and `healthcheck_max_age`, so daemon call sites are just `store.write_heartbeat(status, detail=..., last_ok=...)`. `write_heartbeat()` still accepts the per-call timing keywords for ad-hoc callers, and the heartbeat JSON fields/status values and `--healthcheck` freshness and unhealthy-status rules are unchanged.

---

## 6. Key Reliability Settings (`.env`)

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `KEEPALIVE_INTERVAL` | `120` (min `15`) | Seconds between lightweight `GET /Courses` keep-alive pings. |
| `FETCH_INTERVAL` | `480` (min `30`) | Seconds between full multi-course scrapes. |
| `CYCLE_TIMEOUT` | `120` (min `5`) | Hard per-tick deadline (seconds) before flagging a cycle as timed out / wedged. |
| `SESSION_EXPIRED_TIMEOUT` | `300` (min `0`) | Seconds to poll `.env` after expiry before exiting `1` for restart (`0` = immediate exit). |
| `EXPIRED_POLL_INTERVAL` | `15` (min `1`) | Seconds between `.env` checks while waiting on an expired session. |
| `NOTIFIER_TIMEOUT` | `20` (min `1`) | Single canonical rule in `config.py` (`NOTIFIER_TIMEOUT_DEFAULT = 20.0`, `NOTIFIER_TIMEOUT_MINIMUM = 1.0`): the join timeout per notification dispatch and the per-notifier HTTP timeout. Passed into `build_notifiers()`; there is no separate notifier-side default or minimum. Non-notifier `.env` edits never trigger a notifier rebuild. |
| `MAX_STATE_BACKUPS` | `5` (min `1`) | Number of rolling `state.json.bak*` files to keep. |
| `HEALTHCHECK_MAX_AGE` | `0` (auto) | Max allowed heartbeat age in seconds (`0` = `max(KEEPALIVE_INTERVAL, 60)`). |

---

## 7. Notes for Future Sessions

- **Run the test suite before and after any change**:
  ```bash
  python3 -m unittest discover -s tests -v
  ```
- **Add configuration by editing `config.py`, not `tracker.py`**: env decoding, parsing, session
  normalization, source provenance, path resolution and typed `TrackerConfig` all live in the leaf
  `config.py`. Keeping it leaf (no imports of `tracker`, `probe`, `sis_client` or `notifiers`) is
  what keeps the module graph acyclic — `probe.py` imports config directly and must never import
  `tracker`. New top-level modules must also be added to the `COPY` line in `Dockerfile`.
- **Keep it zero-dependency**: stick to Python standard library modules only (`Dockerfile` does not run `pip install`).
- **Verify HTML changes against real snapshots**: `snapshots/*.html` (gitignored, present locally) contains real captured portal pages; run `detect_login_page()`, `has_authenticated_shell()`, and `parse_active_course()` against them whenever modifying HTML parsing or login detection.
- **Container updates**: changes to `Dockerfile`, `docker-compose.yml`, or Python source require `docker compose up -d --build` on the host; `.env` updates alone do not.
