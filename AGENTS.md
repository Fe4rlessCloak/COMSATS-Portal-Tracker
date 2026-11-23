# AGENTS.md — COMSATS Portal Tracker

## Commands

```bash
python3 scripts/check.py                      # guardrails: 3.9 grammar + stdlib-only
python3 -m unittest discover -s tests -v      # the test suite
```

The suite is **unittest**, not pytest. There is no `pytest`/`conftest.py`/`pyproject.toml`
and no third-party packages — do not install any, and do not add a pytest dependency.

## Module map

Flat modules at repo root, no packages. `config` is the only leaf.

| Module | Role |
| :--- | :--- |
| `config.py` | Leaf. `.env` decode/parse, session-id normalization, single-read watched reload, typed `TrackerConfig`, canonical `NOTIFIER_TIMEOUT`. |
| `runtime_store.py` | Sole owner of all eight `data/` files; one atomic temp+`os.replace` writer. |
| `sis_client.py` | Portal HTTP behind the injectable `PortalTransport` seam; typed roster/snapshot results; in-module HTML scrubbing. |
| `snapshot_transition.py` | Pure policy — validation, rollover, anomaly dedupe, diffing. Zero I/O. |
| `tracker.py` | Daemon loop, `run_cycle`, CLI. Orchestration only. |
| `notifiers.py` | Push backends + `dispatch_all`. |
| `probe.py` | Standalone diagnostic CLI; the one sanctioned raw-HTML consumer. |

`ARCHITECTURE.md` is the reference for behaviour and failure modes. Read it before changing
policy or the duplication rules.

## Invariants that are not negotiable

- **`config` imports nothing local.** It is the leaf; a local import there creates a cycle.
- **Raw portal HTML must not leave `sis_client.py`.** It returns pre-scrubbed
  `diagnostic_html` / `diagnostic_preview`. `probe.py` is the only exception.
- **Every `data/` write goes through `RuntimeStore`'s atomic writer.** No module-level
  `open(..., "w")` on a runtime file.
- **`state.json` is never overwritten on session expiry or on a rejected scan.**
- **Frozen external contracts:** CLI flags, exit codes, `data/` file names and formats, backup
  naming, heartbeat JSON keys/statuses, notification event names/titles/priorities/tags/urls/
  webhook payload fields, `NOTIFIER_TIMEOUT` (20s default, 1s minimum).
- **Python 3.9+ stdlib only.** `from __future__ import annotations` is mandatory — the code
  must run on the 3.9 floor while local dev runs newer.

## Tests

Mock only at genuine edges: portal HTTP (`tests/support_stub_transport.StubTransport` /
`tests/support_portal_pages.SimulatedPortal`) and notifier delivery (`_RecordingNotifier`).
Everything else uses real temp dirs and real `fcntl.flock`. Inject an `environ` mapping —
never patch `os.environ`. Never patch a private opener; never hand-roll a fake client.

## Local environment

- Git runs with `core.fileMode false` here: the checkout is on a setgid shared directory, so
  without it `git status` reports every tracked file as modified with a zero-line diff. Those
  are mode bits only — never stage, stash, or `git checkout --` them.
- `data/` in the canonical checkout holds **live** runtime files. Test against temp dirs and
  never restart `comsats-portal-tracker` from a worktree.
