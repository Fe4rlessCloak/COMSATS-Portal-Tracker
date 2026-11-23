# AGENTS.md — COMSATS Portal Tracker

## Commands

```bash
python3 scripts/check.py                      # static guardrails (see scripts/check.py docstring)
python3 scripts/probe_guardrails.py           # proves check.py fails in every direction it claims
python3 -m unittest discover -s tests -v      # the test suite
```

`scripts/check.py` reads each file once and reports every problem it finds; a file that does
not parse is reported as a `[FAIL]` line, never a traceback. `scripts/probe_guardrails.py`
copies the checkout to a temp dir, breaks one thing at a time there, and asserts the guardrail
catches it — it never writes into this tree, so it is safe to run at any time.

The suite is **unittest**, not pytest. There is no `pytest`/`conftest.py`/`pyproject.toml`
and no third-party packages — do not install any, and do not add a pytest dependency.

## Module map

Flat modules at repo root, no packages. `config.py` is the leaf config module: it imports
nothing local, so a cycle cannot start there. `sis_client.py` also happens to import nothing
local, but that is incidental — do not treat it as a second permitted leaf.

| Module | Role |
| :--- | :--- |
| `config.py` | Leaf. `.env` decode/parse, session-id normalization, single-read watched reload, typed `TrackerConfig`, canonical `NOTIFIER_TIMEOUT`. |
| `runtime_store.py` | Sole owner of all eight `data/` files; one atomic temp+`os.replace` writer. |
| `sis_client.py` | Portal HTTP behind the injectable `PortalTransport` seam; typed roster/snapshot results; in-module HTML scrubbing. |
| `snapshot_transition.py` | Pure policy — validation, rollover, anomaly dedupe, diffing. Zero I/O. |
| `tracker.py` | Daemon loop, `run_cycle`, CLI. Orchestration only. |
| `notifiers.py` | Push backends + `dispatch_all`. |
| `probe.py` | Standalone diagnostic CLI; the one sanctioned raw-HTML consumer. |
| `session_ui.py` | Opt-in renewal page (`http.server`, loopback-only). `GET /` renders `heartbeat.json` read-only; `POST /` normalizes a pasted cookie, verifies it with `SisClient.fetch_roster()`, then rewrites only `SIS_SESSION_ID` via `config.write_session_id()`. One `guard_request` gate runs before routing for both methods: it enforces the optional `SESSION_UI_ALLOWED_USER` exact case-insensitive `Tailscale-User-Login` match, and refuses any `POST` without a same-origin `Origin`/`Sec-Fetch-Site` context plus this process's `csrf_token`. No Docker access. |

`ARCHITECTURE.md` is the reference for behaviour and failure modes. Read it before changing
policy or the duplication rules.

## Invariants that are not negotiable

- **`config` imports nothing local.** It sits at the bottom of the module graph; a local import
  there would create the only possible cycle. `check.py` fails `config.py` on any import of a
  shipped module, and fails any cycle among the root modules.
- **Raw portal HTML must not leave `sis_client.py`.** It returns pre-scrubbed
  `diagnostic_html` / `diagnostic_preview`. `probe.py` is the only exception.
- **Every `data/` write goes through `RuntimeStore`'s atomic writer.** No module-level
  `open(..., "w")` on a runtime file.
- **`state.json` is never overwritten on session expiry or on a rejected scan.**
- **Frozen external contracts:** CLI flags, exit codes, `data/` file names and formats, backup
  naming, heartbeat JSON keys/statuses, notification event names/titles/priorities/tags/urls/
  webhook payload fields, `NOTIFIER_TIMEOUT` (20s default, 1s minimum).
- **Python 3.9+ stdlib only.** `from __future__ import annotations` is mandatory in every
  `.py` file — the code must run on the 3.9 floor while local dev runs newer. The future
  import defers *annotations* only: an annotation `x: int | str` is safe with it, but a
  module-level `Alias = int | str` still evaluates eagerly and raises `TypeError` on 3.9.
  `check.py` enforces both halves, since the grammar floor accepts the syntax silently.
- **The stdlib allowlist is the 3.9 stdlib, not the running interpreter's.** A module added
  after 3.9 (`tomllib` in 3.11, `interpreters` in 3.14) must not be imported even though
  local dev has it — the image is 3.13 and the floor is 3.9.
- **`Dockerfile` `COPY`, `LOCAL_MODULES`, and the two doc tables move together.** Adding a root
  module means editing `Dockerfile`, `LOCAL_MODULES` in `scripts/check.py`, the module map
  here, and the module table in `ARCHITECTURE.md`. `check.py` enforces the first two; the
  tables are on you.

## Tests

Mock only at genuine edges: portal HTTP (`tests/support_stub_transport.StubTransport` /
`tests/support_portal_pages.SimulatedPortal`) and notifier delivery (`_RecordingNotifier`).
Everything else uses real temp dirs and real `fcntl.flock`. Inject an `environ` mapping —
never patch `os.environ`. Never patch a private opener; never hand-roll a fake client.

Test helpers live in `tests/` and are importable only from there — `check.py` fails a shipped
root module that imports `support_*` or `test_*`.
