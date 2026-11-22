#!/usr/bin/env python3
"""Pure snapshot transition policy for the COMSATS Portal Tracker.

This module decides what to do with a completed scan and describes the result; it never
performs I/O. No network, no filesystem, no locking, no logging, no notification
dispatch. ``tracker.run_cycle`` loads the baseline and the prior anomaly marker, calls
:func:`compute_transition`, then executes the returned plan through ``RuntimeStore`` and
``notifiers.dispatch_all``.

Public surface
--------------
::

    compute_transition(previous_state, scan, prior_anomaly, now, options) -> TransitionOutcome

    TransitionOptions(base_url, track_announcements=False, reset_state=False,
                      notify_anomaly=True)
    TransitionOutcome(kind, persist_state, state, anomaly_action, notifications,
                      baseline_totals, scanned_totals, summary)
    AnomalyAction(kind, marker, diagnostics) / AnomalyDiagnostics(...)

    ScanResult, CycleResult          # carrier dataclasses re-imported by tracker.py
    validate_snapshot(...), detect_semester_rollover(...), diff_states(...),
    summarize_totals(...), normalize_semester(...)

Transition kinds (``TransitionOutcome.kind``)
---------------------------------------------
``baseline_saved``   no usable baseline (first run) or ``reset_state`` accepted a snapshot
``semester_rollover`` a new academic semester was detected
``diff_applied``      same-semester snapshot accepted; diff notifications produced
``anomaly_rejected``  snapshot refused; baseline preserved, anomaly marker maintained

Anomaly actions (``TransitionOutcome.anomaly_action.kind``)
------------------------------------------------------------
``none``    leave the on-disk marker alone
``record``  write the updated marker plus the scrubbed diagnostic payload
``clear``   drop the marker because a fresh baseline was committed

Python 3.9+ standard library only.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from notifiers import Notification

__all__ = [
    "KIND_BASELINE_SAVED",
    "KIND_SEMESTER_ROLLOVER",
    "KIND_DIFF_APPLIED",
    "KIND_ANOMALY_REJECTED",
    "TRANSITION_KINDS",
    "ANOMALY_NONE",
    "ANOMALY_RECORD",
    "ANOMALY_CLEAR",
    "AnomalyDiagnostics",
    "AnomalyAction",
    "TransitionOptions",
    "TransitionOutcome",
    "ScanResult",
    "CycleResult",
    "compute_transition",
    "detect_semester_rollover",
    "validate_snapshot",
    "diff_states",
    "summarize_totals",
    "normalize_semester",
]

# ---------------------------------------------------------------- outcome kinds

KIND_BASELINE_SAVED = "baseline_saved"
KIND_SEMESTER_ROLLOVER = "semester_rollover"
KIND_DIFF_APPLIED = "diff_applied"
KIND_ANOMALY_REJECTED = "anomaly_rejected"

TRANSITION_KINDS = frozenset(
    {
        KIND_BASELINE_SAVED,
        KIND_SEMESTER_ROLLOVER,
        KIND_DIFF_APPLIED,
        KIND_ANOMALY_REJECTED,
    }
)

# --------------------------------------------------------------- anomaly kinds

ANOMALY_NONE = "none"
ANOMALY_RECORD = "record"
ANOMALY_CLEAR = "clear"

ANOMALY_ACTIONS = frozenset({ANOMALY_NONE, ANOMALY_RECORD, ANOMALY_CLEAR})

# The daemon truncates the diagnostic page preview to this many characters before it
# reaches a notification or a log line. Kept here so the pure plan and the I/O layer
# can never disagree about what "the preview" is.
PREVIEW_LIMIT = 240


# ------------------------------------------------------------ carrier dataclass


@dataclass
class ScanResult:
    """Everything one full multi-course scan observed.

    ``sample_marks_html``/``sample_marks_preview`` are already scrubbed by the Portal
    module and are only populated for courses that returned zero marks.
    """

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
    """What one cycle did, as reported back to the daemon and the CLI."""

    saved: bool
    anomaly: Optional[str] = None
    notifications: List[Notification] = field(default_factory=list)


# ------------------------------------------------------------------- transition


@dataclass(frozen=True)
class TransitionOptions:
    """Policy knobs for one transition (mirrors the run configuration)."""

    base_url: str
    track_announcements: bool = False
    reset_state: bool = False
    notify_anomaly: bool = True


@dataclass(frozen=True)
class AnomalyDiagnostics:
    """Payload the store persists as ``data/last_anomaly.json`` (+ scrubbed HTML).

    ``marks_html`` is already scrubbed and ``marks_preview`` is its credential-free text
    snippet; both come straight from ``CourseSnapshotResult`` inside the Portal module, so
    the store writes them without re-scrubbing.
    """

    reason: str
    occurrence: int
    scanned_totals: Dict[str, int]
    baseline_totals: Dict[str, int]
    failed_courses: Tuple[str, ...] = ()
    set_cookie_names: Tuple[str, ...] = ()
    sample_marks_course: str = ""
    marks_html: str = ""
    marks_preview: str = ""


@dataclass(frozen=True)
class AnomalyAction:
    """What the store should do with ``data/.anomaly_alerted`` (and diagnostics)."""

    kind: str = ANOMALY_NONE
    marker: Optional[Dict[str, Any]] = None
    diagnostics: Optional[AnomalyDiagnostics] = None

    def __post_init__(self) -> None:
        if self.kind not in ANOMALY_ACTIONS:
            raise ValueError(f"unknown anomaly action kind: {self.kind!r}")


@dataclass(frozen=True)
class TransitionOutcome:
    """The complete, I/O-free plan for one scan cycle."""

    kind: str
    persist_state: bool
    state: Optional[Dict[str, Any]]
    anomaly_action: AnomalyAction
    notifications: List[Notification]
    baseline_totals: Tuple[int, int, int]
    scanned_totals: Tuple[int, int, int]
    summary: Dict[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in TRANSITION_KINDS:
            raise ValueError(f"unknown transition kind: {self.kind!r}")


# ------------------------------------------------------------- pure primitives


def normalize_semester(val: str) -> str:
    """Case/whitespace-insensitive semester label used for rollover comparison."""
    return " ".join(val.strip().lower().split())


# Historical private name, still used by tracker.py and the tests that predate this
# module.
_norm_semester = normalize_semester


def summarize_totals(state: Dict[str, Any]) -> Tuple[int, int, int]:
    """Return ``(courses, marks, resources)`` counts for a snapshot."""
    courses = state.get("courses", {})
    marks = sum(len(c.get("marks", {})) for c in courses.values())
    resources = sum(len(c.get("resources", {})) for c in courses.values())
    return len(courses), marks, resources


def detect_semester_rollover(
    old_state: Optional[Dict[str, Any]], new_state: Dict[str, Any]
) -> Optional[Tuple[str, str]]:
    """Return ``(old_semester, new_semester)`` when state moves to a new semester.

    Two independent signals: a differing normalized semester label, or non-empty and
    fully disjoint course ID sets (legacy state files carry no semester label).
    """
    if old_state is None:
        return None
    old_raw = str(old_state.get("semester") or "").strip()
    new_raw = str(new_state.get("semester") or "").strip()
    old_norm = normalize_semester(old_raw)
    new_norm = normalize_semester(new_raw)
    if old_norm and new_norm:
        return (old_raw, new_raw) if old_norm != new_norm else None

    old_courses = old_state.get("courses", {})
    new_courses = new_state.get("courses", {})
    if old_courses and new_courses and set(old_courses).isdisjoint(new_courses):
        return (old_raw or "previous semester", new_raw or "new semester")
    return None


def _rejection_reason(
    old_state: Optional[Dict[str, Any]],
    scan_result: ScanResult,
    baseline_totals: Tuple[int, int, int],
    scanned_totals: Tuple[int, int, int],
    rollover: Optional[Tuple[str, str]],
    reset_state: bool,
) -> Optional[str]:
    """Plausibility gate. Totals and rollover are passed in, never recomputed.

    Mid-scan session expiry is deliberately **not** handled here: ``run_cycle`` raises
    ``SessionExpiredError`` before ``compute_transition`` is ever reached, so expiry has
    exactly one owner (see :func:`validate_snapshot` for the standalone rule).
    """
    if scan_result.partial:
        return (
            f"Partial scan ({len(scan_result.failed_courses)} course(s) failed: "
            f"{', '.join(scan_result.failed_courses)})"
        )

    new_c, new_m, _new_r = scanned_totals
    if new_c == 0:
        return "Scan returned 0 courses."

    if reset_state:
        return None

    if old_state is not None:
        if rollover is not None:
            return None
        old_c, old_m, old_r = baseline_totals
        if old_m > 0 and new_m == 0:
            return f"Total marks dropped from {old_m} to 0 (degenerate scan)."
        if old_c >= 3 and new_c < old_c * 0.5:
            return f"Course count dropped sharply from {old_c} to {new_c}."
        if old_m >= 5 and new_m < old_m * 0.5:
            return f"Total marks dropped sharply from {old_m} to {new_m}."
        if old_r >= 5 and _new_r < old_r * 0.5:
            return f"Total resources dropped sharply from {old_r} to {_new_r}."

    return None


def validate_snapshot(
    old_state: Optional[Dict[str, Any]],
    scan_result: ScanResult,
    *,
    reset_state: bool = False,
) -> Optional[str]:
    """Return the reason a snapshot must be rejected, or ``None`` when it is safe.

    Standalone entry point for callers that only have a scan in hand; it computes the
    totals and rollover itself. :func:`compute_transition` computes both once and calls
    the internal gate directly.

    This is the **only** place expiry is treated as a rejection reason. The orchestrator
    does not use this path for expiry: ``run_cycle`` raises ``SessionExpiredError`` before
    calling :func:`compute_transition`, so ``state.json`` is never touched. Keeping the
    rule here means a caller that only holds a scan (a diagnostic tool, a future
    non-raising driver) still refuses an expired snapshot instead of persisting it.
    """
    if scan_result.session_expired:
        return f"Session expired mid-scan: {scan_result.session_expired_error}"
    return _rejection_reason(
        old_state,
        scan_result,
        summarize_totals(old_state) if old_state is not None else (0, 0, 0),
        summarize_totals(scan_result.state),
        None if reset_state else detect_semester_rollover(old_state, scan_result.state),
        reset_state,
    )


def diff_states(
    old_state: Dict[str, Any],
    new_state: Dict[str, Any],
    base_url: str,
    track_announcements: bool = False,
) -> List[Notification]:
    """Compare old and new snapshots and return Notifications for added/removed items."""
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


# ------------------------------------------------------- notification builders


def _totals_dict(totals: Tuple[int, int, int]) -> Dict[str, int]:
    courses, marks, resources = totals
    return {"courses": courses, "marks": marks, "resources": resources}


def _truncate_preview(preview: str, limit: int = PREVIEW_LIMIT) -> str:
    if len(preview) > limit:
        return preview[: limit - 3].rstrip() + "..."
    return preview


def _anomaly_notification(
    reason: str,
    occurrence: int,
    totals: Tuple[int, int, int],
    baseline_totals: Tuple[int, int, int],
    scan: ScanResult,
    preview: str,
    options: TransitionOptions,
) -> Notification:
    new_c, new_m, new_r = totals
    old_c, old_m, old_r = baseline_totals
    msg_lines = [
        reason,
        (
            f"Scanned: {new_c} courses, {new_m} marks, {new_r} resources "
            f"(baseline: {old_c} courses, {old_m} marks, {old_r} resources)."
        ),
    ]
    if scan.set_cookie_names:
        msg_lines.append(
            f"Set-Cookie names observed: {', '.join(scan.set_cookie_names)}"
        )
    if preview:
        msg_lines.append(f"Page preview: {preview}")
    msg_lines.append(
        "Existing baseline was NOT overwritten. "
        "Further identical alerts are suppressed until recovery."
    )
    return Notification(
        event="scan_anomaly",
        title="Portal scan anomaly (state preserved)",
        message="\n".join(msg_lines),
        priority="high",
        tags=("warning",),
        url=f"{options.base_url}/Courses",
        data={
            "reason": reason,
            "occurrence": occurrence,
            "failed_courses": scan.failed_courses,
            "scanned": _totals_dict(totals),
            "baseline": _totals_dict(baseline_totals),
            "set_cookie_names": list(scan.set_cookie_names),
        },
    )


def _recovery_notification(
    prior_anomaly: Dict[str, Any],
    totals: Tuple[int, int, int],
    options: TransitionOptions,
) -> Notification:
    prev_reason = str(prior_anomaly.get("reason") or "previous scan anomaly")
    prev_count = int(prior_anomaly.get("count") or 1)
    c_count, m_count, r_count = totals
    return Notification(
        event="scan_recovered",
        title="Portal scan recovered",
        message=(
            f"Normal portal data returned after {prev_count} rejected scan(s) "
            f"({prev_reason}).\n"
            f"Current totals: {c_count} courses, {m_count} marks, {r_count} resources."
        ),
        priority="default",
        tags=("white_check_mark",),
        url=f"{options.base_url}/Courses",
        data={
            "previous_reason": prev_reason,
            "rejected_scans": prev_count,
            "courses": c_count,
            "marks": m_count,
            "resources": r_count,
        },
    )


def _baseline_notification(
    state: Dict[str, Any], totals: Tuple[int, int, int], options: TransitionOptions
) -> Notification:
    c_count, m_count, r_count = totals
    new_sem = str(state.get("semester") or "").strip()
    baseline_data: Dict[str, Any] = _totals_dict(totals)
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
    return Notification(
        event="baseline_saved",
        title="COMSATS Portal Tracker started",
        message=baseline_msg,
        priority="low",
        tags=("white_check_mark",),
        url=f"{options.base_url}/Courses",
        data=baseline_data,
    )


def _rollover_notification(
    state: Dict[str, Any],
    rollover: Tuple[str, str],
    totals: Tuple[int, int, int],
    options: TransitionOptions,
) -> Notification:
    c_count, m_count, r_count = totals
    old_sem_label, new_sem_label = rollover
    course_lines = "\n".join(
        f"- {c.get('label') or cid}" for cid, c in state.get("courses", {}).items()
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
    return Notification(
        event="semester_rollover",
        title=rollover_title,
        message=rollover_msg,
        priority="default",
        tags=("mortar_board", "white_check_mark"),
        url=f"{options.base_url}/Courses",
        data={
            "previous_semester": old_sem_label,
            "semester": new_sem_label,
            "courses": c_count,
            "marks": m_count,
            "resources": r_count,
        },
    )


# -------------------------------------------------------------- entry point


def compute_transition(
    previous_state: Optional[Dict[str, Any]],
    scan: ScanResult,
    prior_anomaly: Optional[Dict[str, Any]],
    now: datetime,
    options: TransitionOptions,
) -> TransitionOutcome:
    """Decide what one completed scan means. Pure: no I/O, no mutation of inputs.

    Rollover is evaluated at most once and totals once per snapshot. Rejection rules,
    dedupe/recovery behaviour, and every notification field are unchanged from the
    pre-extraction tracker.

    Precondition: ``scan.session_expired`` is false. Expiry has exactly one owner —
    ``tracker.run_cycle`` raises ``SessionExpiredError`` before calling this function, so
    ``state.json`` is never touched on expiry. An expired ``ScanResult`` here is a caller
    bug, not a policy question, so it fails loudly instead of being silently persisted
    (or turned into a second, unreachable anomaly branch).
    """
    if scan.session_expired:
        raise ValueError(
            "compute_transition() received an expired scan; the orchestrator must raise "
            "SessionExpiredError before computing a transition."
        )
    now_iso = now.isoformat()
    scanned_totals = summarize_totals(scan.state)
    baseline_totals = (
        summarize_totals(previous_state) if previous_state is not None else (0, 0, 0)
    )
    rollover = (
        None
        if options.reset_state
        else detect_semester_rollover(previous_state, scan.state)
    )

    # -- rejected snapshot: keep the baseline, maintain the anomaly marker ---------
    reason = _rejection_reason(
        previous_state,
        scan,
        baseline_totals,
        scanned_totals,
        rollover,
        options.reset_state,
    )
    if reason is not None:
        anomaly_key = reason.strip()
        if prior_anomaly is not None and str(prior_anomaly.get("key") or "") == anomaly_key:
            occurrence = int(prior_anomaly.get("count") or 1) + 1
            first_seen = str(prior_anomaly.get("first_seen") or now_iso)
            should_notify = False
        else:
            occurrence = 1
            first_seen = now_iso
            should_notify = options.notify_anomaly

        preview = _truncate_preview(scan.sample_marks_preview)
        marker = {
            "key": anomaly_key,
            "reason": reason,
            "first_seen": first_seen,
            "last_seen": now_iso,
            "count": occurrence,
            "scanned": _totals_dict(scanned_totals),
        }
        diagnostics = AnomalyDiagnostics(
            reason=reason,
            occurrence=occurrence,
            scanned_totals=_totals_dict(scanned_totals),
            baseline_totals=_totals_dict(baseline_totals),
            failed_courses=tuple(scan.failed_courses),
            set_cookie_names=tuple(scan.set_cookie_names),
            sample_marks_course=scan.sample_marks_course,
            marks_html=scan.sample_marks_html,
            marks_preview=scan.sample_marks_preview,
        )
        notifications = (
            [
                _anomaly_notification(
                    reason,
                    occurrence,
                    scanned_totals,
                    baseline_totals,
                    scan,
                    preview,
                    options,
                )
            ]
            if should_notify
            else []
        )
        return TransitionOutcome(
            kind=KIND_ANOMALY_REJECTED,
            persist_state=False,
            state=None,
            anomaly_action=AnomalyAction(
                kind=ANOMALY_RECORD, marker=marker, diagnostics=diagnostics
            ),
            notifications=notifications,
            baseline_totals=baseline_totals,
            scanned_totals=scanned_totals,
            summary={
                "kind": KIND_ANOMALY_REJECTED,
                "anomaly": reason,
                "status": "anomaly",
                "reason": reason,
                "occurrence": occurrence,
                "notified": should_notify,
                "duplicate_suppressed": not should_notify and options.notify_anomaly,
                "preview": preview,
                "preview_course": scan.sample_marks_course or "MarksSummary",
                "courses": scanned_totals[0],
                "marks": scanned_totals[1],
                "resources": scanned_totals[2],
                "message": None,
            },
        )

    # -- accepted snapshot: recovery note (if any) always comes first --------------
    recovery_note: Optional[Notification] = None
    if prior_anomaly is not None and options.notify_anomaly:
        recovery_note = _recovery_notification(prior_anomaly, scanned_totals, options)
    clear_anomaly = AnomalyAction(kind=ANOMALY_CLEAR if prior_anomaly is not None else ANOMALY_NONE)

    def _accepted(
        kind: str, state: Dict[str, Any], notifications: List[Notification]
    ) -> TransitionOutcome:
        notes = list(notifications)
        if recovery_note is not None:
            notes.insert(0, recovery_note)
        c_count, m_count, r_count = scanned_totals
        # The operator-facing "nothing happened" line is printed only when this cycle
        # dispatched nothing at all — a lone recovery alert counts as an event.
        message = None
        if not notes:
            message = (
                f"Scanned {c_count} courses ({m_count} marks, {r_count} resources) "
                "— no changes."
            )
        return TransitionOutcome(
            kind=kind,
            persist_state=True,
            state=state,
            anomaly_action=clear_anomaly,
            notifications=notes,
            baseline_totals=baseline_totals,
            scanned_totals=scanned_totals,
            summary={
                "kind": kind,
                "anomaly": None,
                "status": "ok",
                "courses": scanned_totals[0],
                "marks": scanned_totals[1],
                "resources": scanned_totals[2],
                "changed": bool(notifications),
                "recovered": recovery_note is not None,
                "message": message,
            },
        )

    # -- first baseline (or an explicit reset) -------------------------------------
    if previous_state is None or options.reset_state:
        state = scan.state
        return _accepted(
            KIND_BASELINE_SAVED,
            state,
            [_baseline_notification(state, scanned_totals, options)],
        )

    # -- new semester --------------------------------------------------------------
    if rollover is not None:
        return _accepted(
            KIND_SEMESTER_ROLLOVER,
            scan.state,
            [_rollover_notification(scan.state, rollover, scanned_totals, options)],
        )

    # -- same semester: carry the known label forward, then diff --------------------
    state = dict(scan.state)
    if not state.get("semester") and previous_state.get("semester"):
        state["semester"] = str(previous_state["semester"]).strip()
    notifications = diff_states(
        previous_state,
        state,
        base_url=options.base_url,
        track_announcements=options.track_announcements,
    )
    return _accepted(KIND_DIFF_APPLIED, state, notifications)
