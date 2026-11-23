#!/usr/bin/env python3
"""
COMSATS Student Portal Tracker

Two-tier lightweight architecture:
1. Keep-alive ping (GET /Courses every KEEPALIVE_INTERVAL, default 120s) keeps the
   sliding ASP.NET_SessionId alive past the 4-5 minute idle timeout.
2. Full multi-course scrape every FETCH_INTERVAL (default 480s = 8 minutes) checks
   /MarksSummary/Index and /LearningResources/Index across every enrolled course.
3. Diffs against state.json and dispatches open-standard notifications (ntfy.sh,
   generic JSON webhook, Discord, Telegram, console).
4. Hot-reloads .env automatically when SIS_SESSION_ID is updated after expiry.
"""

from __future__ import annotations

import argparse
import copy
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, TypeVar

from config import (
    EnvWatcher,
    load_config,
    load_env_dict,
    parse_int,
    resolve_env_file,
    resolve_heartbeat_file,
    resolve_state_file,
)
from notifiers import BaseNotifier, Notification, build_notifiers, dispatch_all
from runtime_store import RuntimeStore, StateLockError
# DEFAULT_BASE_URL / DEFAULT_USER_AGENT stay re-exported here: probe.py-era callers and
# tests/test_config.py reach for them through this module.
from sis_client import (
    Course,
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    PortalHttpError,
    SessionExpiredError,
    SisClient,
)
from snapshot_transition import (
    ANOMALY_CLEAR,
    ANOMALY_RECORD,
    CycleResult,
    ScanResult,
    TransitionOptions,
    TransitionOutcome,
    compute_transition,
)

T = TypeVar("T")


class CycleTimeoutError(RuntimeError):
    """Raised when a polling/scan cycle exceeds its hard deadline."""

    def __init__(self, message: str, wedged: bool = False) -> None:
        super().__init__(message)
        self.wedged = wedged


class CycleCancelledError(RuntimeError):
    """Raised when a running cycle is interrupted by a shutdown signal."""


def scan_all_courses(
    client: SisClient,
    courses: List[Course],
    track_announcements: bool = False,
    previous_state: Optional[Dict[str, Any]] = None,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[], None]] = None,
    semester: Optional[str] = None,
) -> ScanResult:
    """Iterate through all courses and capture current marks, resources, and optional announcements."""
    snapshot: Dict[str, Any] = {"courses": {}}
    effective_semester = (semester or "").strip()
    if effective_semester:
        snapshot["semester"] = effective_semester
    prev_courses = (previous_state or {}).get("courses", {})
    partial = False
    failed_courses: List[str] = []
    seen_cookie_names: List[str] = []
    sample_marks_html = ""
    sample_marks_course = ""
    sample_marks_preview = ""
    sample_from_prior_marks = False

    def _collect_cookies(exc: Optional[BaseException] = None, names: Iterable[str] = ()) -> None:
        candidates: List[str] = list(names)
        if exc is not None:
            candidates.extend(getattr(exc, "set_cookie_names", ()) or ())
        for name in candidates:
            if name not in seen_cookie_names:
                seen_cookie_names.append(name)

    def _carry_forward(course_obj: Course) -> None:
        if course_obj.course_id in prev_courses:
            snapshot["courses"][course_obj.course_id] = copy.deepcopy(
                prev_courses[course_obj.course_id]
            )

    for idx, course in enumerate(courses):
        if cancel_event and cancel_event.is_set():
            partial = True
            failed_courses.append(f"{course.label} (cancelled)")
            _carry_forward(course)
            for rem in courses[idx + 1 :]:
                _carry_forward(rem)
            break

        try:
            course_snapshot = client.fetch_course_snapshot(
                course, include_announcements=track_announcements
            )
            _collect_cookies(names=course_snapshot.set_cookie_names)
            if not course_snapshot.marks and course_snapshot.diagnostic_html:
                prev_had_marks = bool(prev_courses.get(course.course_id, {}).get("marks"))
                if not sample_marks_html or (prev_had_marks and not sample_from_prior_marks):
                    sample_marks_html = course_snapshot.diagnostic_html
                    sample_marks_preview = course_snapshot.diagnostic_preview
                    sample_marks_course = course.label
                    sample_from_prior_marks = prev_had_marks

            course_entry: Dict[str, Any] = {
                "code": course.code,
                "name": course.name,
                "label": course.label,
                "marks": {m.key: m.to_dict() for m in course_snapshot.marks},
                "resources": {r.key: r.to_dict() for r in course_snapshot.resources},
            }
            if track_announcements:
                course_entry["announcements"] = {
                    a.key: a.to_dict() for a in (course_snapshot.announcements or [])
                }

            snapshot["courses"][course.course_id] = course_entry
            if on_progress is not None:
                on_progress()
        except SessionExpiredError as exc:
            _collect_cookies(exc)
            _carry_forward(course)
            for rem in courses[idx + 1 :]:
                _carry_forward(rem)
            return ScanResult(
                state=snapshot,
                partial=True,
                failed_courses=failed_courses + [course.label],
                session_expired=True,
                session_expired_error=str(exc),
                sample_marks_html=sample_marks_html,
                sample_marks_preview=sample_marks_preview,
                sample_marks_course=sample_marks_course,
                set_cookie_names=seen_cookie_names,
            )
        except (PortalHttpError, ValueError) as exc:
            _collect_cookies(exc)
            print(f"[WARN] Failed scanning {course.label} ({course.course_id}): {exc}")
            partial = True
            failed_courses.append(course.label)
            _carry_forward(course)

    return ScanResult(
        state=snapshot,
        partial=partial,
        failed_courses=failed_courses,
        sample_marks_html=sample_marks_html,
        sample_marks_preview=sample_marks_preview,
        sample_marks_course=sample_marks_course,
        set_cookie_names=seen_cookie_names,
    )


def describe_observed_cookie_names(exc: BaseException) -> str:
    """Render the ``Set-Cookie`` *names* a portal failure observed, for diagnostics.

    Names only, never values: this string lands in logs, heartbeat details and
    notifications. Returns ``""`` when the exception carries no names.
    """
    names: List[str] = []
    for name in getattr(exc, "set_cookie_names", ()) or ():
        if name and name not in names:
            names.append(name)
    if not names:
        return ""
    return f" (Set-Cookie names observed: {', '.join(names)})"


@dataclass(frozen=True)
class CycleOptions:
    """Every knob one cycle needs, in one object."""

    base_url: str
    track_announcements: bool = False
    reset_state: bool = False
    notify_anomaly: bool = True
    semester: Optional[str] = None
    dry_run: bool = False
    max_backups: Optional[int] = None
    notifier_timeout: Optional[float] = None
    lock_timeout: float = 30.0
    cancel_event: Optional[threading.Event] = None
    on_progress: Optional[Callable[[], None]] = None

    def transition_options(self) -> TransitionOptions:
        return TransitionOptions(
            base_url=self.base_url,
            track_announcements=self.track_announcements,
            reset_state=self.reset_state,
            notify_anomaly=self.notify_anomaly,
        )


def _log_transition(outcome: TransitionOutcome) -> None:
    """Print the operator-facing lines the transition itself formats."""
    for line in outcome.log_lines():
        print(line)


def run_cycle(
    client: Optional[SisClient],
    courses: List[Course],
    store: RuntimeStore,
    options: CycleOptions,
    notifiers: List[BaseNotifier],
) -> CycleResult:
    """Run one locked load -> multi-course fetch -> transition -> commit + notify cycle.

    This is the only cycle surface: ``(client, courses, store, options, notifiers)``.
    The pre-refactor keyword form is gone — there is no compatibility shim.
    """
    if isinstance(notifiers, (str, bytes)):
        # A bare string iterates character-by-character and dies with an AttributeError
        # deep inside dispatch_all; fail here instead, where the mistake is visible.
        raise TypeError(
            "run_cycle() expects a sequence of notifiers, not a bare string "
            f"({notifiers!r}); pass a list, e.g. [client_notifier]."
        )
    active_notifiers: List[BaseNotifier] = list(notifiers)
    runtime_store = store

    # Load + scan happen under the exclusive lock, baseline first (unchanged ordering).
    with runtime_store.lock(timeout=options.lock_timeout):
        previous_state = runtime_store.load_state()
        scan_result = scan_all_courses(
            client,
            courses,
            track_announcements=options.track_announcements,
            previous_state=previous_state,
            cancel_event=options.cancel_event,
            on_progress=options.on_progress,
            semester=options.semester,
        )

        if options.cancel_event is not None and options.cancel_event.is_set():
            return CycleResult(saved=False, anomaly="Cancelled")

        if scan_result.session_expired:
            print("[WARN] Session expired mid-scan — preserving existing baseline without saving.")
            # ``scan_all_courses`` collected every Set-Cookie name it saw, including the
            # ones on the response that proved the session dead. Carry them on the
            # exception so the daemon can put cookie *names* in its diagnostic line.
            raise SessionExpiredError(
                scan_result.session_expired_error or "Session expired mid-scan.",
                set_cookie_names=tuple(scan_result.set_cookie_names),
            )

        outcome = compute_transition(
            previous_state,
            scan_result,
            runtime_store.load_anomaly(),
            datetime.now(timezone.utc),
            options.transition_options(),
        )
        _log_transition(outcome)

        # ONE unified post-transition tail: cancel check -> dry-run gate -> commit ->
        # dispatch. Baseline, rollover, and diff all travel this path.
        if options.cancel_event is not None and options.cancel_event.is_set():
            return CycleResult(saved=False, anomaly="Cancelled")

        if not options.dry_run:
            if outcome.persist_state and outcome.state is not None:
                runtime_store.save_state(outcome.state, max_backups=options.max_backups)
            action = outcome.anomaly_action
            if action.kind == ANOMALY_RECORD and action.marker is not None:
                runtime_store.save_anomaly(action.marker)
                diagnostics = action.diagnostics
                if diagnostics is not None:
                    runtime_store.write_anomaly_diagnostics(
                        reason=diagnostics.reason,
                        occurrence=diagnostics.occurrence,
                        scanned_totals=diagnostics.scanned_totals,
                        baseline_totals=diagnostics.baseline_totals,
                        failed_courses=list(diagnostics.failed_courses),
                        set_cookie_names=list(diagnostics.set_cookie_names),
                        sample_marks_course=diagnostics.sample_marks_course,
                        marks_html=diagnostics.marks_html,
                        marks_preview=diagnostics.marks_preview,
                    )
            elif action.kind == ANOMALY_CLEAR:
                runtime_store.clear_anomaly()

        for note in outcome.notifications:
            dispatch_all(active_notifiers, note, timeout=options.notifier_timeout)

        return CycleResult(
            saved=outcome.persist_state and not options.dry_run,
            anomaly=outcome.summary.anomaly,
            notifications=list(outcome.notifications),
        )


def run_with_timeout(
    func: Callable[[], T],
    timeout: float,
    cancel_event: Optional[threading.Event] = None,
    grace: float = 2.0,
    stop_event: Optional[threading.Event] = None,
) -> T:
    """Execute func in a daemon thread with a hard join deadline and optional stop signal."""
    result_box: List[T] = []
    error_box: List[BaseException] = []

    def _runner() -> None:
        try:
            result_box.append(func())
        except BaseException as exc:  # noqa: BLE001
            error_box.append(exc)

    worker = threading.Thread(target=_runner, name="cycle-runner", daemon=True)
    worker.start()
    timeout_sec = max(0.1, float(timeout))
    if stop_event is None:
        worker.join(timeout=timeout_sec)
    else:
        deadline = time.monotonic() + timeout_sec
        while worker.is_alive():
            if stop_event.is_set():
                if cancel_event is not None:
                    cancel_event.set()
                worker.join(timeout=max(0.0, float(grace)))
                raise CycleCancelledError("Cycle cancelled by shutdown signal.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=min(0.2, remaining))
        if stop_event.is_set():
            raise CycleCancelledError("Cycle cancelled by shutdown signal.")

    if worker.is_alive():
        if cancel_event is not None:
            cancel_event.set()
        worker.join(timeout=max(0.0, float(grace)))
        raise CycleTimeoutError(
            f"Cycle exceeded hard deadline of {float(timeout):.1f}s",
            wedged=worker.is_alive(),
        )

    if error_box:
        raise error_box[0]
    return result_box[0]


def run_daemon(
    env_path: Path,
    state_path: Path,
    dry_run: bool = False,
    heartbeat_path: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    stop = False
    stop_event = threading.Event()
    current_cancel_event: Optional[threading.Event] = None

    def _handle_sig(signum, frame):
        nonlocal stop
        stop = True
        stop_event.set()
        if current_cancel_event is not None:
            current_cancel_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    cfg = load_config(env_path, require_session=True, environ=environ)
    notifiers = build_notifiers(cfg.raw, timeout=cfg.notifier_timeout)
    client = SisClient(
        session_id=cfg.session_id,
        base_url=cfg.base_url,
        user_agent=cfg.user_agent,
        request_delay=cfg.request_delay,
    )

    hb_path = heartbeat_path or resolve_heartbeat_file(state_path, environ)
    store = RuntimeStore(
        state_path,
        hb_path,
        environ=environ,
        keepalive_interval=cfg.keepalive_interval,
        fetch_interval=cfg.fetch_interval,
        healthcheck_max_age=cfg.healthcheck_max_age,
        max_backups=cfg.max_state_backups,
    )
    env_watcher = EnvWatcher(env_path, environ)

    last_fetch = 0.0
    expired_since: Optional[float] = None
    last_expired_log = 0.0
    expired_cookie_note = ""
    consecutive_timeouts = 0
    last_ok_ts: Optional[float] = None
    last_status = "starting"
    last_detail = ""
    active_session_id = cfg.session_id
    active_anomaly: Optional[str] = None
    active_notifier_key = cfg.notifier_key

    print(
        f"Starting tracker (keep-alive={cfg.keepalive_interval}s, "
        f"fetch={cfg.fetch_interval}s, cycle_timeout={cfg.cycle_timeout}s, "
        f"notifiers={[n.name for n in notifiers]})"
    )
    print(f"[INFO] SIS_SESSION_ID loaded from {cfg.sources.get('SIS_SESSION_ID', 'unknown')}")
    store.write_heartbeat("starting", last_ok=last_ok_ts)

    while not stop:
        # Hot-reload .env: ONE read per check, reused for fingerprint + parsing.
        try:
            reload = env_watcher.check(require_session=False, environ=environ)
            env_changed, change_desc = reload.changed, reload.description
            fresh_cfg = reload.config
            session_src = fresh_cfg.sources.get("SIS_SESSION_ID", "unknown")

            if fresh_cfg.session_id and fresh_cfg.session_id != active_session_id:
                active_session_id = fresh_cfg.session_id
                client.reconfigure(
                    session_id=active_session_id,
                    user_agent=fresh_cfg.user_agent,
                    base_url=fresh_cfg.base_url,
                    request_delay=fresh_cfg.request_delay,
                )
                expired_since = None
                last_expired_log = 0.0
                last_fetch = 0.0
                active_anomaly = None
                expired_cookie_note = ""
                store.clear_session_alerted()
                now_str = datetime.now().strftime("%H:%M:%S")
                if env_changed:
                    print(
                        f"[{now_str}] [INFO] Detected .env change ({change_desc}; "
                        f"source={session_src}) — updated SIS_SESSION_ID — resuming."
                    )
                else:
                    print(
                        f"[{now_str}] [INFO] Detected updated SIS_SESSION_ID "
                        f"(source={session_src}) — resuming."
                    )
            elif env_changed:
                now_str = datetime.now().strftime("%H:%M:%S")
                if expired_since is not None and fresh_cfg.session_id:
                    # Expired-session .env change: force-clear the cookie jar even though the
                    # session identifier is unchanged, so stale auxiliary cookies are dropped.
                    client.reconfigure(
                        session_id=fresh_cfg.session_id,
                        user_agent=fresh_cfg.user_agent,
                        base_url=fresh_cfg.base_url,
                        request_delay=fresh_cfg.request_delay,
                        reset_cookies=True,
                    )
                    expired_since = None
                    last_expired_log = 0.0
                    last_fetch = 0.0
                    active_anomaly = None
                    expired_cookie_note = ""
                    print(
                        f"[{now_str}] [INFO] Detected .env change ({change_desc}; "
                        f"SIS_SESSION_ID unchanged; source={session_src}) — re-testing session."
                    )
                else:
                    client.reconfigure(
                        user_agent=fresh_cfg.user_agent,
                        base_url=fresh_cfg.base_url,
                        request_delay=fresh_cfg.request_delay,
                    )
                    print(
                        f"[{now_str}] [INFO] Detected .env change ({change_desc}; "
                        f"SIS_SESSION_ID unchanged; source={session_src})."
                    )

            cfg = fresh_cfg
            store.bind_heartbeat(
                keepalive_interval=cfg.keepalive_interval,
                fetch_interval=cfg.fetch_interval,
                healthcheck_max_age=cfg.healthcheck_max_age,
            )
            if cfg.notifier_key != active_notifier_key:
                notifiers = build_notifiers(cfg.raw, timeout=cfg.notifier_timeout)
                active_notifier_key = cfg.notifier_key
        except Exception as exc:
            print(f"[WARN] Could not reload .env: {exc}")

        if expired_since is not None or not active_session_id:
            if expired_since is None:
                expired_since = time.monotonic()
            now_mono = time.monotonic()
            elapsed = now_mono - expired_since
            last_status = "waiting_for_session"
            last_detail = f"expired for {int(elapsed)}s{expired_cookie_note}"
            store.write_heartbeat(last_status, detail=last_detail, last_ok=last_ok_ts)

            log_interval = min(60.0, max(15.0, float(cfg.expired_poll_interval) * 4.0))
            if last_expired_log == 0.0 or (now_mono - last_expired_log) >= log_interval:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{now_str}] [WARN] Waiting for updated SIS_SESSION_ID in {env_path} "
                    f"(expired {int(elapsed)}s / {cfg.session_expired_timeout}s)"
                    f"{expired_cookie_note}."
                )
                last_expired_log = now_mono

            if cfg.session_expired_timeout == 0 or elapsed >= cfg.session_expired_timeout:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{now_str}] [ERROR] Session remained expired for {int(elapsed)}s "
                    "— exiting with code 1 for restart."
                )
                store.write_heartbeat(
                    "exiting_session_expired", detail=last_detail, last_ok=last_ok_ts
                )
                return 1

            for _ in range(cfg.expired_poll_interval):
                if stop or stop_event.is_set():
                    break
                stop_event.wait(1.0)
            continue

        cancel_event = threading.Event()
        client.cancel_event = cancel_event

        def _touch_scan_heartbeat() -> None:
            store.write_heartbeat("scanning", last_ok=last_ok_ts)

        try:
            _touch_scan_heartbeat()

            def _do_tick() -> None:
                nonlocal last_fetch, last_ok_ts, last_status, last_detail, active_anomaly
                roster = client.fetch_roster()
                courses = roster.courses
                if cancel_event.is_set():
                    return
                store.clear_session_alerted()
                now_mono = time.monotonic()
                now_str = datetime.now().strftime("%H:%M:%S")

                if last_fetch == 0.0 or (now_mono - last_fetch) >= cfg.fetch_interval:
                    result = run_cycle(
                        client,
                        courses,
                        store,
                        CycleOptions(
                            base_url=cfg.base_url,
                            track_announcements=cfg.track_announcements,
                            semester=cfg.semester or roster.semester,
                            dry_run=dry_run,
                            max_backups=cfg.max_state_backups,
                            notifier_timeout=cfg.notifier_timeout,
                            cancel_event=cancel_event,
                            on_progress=_touch_scan_heartbeat,
                        ),
                        notifiers,
                    )
                    last_fetch = time.monotonic()
                    if result.anomaly:
                        active_anomaly = result.anomaly
                        last_status = "anomaly"
                        last_detail = result.anomaly
                    else:
                        active_anomaly = None
                        last_ok_ts = time.time()
                        last_status = "ok"
                        last_detail = f"{len(courses)} courses"
                else:
                    last_ok_ts = time.time()
                    if active_anomaly:
                        last_status = "anomaly"
                        last_detail = f"{active_anomaly} (keep-alive OK: {len(courses)} courses)"
                    else:
                        last_status = "ok"
                        last_detail = f"{len(courses)} courses"
                    print(f"[{now_str}] Keep-alive OK ({len(courses)} courses).")

            current_cancel_event = cancel_event
            try:
                run_with_timeout(
                    _do_tick,
                    timeout=cfg.cycle_timeout,
                    cancel_event=cancel_event,
                    stop_event=stop_event,
                )
            finally:
                current_cancel_event = None
            consecutive_timeouts = 0
            store.write_heartbeat(last_status, detail=last_detail, last_ok=last_ok_ts)

        except CycleCancelledError:
            break

        except SessionExpiredError as exc:
            expired_since = time.monotonic()
            last_expired_log = 0.0
            # Cookie NAMES only (never values) so the operator can tell which cookie the
            # portal rotated. Kept for the waiting_for_session heartbeat lines too.
            cookie_note = describe_observed_cookie_names(exc)
            expired_cookie_note = cookie_note
            last_status = "waiting_for_session"
            last_detail = f"{exc}{cookie_note}"

            if not store.was_session_alerted(active_session_id):
                store.mark_session_alerted(active_session_id)
                dispatch_all(
                    notifiers,
                    Notification(
                        event="session_expired",
                        title="SIS session expired",
                        message=(
                            f"{exc}{cookie_note}\n"
                            f"Log in at {cfg.base_url}/ and update SIS_SESSION_ID in .env. "
                            "Tracker will auto-resume once saved."
                        ),
                        priority="urgent",
                        tags=("warning", "key"),
                        url=f"{cfg.base_url}/",
                    ),
                    timeout=cfg.notifier_timeout,
                )
            else:
                print(
                    f"[WARN] Session expired ({exc}){cookie_note} — alert already sent "
                    "for current SIS_SESSION_ID."
                )

            store.write_heartbeat(last_status, detail=last_detail, last_ok=last_ok_ts)
            if cfg.session_expired_timeout == 0:
                store.write_heartbeat(
                    "exiting_session_expired", detail=last_detail, last_ok=last_ok_ts
                )
                return 1
            continue

        except CycleTimeoutError as exc:
            consecutive_timeouts += 1
            if exc.wedged or consecutive_timeouts >= 3:
                print(
                    f"[ERROR] {exc} (wedged={exc.wedged}, consecutive={consecutive_timeouts}) "
                    "— exiting with code 1 for restart."
                )
                store.write_heartbeat("wedged_exiting", detail=str(exc), last_ok=last_ok_ts)
                return 1
            last_status = "timeout"
            last_detail = str(exc)
            print(f"[WARN] {exc} — will retry next tick.")
            store.write_heartbeat(last_status, detail=last_detail, last_ok=last_ok_ts)

        except (PortalHttpError, StateLockError, ValueError) as exc:
            last_status = "portal_error"
            last_detail = str(exc)
            print(f"[WARN] Portal/state error (will retry next tick): {exc}")
            store.write_heartbeat(last_status, detail=last_detail, last_ok=last_ok_ts)

        for elapsed_sleep in range(cfg.keepalive_interval):
            if stop or stop_event.is_set():
                break
            if elapsed_sleep > 0 and elapsed_sleep % 15 == 0:
                store.write_heartbeat(last_status, detail=last_detail, last_ok=last_ok_ts)
            stop_event.wait(1.0)

    print("Tracker stopped.")
    return 0


def main(environ: Optional[Mapping[str, str]] = None) -> int:
    ap = argparse.ArgumentParser(description="COMSATS CUOnline Portal Tracker")
    ap.add_argument("--once", action="store_true", help="Run a single full scan and exit")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan without writing state.json",
    )
    ap.add_argument(
        "--reset-state",
        action="store_true",
        help="Accept the current portal snapshot as a fresh baseline (use with --once)",
    )
    ap.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a test notification to all configured backends and exit",
    )
    ap.add_argument(
        "--healthcheck",
        action="store_true",
        help="Verify daemon heartbeat freshness and exit 0 if healthy, 1 otherwise",
    )
    args = ap.parse_args()

    if args.reset_state and not args.once:
        print("[ERROR] --reset-state must be used together with --once.")
        return 1

    root = Path(__file__).resolve().parent
    env_path = resolve_env_file(root, environ)
    state_path = resolve_state_file(root, environ)

    if args.healthcheck:
        cfg_vals = load_env_dict(env_path, environ=environ)
        max_age_override = parse_int(cfg_vals, "HEALTHCHECK_MAX_AGE", 0, minimum=0) or None
        store = RuntimeStore(state_path, environ=environ)
        ok, msg = store.check_healthcheck(max_age_override=max_age_override)
        print(msg)
        return 0 if ok else 1

    if args.test_notify:
        cfg = load_config(env_path, require_session=False, environ=environ)
        notifiers = build_notifiers(cfg.raw, timeout=cfg.notifier_timeout)
        dispatch_all(
            notifiers,
            Notification(
                event="test",
                title="COMSATS Portal Tracker test",
                message="Your notification channel is configured and working.",
                priority="default",
                tags=("white_check_mark",),
                url=f"{cfg.base_url}/",
            ),
            timeout=cfg.notifier_timeout,
        )
        return 0

    if args.once:
        cfg = load_config(env_path, require_session=True, environ=environ)
        notifiers = build_notifiers(cfg.raw, timeout=cfg.notifier_timeout)
        cancel_event = threading.Event()
        client = SisClient(
            session_id=cfg.session_id,
            base_url=cfg.base_url,
            user_agent=cfg.user_agent,
            request_delay=cfg.request_delay,
            cancel_event=cancel_event,
        )
        try:
            def _once_tick() -> CycleResult:
                roster = client.fetch_roster()
                return run_cycle(
                    client,
                    roster.courses,
                    RuntimeStore(
                        state_path, environ=environ, max_backups=cfg.max_state_backups
                    ),
                    CycleOptions(
                        base_url=cfg.base_url,
                        track_announcements=cfg.track_announcements,
                        semester=cfg.semester or roster.semester,
                        reset_state=args.reset_state,
                        dry_run=args.dry_run,
                        max_backups=cfg.max_state_backups,
                        notifier_timeout=cfg.notifier_timeout,
                        cancel_event=cancel_event,
                    ),
                    notifiers,
                )

            result = run_with_timeout(
                _once_tick, timeout=cfg.cycle_timeout, cancel_event=cancel_event
            )
            return 1 if result.anomaly else 0
        except SessionExpiredError as exc:
            cookie_note = describe_observed_cookie_names(exc)
            print(f"[ERROR] Session expired: {exc}{cookie_note}")
            dispatch_all(
                notifiers,
                Notification(
                    event="session_expired",
                    title="SIS session expired",
                    message=f"{exc}{cookie_note}",
                    priority="urgent",
                    tags=("warning",),
                    url=f"{cfg.base_url}/",
                ),
                timeout=cfg.notifier_timeout,
            )
            return 1
        except (PortalHttpError, CycleTimeoutError, StateLockError, ValueError) as exc:
            print(f"[ERROR] Scan failed: {exc}")
            return 1

    return run_daemon(env_path, state_path, dry_run=args.dry_run, environ=environ)


if __name__ == "__main__":
    sys.exit(main())
