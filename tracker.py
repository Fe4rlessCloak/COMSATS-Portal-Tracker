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
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, TypeVar

from config import (
    EnvWatcher,
    TrackerConfig,
    load_config,
    load_env_dict,
    parse_int,
    resolve_env_file,
    resolve_heartbeat_file,
    resolve_state_file,
)
from notifiers import BaseNotifier, Notification, build_notifiers, dispatch_all
from runtime_store import RuntimeStore, StateLockError
from sis_client import (
    Course,
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    PortalHttpError,
    SessionExpiredError,
    SisClient,
)

T = TypeVar("T")


class CycleTimeoutError(RuntimeError):
    """Raised when a polling/scan cycle exceeds its hard deadline."""

    def __init__(self, message: str, wedged: bool = False) -> None:
        super().__init__(message)
        self.wedged = wedged


class CycleCancelledError(RuntimeError):
    """Raised when a running cycle is interrupted by a shutdown signal."""


@dataclass
class ScanResult:
    state: Dict[str, Any]
    partial: bool = False
    failed_courses: List[str] = field(default_factory=list)
    session_expired: bool = False
    session_expired_error: Optional[str] = None
    sample_marks_html: str = ""
    sample_marks_preview: str = ""
    sample_marks_course: str = ""
    set_cookie_names: List[str] = field(default_factory=list)


@dataclass
class CycleResult:
    saved: bool
    anomaly: Optional[str] = None
    notifications: List[Notification] = field(default_factory=list)


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


def _norm_semester(val: str) -> str:
    return " ".join(val.strip().lower().split())


def detect_semester_rollover(
    old_state: Optional[Dict[str, Any]], new_state: Dict[str, Any]
) -> Optional[Tuple[str, str]]:
    """Return (old_semester, new_semester) when state transitions to a new academic semester."""
    if old_state is None:
        return None
    old_raw = str(old_state.get("semester") or "").strip()
    new_raw = str(new_state.get("semester") or "").strip()
    old_norm = _norm_semester(old_raw)
    new_norm = _norm_semester(new_raw)
    if old_norm and new_norm:
        return (old_raw, new_raw) if old_norm != new_norm else None

    old_courses = old_state.get("courses", {})
    new_courses = new_state.get("courses", {})
    if old_courses and new_courses and set(old_courses).isdisjoint(new_courses):
        return (old_raw or "previous semester", new_raw or "new semester")
    return None


def validate_snapshot(
    old_state: Optional[Dict[str, Any]],
    scan_result: ScanResult,
    *,
    reset_state: bool = False,
) -> Optional[str]:
    """Validate snapshot plausibility before persisting; returns an error reason or None."""
    if scan_result.session_expired:
        return f"Session expired mid-scan: {scan_result.session_expired_error}"
    if scan_result.partial:
        return (
            f"Partial scan ({len(scan_result.failed_courses)} course(s) failed: "
            f"{', '.join(scan_result.failed_courses)})"
        )

    new_c, new_m, new_r = summarize_totals(scan_result.state)
    if new_c == 0:
        return "Scan returned 0 courses."

    if reset_state:
        return None

    if old_state is not None:
        if detect_semester_rollover(old_state, scan_result.state) is not None:
            return None
        old_c, old_m, old_r = summarize_totals(old_state)
        if old_m > 0 and new_m == 0:
            return f"Total marks dropped from {old_m} to 0 (degenerate scan)."
        if old_c >= 3 and new_c < old_c * 0.5:
            return f"Course count dropped sharply from {old_c} to {new_c}."
        if old_m >= 5 and new_m < old_m * 0.5:
            return f"Total marks dropped sharply from {old_m} to {new_m}."
        if old_r >= 5 and new_r < old_r * 0.5:
            return f"Total resources dropped sharply from {old_r} to {new_r}."

    return None


def diff_states(
    old_state: Dict[str, Any],
    new_state: Dict[str, Any],
    base_url: str,
    track_announcements: bool = False,
) -> List[Notification]:
    """Compare old and new snapshots and return Notifications for added, updated, or removed items."""
    notes: List[Notification] = []
    old_courses = old_state.get("courses", {})
    new_courses = new_state.get("courses", {})

    for cid, new_c in new_courses.items():
        old_c = old_courses.get(cid)
        label = new_c.get("label") or cid

        # Skip alerting on brand-new course appearance only if desired; usually helpful to scan its items.
        old_marks = (old_c or {}).get("marks", {})
        new_marks = new_c.get("marks", {})
        mark_lines: List[str] = []
        changed_marks: List[Dict[str, Any]] = []

        for key, item in new_marks.items():
            prev = old_marks.get(key)
            score_str = f"{item['obtained']} / {item['total']}".strip(" /")
            if prev is None:
                mark_lines.append(f"+ [{item['section']}] {item['title']}: {score_str}")
                changed_marks.append({"change": "added", **item})
            elif (
                prev.get("obtained") != item.get("obtained")
                or prev.get("total") != item.get("total")
            ):
                old_score = f"{prev.get('obtained', '')} / {prev.get('total', '')}".strip(" /")
                mark_lines.append(
                    f"~ [{item['section']}] {item['title']}: {old_score} -> {score_str}"
                )
                changed_marks.append({"change": "updated", "previous": prev, **item})

        if mark_lines:
            notes.append(
                Notification(
                    event="marks_updated",
                    title=f"Marks update: {label}",
                    message="\n".join(mark_lines),
                    priority="high",
                    tags=("mortar_board", "chart_with_upwards_trend"),
                    url=f"{base_url}/Courses/SetCourse/{cid}",
                    data={"course_id": cid, "course": label, "items": changed_marks},
                )
            )

        removed_mark_lines: List[str] = []
        removed_marks: List[Dict[str, Any]] = []
        for key, prev in old_marks.items():
            if key not in new_marks:
                score_str = f"{prev.get('obtained', '')} / {prev.get('total', '')}".strip(" /")
                section = prev.get("section", "")
                title = prev.get("title", key)
                removed_mark_lines.append(f"- [{section}] {title}: {score_str}")
                removed_marks.append({"change": "removed", **prev})

        if removed_mark_lines:
            notes.append(
                Notification(
                    event="marks_removed",
                    title=f"Marks removed: {label}",
                    message="\n".join(removed_mark_lines),
                    priority="high",
                    tags=("warning", "chart_with_downwards_trend"),
                    url=f"{base_url}/Courses/SetCourse/{cid}",
                    data={"course_id": cid, "course": label, "items": removed_marks},
                )
            )

        old_res = (old_c or {}).get("resources", {})
        new_res = new_c.get("resources", {})
        res_lines: List[str] = []
        added_res: List[Dict[str, Any]] = []
        first_res_url: Optional[str] = None

        for key, item in new_res.items():
            if key not in old_res:
                meta_parts = [p for p in (item.get("material_type"), item.get("size"), item.get("date_text")) if p]
                meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
                href = item.get("href", "")
                full_href = urllib.parse.urljoin(f"{base_url}/", href) if href else ""
                if not first_res_url and full_href:
                    first_res_url = full_href
                line = f"+ {item['title']}{meta}"
                if full_href:
                    line += f"\n  {full_href}"
                res_lines.append(line)
                added_res.append(item)

        if res_lines:
            notes.append(
                Notification(
                    event="resource_added",
                    title=f"New material: {label}",
                    message="\n".join(res_lines),
                    priority="default",
                    tags=("books", "page_facing_up"),
                    url=first_res_url or f"{base_url}/Courses/SetCourse/{cid}",
                    data={"course_id": cid, "course": label, "items": added_res},
                )
            )

        removed_res_lines: List[str] = []
        removed_res: List[Dict[str, Any]] = []
        for key, prev in old_res.items():
            if key not in new_res:
                meta_parts = [
                    p
                    for p in (prev.get("material_type"), prev.get("size"), prev.get("date_text"))
                    if p
                ]
                meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
                removed_res_lines.append(f"- {prev.get('title', key)}{meta}")
                removed_res.append(prev)

        if removed_res_lines:
            notes.append(
                Notification(
                    event="resource_removed",
                    title=f"Material removed: {label}",
                    message="\n".join(removed_res_lines),
                    priority="default",
                    tags=("warning", "books"),
                    url=f"{base_url}/Courses/SetCourse/{cid}",
                    data={"course_id": cid, "course": label, "items": removed_res},
                )
            )

        if track_announcements:
            old_ann = (old_c or {}).get("announcements", {})
            new_ann = new_c.get("announcements", {})
            ann_lines: List[str] = []
            added_ann: List[Dict[str, Any]] = []
            for key, item in new_ann.items():
                if key not in old_ann:
                    snippet = item.get("body", "")[:180]
                    ann_lines.append(f"+ {item.get('title', 'Announcement')}\n  {snippet}")
                    added_ann.append(item)
            if ann_lines:
                notes.append(
                    Notification(
                        event="announcement_added",
                        title=f"Announcement: {label}",
                        message="\n".join(ann_lines),
                        priority="default",
                        tags=("loudspeaker",),
                        url=f"{base_url}/Courses/SetCourse/{cid}",
                        data={"course_id": cid, "course": label, "items": added_ann},
                    )
                )

            removed_ann_lines: List[str] = []
            removed_ann: List[Dict[str, Any]] = []
            for key, prev in old_ann.items():
                if key not in new_ann:
                    removed_ann_lines.append(f"- {prev.get('title', 'Announcement')}")
                    removed_ann.append(prev)
            if removed_ann_lines:
                notes.append(
                    Notification(
                        event="announcement_removed",
                        title=f"Announcement removed: {label}",
                        message="\n".join(removed_ann_lines),
                        priority="default",
                        tags=("loudspeaker",),
                        url=f"{base_url}/Courses/SetCourse/{cid}",
                        data={"course_id": cid, "course": label, "items": removed_ann},
                    )
                )

    for cid, old_c in old_courses.items():
        if cid not in new_courses:
            old_label = old_c.get("label") or cid
            old_marks = old_c.get("marks", {})
            old_res = old_c.get("resources", {})
            notes.append(
                Notification(
                    event="course_removed",
                    title=f"Course removed: {old_label}",
                    message=(
                        f"Course {old_label} is no longer present "
                        f"(had {len(old_marks)} marks, {len(old_res)} resources)."
                    ),
                    priority="high",
                    tags=("warning",),
                    url=f"{base_url}/Courses",
                    data={
                        "course_id": cid,
                        "course": old_label,
                        "marks_count": len(old_marks),
                        "resources_count": len(old_res),
                    },
                )
            )

    return notes


def summarize_totals(state: Dict[str, Any]) -> Tuple[int, int, int]:
    courses = state.get("courses", {})
    marks = sum(len(c.get("marks", {})) for c in courses.values())
    resources = sum(len(c.get("resources", {})) for c in courses.values())
    return len(courses), marks, resources


def run_cycle(
    client: SisClient,
    courses: List[Course],
    state_path: Path,
    base_url: str,
    track_announcements: bool,
    notifiers: List[BaseNotifier],
    dry_run: bool = False,
    *,
    store: Optional[RuntimeStore] = None,
    max_backups: Optional[int] = None,
    notifier_timeout: Optional[float] = None,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[], None]] = None,
    lock_timeout: float = 30.0,
    notify_anomaly: bool = True,
    semester: Optional[str] = None,
    reset_state: bool = False,
) -> CycleResult:
    """Run one locked load -> multi-course fetch -> validate -> save -> diff + notify cycle."""
    store = store or RuntimeStore(state_path)
    with store.lock(timeout=lock_timeout):
        old_state = store.load_state()
        scan_result = scan_all_courses(
            client,
            courses,
            track_announcements=track_announcements,
            previous_state=old_state,
            cancel_event=cancel_event,
            on_progress=on_progress,
            semester=semester,
        )

        if cancel_event is not None and cancel_event.is_set():
            return CycleResult(saved=False, anomaly="Cancelled")

        if scan_result.session_expired:
            print("[WARN] Session expired mid-scan — preserving existing baseline without saving.")
            raise SessionExpiredError(
                scan_result.session_expired_error or "Session expired mid-scan."
            )

        anomaly = validate_snapshot(old_state, scan_result, reset_state=reset_state)
        if anomaly is not None:
            new_c, new_m, new_r = summarize_totals(scan_result.state)
            old_c, old_m, old_r = (
                summarize_totals(old_state) if old_state is not None else (0, 0, 0)
            )
            now_iso = datetime.now(timezone.utc).isoformat()
            anomaly_key = anomaly.strip()
            prev_anom = store.load_anomaly()
            if prev_anom is not None and str(prev_anom.get("key") or "") == anomaly_key:
                occurrence = int(prev_anom.get("count") or 1) + 1
                first_seen = str(prev_anom.get("first_seen") or now_iso)
                should_notify = False
            else:
                occurrence = 1
                first_seen = now_iso
                should_notify = notify_anomaly

            if not dry_run:
                store.save_anomaly(
                    {
                        "key": anomaly_key,
                        "reason": anomaly,
                        "first_seen": first_seen,
                        "last_seen": now_iso,
                        "count": occurrence,
                        "scanned": {"courses": new_c, "marks": new_m, "resources": new_r},
                    }
                )
                store.write_anomaly_diagnostics(
                    reason=anomaly,
                    occurrence=occurrence,
                    scanned_totals={"courses": new_c, "marks": new_m, "resources": new_r},
                    baseline_totals={"courses": old_c, "marks": old_m, "resources": old_r},
                    failed_courses=scan_result.failed_courses,
                    set_cookie_names=scan_result.set_cookie_names,
                    sample_marks_course=scan_result.sample_marks_course,
                    marks_html=scan_result.sample_marks_html,
                )

            preview = scan_result.sample_marks_preview
            if len(preview) > 240:
                preview = preview[:237].rstrip() + "..."
            dedup_note = (
                " (duplicate alert suppressed)"
                if (not should_notify and notify_anomaly)
                else ""
            )
            print(
                f"[WARN] Rejected snapshot ({anomaly}; scanned={new_c}c/{new_m}m/{new_r}r; "
                f"occurrence #{occurrence}){dedup_note} — preserving existing baseline."
            )
            if preview:
                course_label = scan_result.sample_marks_course or "MarksSummary"
                print(f"[INFO] Anomaly page preview ({course_label}): {preview}")

            msg_lines = [
                anomaly,
                (
                    f"Scanned: {new_c} courses, {new_m} marks, {new_r} resources "
                    f"(baseline: {old_c} courses, {old_m} marks, {old_r} resources)."
                ),
            ]
            if scan_result.set_cookie_names:
                msg_lines.append(
                    f"Set-Cookie names observed: {', '.join(scan_result.set_cookie_names)}"
                )
            if preview:
                msg_lines.append(f"Page preview: {preview}")
            msg_lines.append(
                "Existing baseline was NOT overwritten. "
                "Further identical alerts are suppressed until recovery."
            )
            anomaly_note = Notification(
                event="scan_anomaly",
                title="Portal scan anomaly (state preserved)",
                message="\n".join(msg_lines),
                priority="high",
                tags=("warning",),
                url=f"{base_url}/Courses",
                data={
                    "reason": anomaly,
                    "occurrence": occurrence,
                    "failed_courses": scan_result.failed_courses,
                    "scanned": {"courses": new_c, "marks": new_m, "resources": new_r},
                    "baseline": {"courses": old_c, "marks": old_m, "resources": old_r},
                    "set_cookie_names": list(scan_result.set_cookie_names),
                },
            )
            if should_notify:
                dispatch_all(notifiers, anomaly_note, timeout=notifier_timeout)
                return CycleResult(saved=False, anomaly=anomaly, notifications=[anomaly_note])
            return CycleResult(saved=False, anomaly=anomaly, notifications=[])

        new_state = scan_result.state
        prev_anom = store.load_anomaly()
        c_count, m_count, r_count = summarize_totals(new_state)
        recovery_note: Optional[Notification] = None
        if prev_anom is not None:
            prev_reason = str(prev_anom.get("reason") or "previous scan anomaly")
            prev_count = int(prev_anom.get("count") or 1)
            recovery_note = Notification(
                event="scan_recovered",
                title="Portal scan recovered",
                message=(
                    f"Normal portal data returned after {prev_count} rejected scan(s) "
                    f"({prev_reason}).\n"
                    f"Current totals: {c_count} courses, {m_count} marks, {r_count} resources."
                ),
                priority="default",
                tags=("white_check_mark",),
                url=f"{base_url}/Courses",
                data={
                    "previous_reason": prev_reason,
                    "rejected_scans": prev_count,
                    "courses": c_count,
                    "marks": m_count,
                    "resources": r_count,
                },
            )

        rollover = None if reset_state else detect_semester_rollover(old_state, new_state)

        if old_state is None or reset_state:
            if cancel_event is not None and cancel_event.is_set():
                return CycleResult(saved=False, anomaly="Cancelled")
            if not dry_run:
                store.save_state(new_state, max_backups=max_backups)
                if prev_anom is not None:
                    store.clear_anomaly()
            new_sem = str(new_state.get("semester") or "").strip()
            baseline_data: Dict[str, Any] = {
                "courses": c_count,
                "marks": m_count,
                "resources": r_count,
            }
            if new_sem:
                baseline_data["semester"] = new_sem
                baseline_msg = (
                    f"Baseline saved for {c_count} courses "
                    f"({new_sem}: {m_count} marks, {r_count} resources)."
                )
            else:
                baseline_msg = (
                    f"Baseline saved for {c_count} courses "
                    f"({m_count} marks, {r_count} resources)."
                )
            baseline_note = Notification(
                event="baseline_saved",
                title="COMSATS Portal Tracker started",
                message=baseline_msg,
                priority="low",
                tags=("white_check_mark",),
                url=f"{base_url}/Courses",
                data=baseline_data,
            )
            sent_notes: List[Notification] = []
            if recovery_note is not None and notify_anomaly:
                dispatch_all(notifiers, recovery_note, timeout=notifier_timeout)
                sent_notes.append(recovery_note)
            dispatch_all(notifiers, baseline_note, timeout=notifier_timeout)
            sent_notes.append(baseline_note)
            return CycleResult(saved=not dry_run, notifications=sent_notes)

        if rollover is not None:
            if cancel_event is not None and cancel_event.is_set():
                return CycleResult(saved=False, anomaly="Cancelled")
            old_sem_label, new_sem_label = rollover
            if not dry_run:
                store.save_state(new_state, max_backups=max_backups)
                if prev_anom is not None:
                    store.clear_anomaly()
            course_lines = "\n".join(
                f"- {c.get('label') or cid}"
                for cid, c in new_state.get("courses", {}).items()
            )
            rollover_title = (
                "New semester detected"
                if new_sem_label == "new semester"
                else f"New semester detected: {new_sem_label}"
            )
            rollover_msg = (
                f"Transition: {old_sem_label} -> {new_sem_label}\n"
                f"Saved fresh baseline for {c_count} courses "
                f"({m_count} marks, {r_count} resources)."
            )
            if course_lines:
                rollover_msg += f"\n{course_lines}"
            rollover_note = Notification(
                event="semester_rollover",
                title=rollover_title,
                message=rollover_msg,
                priority="default",
                tags=("mortar_board", "white_check_mark"),
                url=f"{base_url}/Courses",
                data={
                    "previous_semester": old_sem_label,
                    "semester": new_sem_label,
                    "courses": c_count,
                    "marks": m_count,
                    "resources": r_count,
                },
            )
            sent_rollover: List[Notification] = []
            if recovery_note is not None and notify_anomaly:
                dispatch_all(notifiers, recovery_note, timeout=notifier_timeout)
                sent_rollover.append(recovery_note)
            dispatch_all(notifiers, rollover_note, timeout=notifier_timeout)
            sent_rollover.append(rollover_note)
            return CycleResult(saved=not dry_run, notifications=sent_rollover)

        if not new_state.get("semester") and old_state.get("semester"):
            new_state["semester"] = str(old_state["semester"]).strip()

        notifications = diff_states(
            old_state,
            new_state,
            base_url=base_url,
            track_announcements=track_announcements,
        )
        if cancel_event is not None and cancel_event.is_set():
            return CycleResult(saved=False, anomaly="Cancelled")
        if not dry_run:
            store.save_state(new_state, max_backups=max_backups)
            if prev_anom is not None:
                store.clear_anomaly()

        all_notes: List[Notification] = []
        if recovery_note is not None and notify_anomaly:
            all_notes.append(recovery_note)
        all_notes.extend(notifications)

        if all_notes:
            for note in all_notes:
                dispatch_all(notifiers, note, timeout=notifier_timeout)
        else:
            now = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{now}] Scanned {c_count} courses ({m_count} marks, {r_count} resources) — no changes."
            )
        return CycleResult(saved=not dry_run, notifications=all_notes)


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
        keepalive_interval=cfg.keepalive_interval,
        fetch_interval=cfg.fetch_interval,
        healthcheck_max_age=cfg.healthcheck_max_age,
        max_backups=cfg.max_state_backups,
    )
    env_watcher = EnvWatcher(env_path, environ)

    def _load_env_snapshot(
        env_file: Path, raw: Optional[bytes], require_session: bool
    ) -> TrackerConfig:
        """Parse an already-read .env snapshot so the daemon reads the file only once."""
        if environ is None:
            return load_config(env_file, require_session=require_session, raw=raw)
        return load_config(
            env_file, require_session=require_session, raw=raw, environ=environ
        )

    last_fetch = 0.0
    expired_since: Optional[float] = None
    last_expired_log = 0.0
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
            reload = env_watcher.check(
                require_session=False, environ=environ, loader=_load_env_snapshot
            )
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
            last_detail = f"expired for {int(elapsed)}s"
            store.write_heartbeat(last_status, detail=last_detail, last_ok=last_ok_ts)

            log_interval = min(60.0, max(15.0, float(cfg.expired_poll_interval) * 4.0))
            if last_expired_log == 0.0 or (now_mono - last_expired_log) >= log_interval:
                now_str = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{now_str}] [WARN] Waiting for updated SIS_SESSION_ID in {env_path} "
                    f"(expired {int(elapsed)}s / {cfg.session_expired_timeout}s)."
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
                        state_path=state_path,
                        base_url=cfg.base_url,
                        track_announcements=cfg.track_announcements,
                        notifiers=notifiers,
                        dry_run=dry_run,
                        store=store,
                        max_backups=cfg.max_state_backups,
                        notifier_timeout=cfg.notifier_timeout,
                        cancel_event=cancel_event,
                        on_progress=_touch_scan_heartbeat,
                        semester=cfg.semester or roster.semester,
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
            last_status = "waiting_for_session"
            last_detail = str(exc)

            if not store.was_session_alerted(active_session_id):
                store.mark_session_alerted(active_session_id)
                dispatch_all(
                    notifiers,
                    Notification(
                        event="session_expired",
                        title="SIS session expired",
                        message=(
                            f"{exc}\nLog in at {cfg.base_url}/ and update SIS_SESSION_ID in .env. "
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
                    f"[WARN] Session expired ({exc}) — alert already sent for current SIS_SESSION_ID."
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
        store = RuntimeStore(state_path, resolve_heartbeat_file(state_path, environ))
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
                    state_path=state_path,
                    base_url=cfg.base_url,
                    track_announcements=cfg.track_announcements,
                    notifiers=notifiers,
                    dry_run=args.dry_run,
                    store=RuntimeStore(state_path, max_backups=cfg.max_state_backups),
                    max_backups=cfg.max_state_backups,
                    notifier_timeout=cfg.notifier_timeout,
                    cancel_event=cancel_event,
                    semester=cfg.semester or roster.semester,
                    reset_state=args.reset_state,
                )

            result = run_with_timeout(
                _once_tick, timeout=cfg.cycle_timeout, cancel_event=cancel_event
            )
            return 1 if result.anomaly else 0
        except SessionExpiredError as exc:
            print(f"[ERROR] Session expired: {exc}")
            dispatch_all(
                notifiers,
                Notification(
                    event="session_expired",
                    title="SIS session expired",
                    message=str(exc),
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
