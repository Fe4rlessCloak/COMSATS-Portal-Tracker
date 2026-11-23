# COMSATS Portal Tracker — Architecture & Fallbacks

A simple, high-level guide to how the tracker works, why things are built the way they are, and what safety fallbacks protect your data and uptime. Intended for both humans and future agent sessions.

---

## 1. File Map

Zero external packages — **Python 3.9+ standard library only** (`urllib`, `html.parser`, `fcntl`, `threading`, `json`).

| File | Role |
| :--- | :--- |
| `config.py` | **Leaf** config module (no project imports): `.env` byte decoding (UTF-8/16/32 BOMs + cp1252 fallback), text parsing (`export`, quotes/escapes, inline `#`), `SIS_SESSION_ID` normalization + placeholder rejection, the one atomic `SIS_SESSION_ID`-only writer (`write_session_id()`, which rewrites only that assignment — appending it when absent — preserves every comment, blank line, key order and other setting, replaces the file atomically so `EnvWatcher` detects it, and creates a new file `0600`), secret-free missing-session diagnostics, process-env vs. file precedence with per-key provenance, single-read `.env` fingerprint watching, path resolution (`ENV_FILE`/`STATE_FILE`/`HEARTBEAT_FILE`), typed `TrackerConfig`, and the one canonical `NOTIFIER_TIMEOUT` rule. Every entry point takes an optional `environ` mapping. |
| `tracker.py` | **Orchestration / CLI** — daemon loop, warm-up + keep-alive cadence, watchdog timeouts, and the CLI (`--once`, `--dry-run`, `--reset-state`, `--test-notify`, `--healthcheck`). Owns `ScanResult` production (`scan_all_courses()`), `run_cycle()` behind a compact `CycleOptions` object, `run_with_timeout()`, `run_daemon()`, and `main()`; consumes only *typed* Portal results (never raw HTML) and persists everything through `RuntimeStore`. `ScanResult`/`CycleResult` and every policy decision come from `snapshot_transition`. |
| `snapshot_transition.py` | **Snapshot Transition (pure)** — zero network/filesystem/locking/logging/dispatch. Owns `compute_transition(previous_state, scan, prior_anomaly, now, options) -> TransitionOutcome` plus the policy primitives `normalize_semester()`, `detect_semester_rollover()`, `validate_snapshot()`, `diff_states()`, `summarize_totals()` and the carrier dataclasses `ScanResult`/`CycleResult`/`TransitionOptions`/`TransitionOutcome`/`AnomalyAction`. Computes rollover once and totals once per snapshot, and returns the state to commit, the anomaly action, and the ordered notifications. |
| `runtime_store.py` | **Runtime Store** — single owner of all eight `data/` runtime files: exclusive `fcntl.flock` locking, atomic state saves with rolling backups + corrupt fallback, heartbeat writing/`--healthcheck` evaluation, SHA-256 session-expiry marker, anomaly marker, and scrubbed anomaly diagnostics. One atomic temp-file+`os.replace` writer with guaranteed temp cleanup. |
| `sis_client.py` | **Portal module** — HTTP client (`SisClient`) behind a single injectable HTTP transport adapter (`PortalTransport`, production `UrllibTransport`), with `ASP.NET_SessionId` + authenticated `Set-Cookie` handling, explicit `reconfigure()` for connection settings, login/expiry detection, and two typed operations that own the ASP.NET quirks: `fetch_roster() -> RosterResult` and `fetch_course_snapshot(course, include_announcements) -> CourseSnapshotResult`. Diagnostic HTML is scrubbed *inside* the module before it is returned. |
| `notifiers.py` | Pluggable push backends (`ConsoleNotifier`, `NtfyNotifier`, `WebhookNotifier`, `DiscordNotifier`, `TelegramNotifier`) with threaded, timeout-bounded `dispatch_all`. `build_notifiers()` takes the unified timeout from `config.py`. |
| `probe.py` | Standalone diagnostic CLI (respects `ENV_FILE`) to inspect raw portal HTML endpoints and test keep-alive timing. Imports env loading + session normalization from `config`, never from `tracker`. |
| `session_ui.py` | **Session UI** — the opt-in, zero-dependency renewal page (`http.server`/`ThreadingHTTPServer`, loopback-only bind unless `SESSION_UI_ALLOW_NON_LOOPBACK`). `GET /` renders `heartbeat.json` read-only (missing/corrupt/stale handled, never a secret); `POST /` normalizes a pasted cookie with `config.normalize_session_id`, verifies it through the real `SisClient.fetch_roster()` over the injectable `PortalTransport` seam, and only then calls `config.write_session_id()`. All requests pass one `SessionUiApp.guard_request()` gate before routing, so `GET` and `POST` are gated symmetrically: when `SESSION_UI_ALLOWED_USER` is set it requires an exact case-insensitive `Tailscale-User-Login` match, and every `POST` additionally needs a same-origin `Origin`/`Sec-Fetch-Site` context plus the process's hidden `csrf_token` — a `POST` with neither `Origin` nor `Sec-Fetch-Site` is refused. Any denial is `403` with no portal call and no `.env` write. Holds no Docker access and no other `.env` writer. |
| `Dockerfile` | `python:3.13-slim` container running `python3 -u tracker.py` with `HEALTHCHECK` calling `python3 tracker.py --healthcheck`, and `EXPOSE`s `8765` for the optional session UI. The `COPY` line names every module explicitly, so adding a root module means editing **four** places that move together: that `COPY`, `LOCAL_MODULES` in `scripts/check.py`, the module map in `AGENTS.md`, and the module table in this file. `check.py` fails if the first two disagree in either direction; the two tables are not machine-checked. |
| `docker-compose.yml` | Pull-only: `image: ghcr.io/fe4rlesscloak/comsats-portal-tracker:latest` (published by `.github/workflows/publish-image.yml`), `comsats-portal-tracker`, `restart: unless-stopped`, directory-mounted config (`./:/app/config:ro`), persistent `./data:/app/data`. Its only job is to run a host with no source checkout. |
| `docker-compose.local.yml` | Development override layered with `-f`: adds `build: .` for the same `image:` reference. Never used on a server. |
| `deploy/systemd/` | `comsats-tracker-update.service` (oneshot) + `.timer` (15 min) + `comsats-tracker-update.sh`. The script pulls, compares the `:latest` **image ID** against the container's, runs `compose up -d` only on a change, then removes superseded images selected solely by `label=org.opencontainers.image.source=...`. |
| `tests/test_tracker.py` | Offline unit tests (`python3 -m unittest discover -s tests -v`) covering parsers, real HTML snapshots, daemon behaviour, and every failure fallback, plus `CycleIntegrationTests` — full `run_cycle` cycles wired as real `SisClient` -> `SimulatedPortal` -> real `RuntimeStore` (temp dir) -> real `snapshot_transition` -> recording notifier. |
| `scripts/check.py` | Static guardrails: Python 3.9 grammar floor; deferred annotations (mandatory `from __future__ import annotations`, and no eagerly-evaluated `X \| Y` outside one); stdlib-only imports against the *3.9* stdlib, with test helpers scoped to `tests/`; the import graph (`config.py` local-import-free, flat modules acyclic); and `Dockerfile`/`LOCAL_MODULES` parity in both directions; and both module tables (`AGENTS.md`, this file) listing exactly the shipped modules, so a new module without a doc row fails the build. Accepts `--root DIR`. Run it with `python3 scripts/check.py`. |
| `scripts/probe_guardrails.py` | Proves `scripts/check.py` fails in every direction it claims. Copies the checkout into a temp dir, breaks one thing per probe, and asserts a `[FAIL]` line with no traceback — it never writes to the working tree. Run it after editing `check.py`. |
| `AGENTS.md` | The short entry point for an agent working here: commands, module map, non-negotiable invariants, test-seam conventions. |
| `tests/test_snapshot_transition.py` | Zero-seam tests for the pure Snapshot Transition module: every validation/rollover/dedupe/recovery/diff rule with plain Python in and out, plus "rollover once" and "totals once" proofs via counting dict subclasses. No filesystem, no network, no mocks. |
| `tests/test_config.py` | Offline tests for the leaf Config module: encodings, parsing, session normalization, precedence/provenance, diagnostics, unified `NOTIFIER_TIMEOUT`, single-read watching, notifier-change detection, and prober loading — all via injected `environ` mappings and real temp `.env` files. |
| `tests/test_runtime_store.py` | Behavioral tests for the Runtime Store against real temp dirs: real `flock` contention/timeout, atomic writes + temp cleanup on simulated disk failure, backup rotation/recovery, markers, diagnostics, bound heartbeat, healthcheck. |
| `tests/test_portal.py` | Portal transport-seam tests: every `SisClient` protocol behavior (redirect safety, cookie lifecycle, login detection, active-course verification, cancellation, header sanitization, error translation, explicit reconfiguration) driven through one stub transport adapter, plus the typed `fetch_roster()` / `fetch_course_snapshot()` contract and behavioral proof that password inputs, `synopsisForm`, and session-cookie *values* never cross the seam. |
| `tests/support_stub_transport.py` | Reusable in-memory `PortalTransport` stub (`StubTransport` / `StubResponse`) serving canned responses, redirect sequences, multi-value `Set-Cookie`, and transport errors while recording outbound requests. Not shipped in the image. |
| `tests/support_portal_pages.py` | In-memory COMSATS portal *simulator* (`SimulatedPortal`) built on the stub transport: generates dashboard / `SetCourse` / `MarksSummary` / `LearningResources` HTML per request so daemon and cycle tests drive the real `SisClient` instead of hand-rolled fake clients. Not shipped in the image. |
| `tests/test_heartbeat_contract.py` | Pins the one `heartbeat.json` staleness rule (and the cookie-waiting status set) and proves both of its readers — `runtime_store.check_healthcheck` and `session_ui.read_heartbeat` — still agree on the same payload, so the container and the page cannot drift apart. |
| `tests/test_session_ui.py` | End-to-end HTTP tests for the Session UI: the real `ThreadingHTTPServer` on `127.0.0.1:0` against real temporary `.env`/`heartbeat.json` files, wired to `SimulatedPortal`. Covers status rendering (ok/missing/corrupt/stale), the three paste formats, `.env` byte equality on every rejection, `EnvWatcher` detection after a save, loopback bind refusal, the single `guard_request` seam, the `SESSION_UI_ALLOWED_USER` gate (allowed/missing/mismatched/padded, case-insensitive exact match, no substring or prefix escape — and read from `.env` as well as the process environment), cross-site `POST` refusal on each signal (foreign `Origin`, disallowed `Sec-Fetch-Site`, missing/forged token, no browser context at all), a malformed or oversized `POST` answering 403/413 instead of dropping the connection, HEAD served like GET, same-origin success, and that no cookie value, login or token reaches the request log or an error body. |

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
 │      │      -> store.load_anomaly()                                    │
 │      │      -> compute_transition(...)   # pure, no I/O                │
 │      │           totals x1/snapshot, rollover x1                       │
 │      │      -> ONE tail: cancel -> dry-run gate -> store commit        │
 │      │           (save_state / save_anomaly+diag / clear_anomaly)      │
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

## 3a. Snapshot Transition (pure policy)

`snapshot_transition.py` is the single place that decides what a completed scan means. It
performs **no** I/O: no network, no filesystem, no locking, no logging, no notification
dispatch. `tracker.run_cycle()` loads the baseline and the prior anomaly marker, hands
them in, and then executes the returned plan.

```python
compute_transition(previous_state, scan, prior_anomaly, now, options) -> TransitionOutcome

TransitionOptions(base_url, track_announcements=False, reset_state=False, notify_anomaly=True)

TransitionOutcome(
    kind,               # baseline_saved | semester_rollover | diff_applied | anomaly_rejected
    persist_state,      # whether the caller must commit `state`
    state,              # the finalized snapshot dict to write (None when rejected)
    anomaly_action,     # AnomalyAction(kind="none"|"record"|"clear", marker, diagnostics)
    notifications,      # ordered: scan_recovered first, then baseline/rollover/diff
    baseline_totals,    # (courses, marks, resources) of the previous baseline
    scanned_totals,     # (courses, marks, resources) of this scan
    summary,            # TransitionSummary: typed status/reason/occurrence/preview/...
)
```

`TransitionOutcome.log_lines() -> List[str]` owns the operator-facing formatting (the
`[WARN] Rejected snapshot (...)` line, its anomaly-preview line, and the `Scanned … — no
changes.` line); `tracker._log_transition()` only prints what it returns.
`TransitionSummary` is a frozen dataclass — the old `Dict[str, Any]` bag is gone.

`Totals(courses, marks, resources)` is the single carrier for the `(courses, marks,
resources)` triple: `as_dict()` is the `data/last_anomaly.json` payload shape and
`as_tuple()` the shape `summarize_totals()` returns, so the two can never disagree about
a key name.

Decision order (each step can only short-circuit what follows):

1. **Reject** — partial scan, 0-course scan, `>0 -> 0` mark drop,
   or a `<50%` drop when the baseline had `>=3` courses / `>=5` marks / `>=5` resources.
   Rollover beats every drop rule; `reset_state` beats every drop rule.
   **Session expiry is not decided here**: `run_cycle()` raises `SessionExpiredError`
   before `compute_transition()` is called, so expiry has exactly one owner and
   `state.json` is never touched. `compute_transition()` rejects an expired `ScanResult`
   with `ValueError` (caller bug), while the standalone `validate_snapshot()` still
   reports `"Session expired mid-scan: …"` for callers that only hold a scan.
2. **Recover** — if an anomaly marker was present and the scan is accepted, one
   `scan_recovered` notification is placed *first* and the marker is scheduled for clearing
   (`notify_anomaly=False` suppresses the notification but still clears the marker).
3. **Classify** — `baseline_saved` (no previous state, or `reset_state`),
   `semester_rollover` (differing normalized label, or disjoint non-empty course IDs), or
   `diff_applied` (the normal path, which also carries the previous semester label forward
   when the scan omitted one).

Two computations happen **once per transition, never repeated**: `detect_semester_rollover()`
is evaluated a single time and fed into the rejection gate, and `summarize_totals()` runs
once for the baseline and once for the new snapshot. The pre-extraction code re-evaluated
rollover inside `validate_snapshot()` on every call and recomputed totals seven times.
`tests/test_snapshot_transition.py::OnceOnlyComputationTests` locks this in with counting
dict subclasses rather than mocks.

Anomaly deduplication is part of the outcome: an identical `reason` key increments
`occurrence`, keeps the original `first_seen`, refreshes `last_seen`, refreshes the
diagnostics payload, and suppresses the notification; a different reason restarts at 1 and
notifies (unless `notify_anomaly` is off).

The two policy primitives `validate_snapshot()` and `detect_semester_rollover()` remain
directly callable for callers that only hold a snapshot, and `diff_states()` remains the
public diff entry point — all three live here now, not in `tracker.py`.

### Cycle orchestration

`run_cycle(client, courses, store, options, notifiers)` takes a real `RuntimeStore` and one
`CycleOptions`. It is the only cycle surface — the pre-refactor keyword form and the
`CycleOptions.state_path` field are gone:

```python
CycleOptions(base_url, track_announcements=False, reset_state=False, notify_anomaly=True,
             semester=None, dry_run=False, max_backups=None, notifier_timeout=None,
             lock_timeout=30.0, cancel_event=None, on_progress=None)
```

The body is one straight line with a single tail: `store.lock()` -> `store.load_state()` ->
`scan_all_courses()` -> cancel check -> mid-scan `SessionExpiredError` re-raise carrying the
observed `Set-Cookie` **names** (state untouched) -> `store.load_anomaly()` ->
`compute_transition()` -> `outcome.log_lines()` printed -> cancel check -> dry-run gate ->
`store.save_state()` / `save_anomaly()`+`write_anomaly_diagnostics()` / `clear_anomaly()` ->
`dispatch_all()` per notification. Baseline, rollover, and diff no longer have separate
tails, so lifecycle fixes cannot diverge between them.

---

## 4. Fallbacks, Guardrails & Failure Recovery

| Failure Mode | What Happens (Safeguard / Fallback) |
| :--- | :--- |
| **One course fails mid-scan** (`PortalHttpError`, `PortalParseError`, `CourseMismatchError`) | `scan_all_courses()` calls `client.fetch_course_snapshot(course, ...)` per course; a per-course failure is logged `[WARN]` with that course's `set_cookie_names`, the course's entry is carried forward from `previous_state`, the remaining courses keep scanning, and `partial=True` is set. `compute_transition()` then returns `anomaly_rejected`, so `run_cycle()` refuses to overwrite `state.json` and triggers deduplicated `scan_anomaly` handling. |
| **Session expires mid-scan** (`SessionExpiredError`) | `scan_all_courses()` stops making further course requests immediately, preserves already-fetched + carried-forward courses in `ScanResult`, and `run_cycle()` re-raises `SessionExpiredError` **without** touching `state.json`, now carrying every `Set-Cookie` **name** observed (its own plus the ones from earlier successful hops in the same snapshot) on `set_cookie_names`. The daemon appends those names — never values — to the `session_expired` notification, the `waiting_for_session` heartbeat detail, and its log line, so the operator can see which cookie the portal rotated. |
| **Degenerate / empty scan, semester rollover, & anomaly alert spam** (0 courses, `>0 -> 0` marks, or `>50%` drop when baseline has `>=3` courses or `>=5` marks/resources) | The semester label comes from `fetch_roster().semester` (or the `SIS_SEMESTER` override, `cfg.semester or roster.semester`) and is recorded in `state.json`. When the semester changes (`compute_transition()` -> `semester_rollover`, including when retaken courses share course IDs across semesters, or for legacy `state.json` files with 0% course ID overlap), `run_cycle()` backs up the old semester, saves the new baseline, and emits one `semester_rollover` notification. Within the same semester, `compute_transition()` returns `anomaly_rejected` for anomalous drops (unless `--once --reset-state` is passed), persists the anomaly key/count in `data/.anomaly_alerted` (sending **one** `scan_anomaly` alert per unique anomaly reason until recovery), writes diagnostics to `data/last_anomaly.json` and `data/last_anomaly_marks.html` from the snapshot's **already-scrubbed** `diagnostic_html` / `diagnostic_preview` (the Portal module stripped `synopsisForm` and password inputs before this point), and sends a `scan_recovered` alert once normal scans resume — always ordered ahead of that scan's baseline/rollover/diff notifications. Keep-alives preserve `"status": "anomaly"` in `heartbeat.json` until a full scan recovers. |
| **Silent deletions on the portal** | `snapshot_transition.diff_states()` compares both directions (`new - old` and `old - new`), emitting `marks_removed`, `resource_removed`, `announcement_removed`, and `course_removed`. Legitimate small removals (e.g. `2 -> 1` marks) pass plausibility checks, update state, and notify. |
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

**Heartbeat timing is bound once.** `RuntimeStore` is constructed (and re-bound via `bind_heartbeat()` on every `.env` reload) with `keepalive_interval`, `fetch_interval`, and `healthcheck_max_age`, so daemon call sites are just `store.write_heartbeat(status, detail=..., last_ok=...)`. `write_heartbeat()` takes no per-call timing keywords — `bind_heartbeat()` is the only way to change timing — and the heartbeat JSON fields/status values and `--healthcheck` freshness and unhealthy-status rules are unchanged. `RuntimeStore(state_path, heartbeat_path=None, *, environ=None, ...)` resolves `HEARTBEAT_FILE` through `config.resolve_heartbeat_file`, so a store built from a bare state path honours an injected `environ` instead of reading `os.environ`.

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

- **Read `AGENTS.md` first** — it carries the module map, the non-negotiable invariants, and
  the test-seam conventions in the fewest lines.
- **Run the guardrails and the test suite before and after any change**:
  ```bash
  python3 scripts/check.py
  python3 scripts/probe_guardrails.py
  python3 -m unittest discover -s tests -v
  ```
  `scripts/check.py` enforces the Python 3.9 grammar floor, deferred annotations (including
  no eagerly-evaluated `X | Y`), stdlib-only imports against the *3.9* stdlib, the module
  import graph (`config.py` leaf and acyclic root modules), and
  `Dockerfile`/`LOCAL_MODULES` parity. The container image runs a newer interpreter than the
  floor, so those checks are the only thing pinning it. `scripts/probe_guardrails.py` proves
  the guardrail still fails on the cases it claims to catch — run it after editing
  `check.py`. It copies the repo into a temp dir first, so this checkout is never written to.
  Its probes cover the three floor constructs `check.py` names (`match`, `except*`,
  `type X = int`) as well as the import, annotation, union, module-graph and Dockerfile rules.
  CI (`.github/workflows/checks.yml`) runs those two plus the suite on Python 3.9 and 3.13,
  and an `image` job builds the real `linux/amd64` image, imports every module inside it,
  and exercises the `HEALTHCHECK` both ways. That job runs on pull requests too and holds no
  registry credentials — publishing lives only in `publish-image.yml`.
- **Change snapshot policy in `snapshot_transition.py`, not `tracker.py`**: validation,
  rollover, anomaly dedupe/recovery, and diffing all live in that pure module, which must
  never import `tracker`, `runtime_store`, or `os`/`io`/`json`. `run_cycle()` only loads,
  calls `compute_transition()`, commits the returned plan, and dispatches.
- **Add configuration by editing `config.py`, not `tracker.py`**: env decoding, parsing, session
  normalization, source provenance, path resolution and typed `TrackerConfig` all live in the leaf
  `config.py`. Keeping it leaf (no imports of `tracker`, `probe`, `sis_client` or `notifiers`) is
  what keeps the module graph acyclic — `probe.py` imports config directly and must never import
  `tracker`. New top-level modules must also be added to the `COPY` line in `Dockerfile`.
- **Keep it zero-dependency**: stick to Python standard library modules only (`Dockerfile` does not run `pip install`).
- **Verify HTML changes against real snapshots**: `snapshots/*.html` (gitignored, present locally) contains real captured portal pages; run `detect_login_page()`, `has_authenticated_shell()`, and `parse_active_course()` against them whenever modifying HTML parsing or login detection.
- **Container updates**: servers run the published image, so an update is `docker compose pull && docker compose up -d` (what `deploy/systemd/comsats-tracker-update.sh` automates; it recreates only when the image ID changed). Local development builds from a checkout instead: `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build` after a source or `Dockerfile` change. `.env` updates alone never need either.
