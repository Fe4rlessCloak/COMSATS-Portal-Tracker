#!/usr/bin/env python3
"""Runtime Store: the single owner of the tracker's ``data/`` runtime files.

Eight sibling files live in the data directory:

1. ``state.json``                    -- primary baseline
2. ``state.json.bak``, ``.bak.N``     -- rolling backups (newest -> oldest)
3. ``state.json.lock``               -- exclusive POSIX advisory lock file
4. ``heartbeat.json``                -- daemon liveness payload for ``--healthcheck``
5. ``.session_alerted``              -- SHA-256 of the cookie already alerted on
6. ``.anomaly_alerted``              -- active anomaly key/count for dedup + recovery
7. ``last_anomaly.json``             -- latest rejected-scan diagnostic summary
8. ``last_anomaly_marks.html``       -- credential-scrubbed marks HTML of that scan

Every one of them is written by :func:`_atomic_write_bytes` -- a temp file in the
same directory followed by ``os.replace`` -- which creates parent directories and
removes the temp file on any exception or interruption (``BaseException``), so a
crash can never leave a half-written runtime file or an orphan ``*.tmp.*`` behind.

Python 3.9+ standard library only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ContextManager, Dict, Iterator, List, Mapping, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility for non-locking helpers
    fcntl = None  # type: ignore[assignment]

from config import resolve_heartbeat_file
from sis_client import extract_diagnostic_text, scrub_diagnostic_html

DEFAULT_KEEPALIVE_INTERVAL = 120
DEFAULT_FETCH_INTERVAL = 480
DEFAULT_HEALTHCHECK_MAX_AGE = 0
DEFAULT_LOCK_TIMEOUT = 30.0
DEFAULT_MAX_BACKUPS = 5


class StateLockError(RuntimeError):
    """Raised when the state file lock cannot be acquired within timeout."""


def _temp_path_for(path: Path) -> Path:
    """PID/thread-scoped sibling temp path (never shared between writers)."""
    return path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")


def _discard_temps(*paths: Path) -> None:
    """Best-effort removal of staged temp files; never masks the original error."""
    for path in paths:
        with contextlib.suppress(OSError):
            if path.exists():
                path.unlink()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, cleaning up the temp on any failure.

    This is the single writer behind every one of the eight runtime files: the
    parent directory is created, the payload is staged to a sibling temp file, and
    ``os.replace`` commits it. If anything raises -- including ``BaseException``
    such as ``KeyboardInterrupt`` -- the staged temp file is removed before the
    exception propagates, so no half-written file or ``*.tmp*`` litter survives.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _temp_path_for(path)
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except BaseException:
        _discard_temps(tmp_path)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic UTF-8 text write routed through the one byte-level writer."""
    _atomic_write_bytes(path, text.encode("utf-8"))


def _read_json_dict(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _state_backup_paths(state_path: Path) -> List[Path]:
    """Return existing backup paths ordered from newest (.bak) to oldest (.bak.N)."""
    paths: List[Path] = []
    primary_bak = state_path.with_name(f"{state_path.name}.bak")
    if primary_bak.exists():
        paths.append(primary_bak)
    numbered_re = re.compile(rf"^{re.escape(state_path.name)}\.bak\.(\d+)$")
    numbered: List[Tuple[int, Path]] = []
    try:
        for candidate in state_path.parent.iterdir():
            m = numbered_re.match(candidate.name)
            if m:
                numbered.append((int(m.group(1)), candidate))
    except OSError:
        pass
    numbered.sort(key=lambda pair: pair[0])
    paths.extend(path for _, path in numbered)
    return paths


@contextlib.contextmanager
def state_lock(state_path: Path, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Acquire an exclusive advisory lock around state load/scan/save cycles."""
    if fcntl is None:
        raise RuntimeError("state_lock requires POSIX fcntl")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    fh = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + max(0.0, timeout)
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateLockError(
                        f"Timed out waiting for lock {lock_path} after {timeout:.1f}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


class RuntimeStore:
    """Owns every runtime file the tracker persists between ticks.

    Initialize once with the primary state path (and an optional heartbeat path,
    falling back to ``HEARTBEAT_FILE`` / ``<data>/heartbeat.json``), then either
    pass it around or let callers who only know the path build a thin one.
    """

    def __init__(
        self,
        state_path: Path,
        heartbeat_path: Optional[Path] = None,
        *,
        environ: Optional[Mapping[str, str]] = None,
        keepalive_interval: int = DEFAULT_KEEPALIVE_INTERVAL,
        fetch_interval: int = DEFAULT_FETCH_INTERVAL,
        healthcheck_max_age: int = DEFAULT_HEALTHCHECK_MAX_AGE,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        max_backups: int = DEFAULT_MAX_BACKUPS,
    ) -> None:
        self.state_path = Path(state_path)
        self.environ = environ
        self.heartbeat_path = (
            Path(heartbeat_path)
            if heartbeat_path is not None
            else resolve_heartbeat_file(self.state_path, environ)
        )
        self.lock_timeout = float(lock_timeout)
        self.max_backups = int(max_backups)
        self._keepalive_interval = int(keepalive_interval)
        self._fetch_interval = int(fetch_interval)
        self._healthcheck_max_age = int(healthcheck_max_age)

    # ------------------------------------------------------------------ paths

    @property
    def lock_path(self) -> Path:
        return self.state_path.with_name(f"{self.state_path.name}.lock")

    @property
    def backup_path(self) -> Path:
        """Newest rolling backup (``state.json.bak``)."""
        return self.state_path.with_name(f"{self.state_path.name}.bak")

    def backup_paths(self) -> List[Path]:
        """Existing backups, newest (``.bak``) to oldest (``.bak.N``)."""
        return _state_backup_paths(self.state_path)

    @property
    def session_alert_path(self) -> Path:
        return self.state_path.with_name(".session_alerted")

    @property
    def anomaly_alert_path(self) -> Path:
        return self.state_path.with_name(".anomaly_alerted")

    @property
    def anomaly_diagnostics_path(self) -> Path:
        return self.state_path.with_name("last_anomaly.json")

    @property
    def anomaly_marks_path(self) -> Path:
        return self.state_path.with_name("last_anomaly_marks.html")

    # ------------------------------------------------------------------- lock

    def lock(self, timeout: Optional[float] = None) -> ContextManager[None]:
        """Exclusive ``fcntl.flock`` on the lock file; ``StateLockError`` on timeout.

        The lock file is the one runtime file that is not written through the atomic
        writer: an advisory exclusive lock needs an open descriptor, so it is opened
        ``a+`` and left empty. Its *content* is never meaningful.
        """
        effective = self.lock_timeout if timeout is None else float(timeout)
        return state_lock(self.state_path, timeout=effective)

    # ------------------------------------------------------------------ state

    def load_state(self) -> Optional[Dict[str, Any]]:
        """Load the baseline, falling back across the backup chain if corrupt."""
        if not self.state_path.exists():
            return None
        data = _read_json_dict(self.state_path)
        if data is not None:
            return data

        for bak_path in _state_backup_paths(self.state_path):
            bak_data = _read_json_dict(bak_path)
            if bak_data is not None:
                print(
                    f"[WARN] Corrupt state file {self.state_path}; "
                    f"recovered baseline from {bak_path}."
                )
                return bak_data
        return None

    def save_state(
        self, state: Dict[str, Any], max_backups: Optional[int] = None
    ) -> None:
        """Persist ``state`` atomically, rotating bounded rolling backups.

        Ordering (unchanged from the original implementation): older backups are
        shifted ``.bak -> .bak.2 -> ...``, the outgoing primary's bytes are written
        to ``state.json.bak``, and only then is the new primary committed. Every
        one of those three writes goes through the module's single atomic writer.
        Rotation is skipped entirely when the current primary is corrupt JSON, so
        corrupt state can never be rotated over a healthy backup chain.
        """
        keep = self.max_backups if max_backups is None else int(max_backups)
        text = json.dumps(state, indent=2, ensure_ascii=False)

        rotate = False
        if self.state_path.exists() and keep >= 1:
            if _read_json_dict(self.state_path) is not None:
                rotate = True
            else:
                print(
                    f"[WARN] Skipping backup rotation of corrupt {self.state_path}; "
                    "preserving existing backups."
                )

        if not rotate:
            _atomic_write_text(self.state_path, text)
            return

        # The backup receives the outgoing primary's bytes, so read them before the
        # new primary replaces them.
        outgoing = self.state_path.read_bytes()
        self._rotate_backups(keep)
        _atomic_write_bytes(self.backup_path, outgoing)
        _atomic_write_text(self.state_path, text)

    def _rotate_backups(self, max_backups: int) -> None:
        for idx in range(max_backups, 1, -1):
            src = self.state_path.with_name(
                f"{self.state_path.name}.bak"
                if idx == 2
                else f"{self.state_path.name}.bak.{idx - 1}"
            )
            dst = self.state_path.with_name(f"{self.state_path.name}.bak.{idx}")
            if src.exists():
                os.replace(src, dst)

    # -------------------------------------------------------------- heartbeat

    def bind_heartbeat(
        self,
        keepalive_interval: Optional[int] = None,
        fetch_interval: Optional[int] = None,
        healthcheck_max_age: Optional[int] = None,
        lock_timeout: Optional[float] = None,
    ) -> None:
        """(Re)bind heartbeat timing parameters, e.g. after a config reload."""
        if keepalive_interval is not None:
            self._keepalive_interval = int(keepalive_interval)
        if fetch_interval is not None:
            self._fetch_interval = int(fetch_interval)
        if healthcheck_max_age is not None:
            self._healthcheck_max_age = int(healthcheck_max_age)
        if lock_timeout is not None:
            self.lock_timeout = float(lock_timeout)

    def write_heartbeat(
        self,
        status: str,
        detail: Optional[str] = None,
        last_ok: Optional[float] = None,
        **legacy: Any,
    ) -> None:
        """Write the liveness payload using the bound timing parameters.

        Legacy per-call timing keywords (``keepalive_interval``, ``fetch_interval``,
        ``healthcheck_max_age``, ``last_ok_ts``) are still accepted for readability
        at ad-hoc call sites; the daemon uses the short form.
        """
        keepalive = int(legacy.pop("keepalive_interval", self._keepalive_interval))
        fetch = int(legacy.pop("fetch_interval", self._fetch_interval))
        max_age = int(legacy.pop("healthcheck_max_age", self._healthcheck_max_age))
        legacy_last_ok = legacy.pop("last_ok_ts", None)
        if last_ok is None:
            last_ok = legacy_last_ok
        if legacy:
            raise TypeError(
                "write_heartbeat() got unexpected keyword arguments: "
                f"{sorted(legacy)}"
            )

        now_ts = time.time()
        payload = {
            "timestamp": now_ts,
            "iso": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "status": status,
            "keepalive_interval": keepalive,
            "fetch_interval": fetch,
            "healthcheck_max_age": max_age,
            "detail": "" if detail is None else str(detail),
            "last_ok_timestamp": last_ok,
        }
        _atomic_write_text(self.heartbeat_path, json.dumps(payload, indent=2))

    def check_healthcheck(
        self, max_age_override: Optional[int] = None
    ) -> Tuple[bool, str]:
        """Verify heartbeat freshness and daemon status for container HEALTHCHECK."""
        heartbeat_path = self.heartbeat_path
        if not heartbeat_path.exists():
            return False, f"Heartbeat file missing: {heartbeat_path}"
        try:
            hb = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return False, f"Heartbeat unreadable: {exc}"
        if not isinstance(hb, dict) or "timestamp" not in hb:
            return False, "Heartbeat payload invalid."

        try:
            ts = float(hb["timestamp"])
        except (TypeError, ValueError):
            return False, "Heartbeat timestamp invalid."

        if max_age_override is not None and max_age_override > 0:
            max_age = int(max_age_override)
        else:
            hb_max = int(hb.get("healthcheck_max_age") or 0)
            keepalive = int(hb.get("keepalive_interval") or DEFAULT_KEEPALIVE_INTERVAL)
            max_age = hb_max if hb_max > 0 else max(keepalive, 60)

        age = max(0.0, time.time() - ts)
        if age > max_age:
            return False, f"Heartbeat stale ({age:.1f}s > {max_age}s)"

        status = str(hb.get("status") or "unknown")
        detail = str(hb.get("detail") or "")
        if status in ("waiting_for_session", "exiting_session_expired", "wedged_exiting"):
            suffix = f" ({detail})" if detail else ""
            return False, f"Unhealthy status: {status}{suffix}"

        return True, f"OK (status={status}, age={age:.1f}s)"

    # --------------------------------------------------------- session marker

    @staticmethod
    def _session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def was_session_alerted(self, session_id: str) -> bool:
        """True when this exact session id already produced a session alert."""
        if not session_id:
            return False
        try:
            return (
                self.session_alert_path.exists()
                and self.session_alert_path.read_text(encoding="utf-8").strip()
                == self._session_hash(session_id)
            )
        except OSError:
            return False

    def mark_session_alerted(self, session_id: str) -> None:
        """Record the SHA-256 of the alerted session id (never the id itself)."""
        if not session_id:
            return
        try:
            _atomic_write_text(self.session_alert_path, self._session_hash(session_id))
        except OSError:
            pass

    def clear_session_alerted(self) -> None:
        try:
            if self.session_alert_path.exists():
                self.session_alert_path.unlink()
        except OSError:
            pass

    # --------------------------------------------------------- anomaly marker

    def load_anomaly(self) -> Optional[Dict[str, Any]]:
        return _read_json_dict(self.anomaly_alert_path)

    def save_anomaly(self, payload: Dict[str, Any]) -> None:
        try:
            _atomic_write_text(
                self.anomaly_alert_path,
                json.dumps(payload, indent=2, ensure_ascii=False),
            )
        except OSError as exc:
            print(f"[WARN] Could not persist anomaly marker: {exc}")

    def clear_anomaly(self) -> None:
        try:
            if self.anomaly_alert_path.exists():
                self.anomaly_alert_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------ diagnostics

    def write_anomaly_diagnostics(
        self,
        reason: str,
        occurrence: int = 1,
        scanned_totals: Optional[Dict[str, int]] = None,
        baseline_totals: Optional[Dict[str, int]] = None,
        failed_courses: Optional[List[str]] = None,
        set_cookie_names: Optional[List[str]] = None,
        sample_marks_course: str = "",
        marks_html: str = "",
    ) -> None:
        """Persist the rejected-scan summary plus scrubbed marks HTML."""
        scrubbed_html = scrub_diagnostic_html(marks_html or "")
        preview = extract_diagnostic_text(marks_html or "", max_chars=500)
        diag_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "occurrence": occurrence,
            "scanned_totals": dict(
                scanned_totals or {"courses": 0, "marks": 0, "resources": 0}
            ),
            "baseline_totals": dict(
                baseline_totals or {"courses": 0, "marks": 0, "resources": 0}
            ),
            "failed_courses": list(failed_courses or ()),
            "set_cookie_names": list(set_cookie_names or ()),
            "sample_marks_course": sample_marks_course,
            "sample_marks_preview": preview,
        }
        try:
            _atomic_write_text(
                self.anomaly_diagnostics_path,
                json.dumps(diag_payload, indent=2, ensure_ascii=False),
            )
            if scrubbed_html:
                _atomic_write_text(self.anomaly_marks_path, scrubbed_html)
        except OSError as exc:
            print(f"[WARN] Could not write anomaly diagnostics: {exc}")
