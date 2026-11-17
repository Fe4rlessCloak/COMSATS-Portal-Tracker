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
import json
import os
import signal
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from notifiers import BaseNotifier, Notification, build_notifiers, dispatch_all
from sis_client import (
    AnnouncementItem,
    Course,
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    MarkItem,
    PortalHttpError,
    ResourceItem,
    SessionExpiredError,
    SisClient,
    parse_announcements,
)


@dataclass
class TrackerConfig:
    raw: Dict[str, str]
    session_id: str
    base_url: str
    user_agent: str
    keepalive_interval: int
    fetch_interval: int
    request_delay: float
    track_announcements: bool


def load_env_dict(env_path: Path) -> Dict[str, str]:
    """Load key=value settings from .env merged with process environment."""
    values: Dict[str, str] = {}
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")

    # Process environment overrides .env
    for key in (
        "SIS_SESSION_ID",
        "SIS_BASE_URL",
        "SIS_USER_AGENT",
        "KEEPALIVE_INTERVAL",
        "FETCH_INTERVAL",
        "REQUEST_DELAY",
        "TRACK_ANNOUNCEMENTS",
        "NTFY_URL",
        "NTFY_TOKEN",
        "WEBHOOK_URL",
        "DISCORD_WEBHOOK_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        if key in os.environ and os.environ[key] != "":
            values[key] = os.environ[key].strip()
    return values


def _parse_int(values: Dict[str, str], key: str, default: int, minimum: int = 5) -> int:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _parse_float(values: Dict[str, str], key: str, default: float) -> float:
    raw = values.get(key, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def load_config(env_path: Path, require_session: bool = True) -> TrackerConfig:
    values = load_env_dict(env_path)
    session_id = values.get("SIS_SESSION_ID", "").strip()
    if session_id.startswith("ASP.NET_SessionId="):
        session_id = session_id.split("=", 1)[1].strip()
    if session_id == "paste_your_ASP_NET_SessionId_here":
        session_id = ""

    if require_session and not session_id:
        raise SystemExit(
            "Missing SIS_SESSION_ID.\n"
            f"Copy {env_path.parent / '.env.example'} to {env_path} and set SIS_SESSION_ID."
        )

    return TrackerConfig(
        raw=values,
        session_id=session_id,
        base_url=(values.get("SIS_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
        user_agent=(values.get("SIS_USER_AGENT") or DEFAULT_USER_AGENT).strip(),
        keepalive_interval=_parse_int(values, "KEEPALIVE_INTERVAL", 120, minimum=15),
        fetch_interval=_parse_int(values, "FETCH_INTERVAL", 480, minimum=30),
        request_delay=_parse_float(values, "REQUEST_DELAY", 0.6),
        track_announcements=values.get("TRACK_ANNOUNCEMENTS", "").strip().lower()
        in ("1", "true", "yes", "on"),
    )


def load_state(state_path: Path) -> Optional[Dict[str, Any]]:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, state_path)


def scan_all_courses(
    client: SisClient,
    courses: List[Course],
    track_announcements: bool = False,
) -> Dict[str, Any]:
    """Iterate through all courses and capture current marks, resources, and optional announcements."""
    snapshot: Dict[str, Any] = {"courses": {}}
    for course in courses:
        ann_html = client.select_course(course.course_id)
        marks = client.get_marks_summary()
        resources = client.get_learning_resources()

        course_entry: Dict[str, Any] = {
            "code": course.code,
            "name": course.name,
            "label": course.label,
            "marks": {m.key: m.to_dict() for m in marks},
            "resources": {r.key: r.to_dict() for r in resources},
        }
        if track_announcements:
            anns = parse_announcements(ann_html)
            course_entry["announcements"] = {a.key: a.to_dict() for a in anns}

        snapshot["courses"][course.course_id] = course_entry
    return snapshot


def diff_states(
    old_state: Dict[str, Any],
    new_state: Dict[str, Any],
    base_url: str,
    track_announcements: bool = False,
) -> List[Notification]:
    """Compare old and new snapshots and return Notifications for any new/updated items."""
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
) -> None:
    """Run one full multi-course fetch + diff + notification cycle."""
    new_state = scan_all_courses(client, courses, track_announcements=track_announcements)
    old_state = load_state(state_path)

    if old_state is None:
        c_count, m_count, r_count = summarize_totals(new_state)
        if not dry_run:
            save_state(state_path, new_state)
        dispatch_all(
            notifiers,
            Notification(
                event="baseline_saved",
                title="COMSATS Portal Tracker started",
                message=(
                    f"Baseline saved for {c_count} courses "
                    f"({m_count} marks, {r_count} resources)."
                ),
                priority="low",
                tags=("white_check_mark",),
                url=f"{base_url}/Courses",
                data={"courses": c_count, "marks": m_count, "resources": r_count},
            ),
        )
        return

    notifications = diff_states(
        old_state,
        new_state,
        base_url=base_url,
        track_announcements=track_announcements,
    )
    if not dry_run:
        save_state(state_path, new_state)

    if notifications:
        for note in notifications:
            dispatch_all(notifiers, note)
    else:
        now = datetime.now().strftime("%H:%M:%S")
        c_count, m_count, r_count = summarize_totals(new_state)
        print(
            f"[{now}] Scanned {c_count} courses ({m_count} marks, {r_count} resources) — no changes."
        )


def run_daemon(env_path: Path, state_path: Path, dry_run: bool = False) -> int:
    stop = False

    def _handle_sig(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    cfg = load_config(env_path, require_session=True)
    notifiers = build_notifiers(cfg.raw)
    client = SisClient(
        session_id=cfg.session_id,
        base_url=cfg.base_url,
        user_agent=cfg.user_agent,
        request_delay=cfg.request_delay,
    )

    last_fetch = 0.0
    expired_alert_sent = False
    active_session_id = cfg.session_id

    print(
        f"Starting tracker (keep-alive={cfg.keepalive_interval}s, "
        f"fetch={cfg.fetch_interval}s, notifiers={[n.name for n in notifiers]})"
    )

    while not stop:
        # Hot-reload .env so a mini-PC user can refresh SIS_SESSION_ID without restarting
        try:
            fresh_cfg = load_config(env_path, require_session=False)
            if fresh_cfg.session_id and fresh_cfg.session_id != active_session_id:
                active_session_id = fresh_cfg.session_id
                client.session_id = active_session_id
                client.user_agent = fresh_cfg.user_agent
                expired_alert_sent = False
                last_fetch = 0.0
                print("[INFO] Detected updated SIS_SESSION_ID in .env — resuming.")
            cfg = fresh_cfg
            notifiers = build_notifiers(cfg.raw)
        except Exception as exc:
            print(f"[WARN] Could not reload .env: {exc}")

        if expired_alert_sent or not active_session_id:
            for _ in range(15):
                if stop:
                    break
                time.sleep(1)
            continue

        try:
            courses = client.get_courses()
            now_mono = time.monotonic()
            now_str = datetime.now().strftime("%H:%M:%S")

            if last_fetch == 0.0 or (now_mono - last_fetch) >= cfg.fetch_interval:
                run_cycle(
                    client,
                    courses,
                    state_path=state_path,
                    base_url=cfg.base_url,
                    track_announcements=cfg.track_announcements,
                    notifiers=notifiers,
                    dry_run=dry_run,
                )
                last_fetch = time.monotonic()
            else:
                print(f"[{now_str}] Keep-alive OK ({len(courses)} courses).")

        except SessionExpiredError as exc:
            if not expired_alert_sent:
                expired_alert_sent = True
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
                )
        except PortalHttpError as exc:
            print(f"[WARN] Portal error (will retry next tick): {exc}")

        for _ in range(cfg.keepalive_interval):
            if stop:
                break
            time.sleep(1)

    print("Tracker stopped.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="COMSATS CUOnline Portal Tracker")
    ap.add_argument("--once", action="store_true", help="Run a single full scan and exit")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan without writing state.json",
    )
    ap.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a test notification to all configured backends and exit",
    )
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    env_path = Path(os.environ.get("ENV_FILE") or (root / ".env"))
    state_path = Path(os.environ.get("STATE_FILE") or (root / "state.json"))

    if args.test_notify:
        cfg = load_config(env_path, require_session=False)
        notifiers = build_notifiers(cfg.raw)
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
        )
        return 0

    if args.once:
        cfg = load_config(env_path, require_session=True)
        notifiers = build_notifiers(cfg.raw)
        client = SisClient(
            session_id=cfg.session_id,
            base_url=cfg.base_url,
            user_agent=cfg.user_agent,
            request_delay=cfg.request_delay,
        )
        try:
            courses = client.get_courses()
            run_cycle(
                client,
                courses,
                state_path=state_path,
                base_url=cfg.base_url,
                track_announcements=cfg.track_announcements,
                notifiers=notifiers,
                dry_run=args.dry_run,
            )
            return 0
        except SessionExpiredError as exc:
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
            )
            return 1

    return run_daemon(env_path, state_path, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
