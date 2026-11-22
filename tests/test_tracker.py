#!/usr/bin/env python3
"""Unit tests for COMSATS Portal Tracker parsers, diffing, and notifiers."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import probe  # noqa: E402
import tracker  # noqa: E402
from notifiers import BaseNotifier, Notification, _latin1_safe, dispatch_all  # noqa: E402
from sis_client import (  # noqa: E402
    Course,
    CourseMismatchError,
    PortalHttpError,
    SessionExpiredError,
    SisClient,
    detect_login_page,
    extract_diagnostic_text,
    has_authenticated_shell,
    parse_active_course,
    parse_announcements,
    parse_courses,
    parse_learning_resources,
    parse_marks_summary,
    parse_semester,
    scrub_diagnostic_html,
    verify_active_course,
)
from runtime_store import RuntimeStore, StateLockError  # noqa: E402
from tracker import (  # noqa: E402
    CycleTimeoutError,
    EnvWatcher,
    detect_semester_rollover,
    diff_states,
    load_config,
    load_env_with_sources,
    normalize_session_id,
    parse_env_file,
    parse_env_text,
    run_cycle,
    run_daemon,
    run_with_timeout,
    scan_all_courses,
    validate_snapshot,
)

def save_state(state_path: Path, state: dict, max_backups: int = 5) -> None:
    """Fixture helper: persist a baseline through the Runtime Store."""
    RuntimeStore(state_path).save_state(state, max_backups=max_backups)


def load_state(state_path: Path):
    """Fixture helper: read a baseline through the Runtime Store."""
    return RuntimeStore(state_path).load_state()


COURSES_HTML = """
<table class="table sis-dash-table">
  <thead>
    <tr><th>Course No</th><th>Course Name</th><th>Credits</th><th>Teacher</th><th>Class</th><th>Attendance</th></tr>
  </thead>
  <tbody>
    <tr onclick="window.location='/Courses/SetCourse/100001'">
      <td>CSC101</td>
      <td>Intro to Computing</td>
      <td>3</td>
      <td>Jane Doe</td>
      <td>BCS-1A</td>
      <td><div id="bar_100001"></div><script>$('#bar_100001').progressBarTimer();</script></td>
    </tr>
    <tr onclick="window.location='/Courses/SetCourse/100002'">
      <td>MTH104</td>
      <td>Calculus</td>
      <td>3</td>
      <td>John Smith</td>
      <td>BCS-1A</td>
      <td>90%</td>
    </tr>
  </tbody>
</table>
"""

MARKS_HTML = """
<div class="sis-marks-summary table-responsive quiz_listing">
  <div class="sis-marks-section-head">Quizzes</div>
  <table class="table table-striped sis-marks-table">
    <thead><tr><th>Title</th><th>Obtained Marks</th><th>Total Marks</th><th>Date Time</th></tr></thead>
    <tbody>
      <tr id="9001">
        <td>Quiz1</td>
        <td><span class="sis-marks-score">7.0</span></td>
        <td>10.0</td>
        <td>Saturday, 31 October 2026</td>
      </tr>
    </tbody>
  </table>
  <div class="sis-marks-section-head">Assignments</div>
  <table class="table table-striped sis-marks-table">
    <thead><tr><th>Title</th><th>Obtained Marks</th><th>Total Marks</th><th>Date Time</th></tr></thead>
    <tbody>
      <tr id="9002">
        <td>Assign1</td>
        <td><span class="sis-marks-score">8.5</span></td>
        <td>10.0</td>
        <td>Sunday, 1 November 2026</td>
      </tr>
    </tbody>
  </table>
</div>
"""

RESOURCES_HTML = """
<div class="sis-lr">
  <span class="sis-lr-count">1 resource</span>
  <div class="sis-lr-table-wrap">
    <table class="table sis-lr-table">
      <thead><tr><th>#</th><th>Title</th><th>Type</th><th>Date</th><th>Size</th><th>Action</th></tr></thead>
      <tbody>
        <tr class="sis-lr-row" id="res-55">
          <td class="sis-lr-num">1</td>
          <td class="sis-lr-title">Week 1 Slides</td>
          <td><span class="sis-lr-material">Lecture Notes</span></td>
          <td class="sis-lr-date">02 Nov 2026</td>
          <td class="sis-lr-size">1.2 MB</td>
          <td class="actions"><a class="sis-lr-btn" href="/LearningResources/Download/55">Download</a></td>
        </tr>
      </tbody>
    </table>
  </div>
</div>
"""

RESOURCES_EMPTY_HTML = """
<div class="sis-lr">
  <span class="sis-lr-count">0 resources</span>
  <p class="sis-empty-inline is-info"><strong>No learning resources yet</strong></p>
</div>
"""

ANNOUNCEMENTS_HTML = """
<div class="sis-ann announcements_box">
  <div class="sis-ann-list">
    <div class="sis-ann-item">
      <div class="sis-ann-item-head">
        <h2>Quiz 2 Postponed</h2>
        <ul class="sis-ann-meta"><li><strong>Date:</strong> 10 Nov 2026</li></ul>
      </div>
      <div class="sis-ann-body"><p>Moved to Friday.</p></div>
    </div>
  </div>
</div>
"""


class ParserAndDiffTests(unittest.TestCase):
    def test_parse_courses_ignores_inline_script_text(self) -> None:
        courses = parse_courses(COURSES_HTML)
        self.assertEqual(len(courses), 2)
        self.assertEqual(courses[0].course_id, "100001")
        self.assertEqual(courses[0].code, "CSC101")
        self.assertEqual(courses[0].name, "Intro to Computing")
        self.assertEqual(courses[1].course_id, "100002")

    def test_parse_marks_summary(self) -> None:
        marks = parse_marks_summary(MARKS_HTML)
        self.assertEqual(len(marks), 2)
        self.assertEqual(marks[0].section, "Quizzes")
        self.assertEqual(marks[0].title, "Quiz1")
        self.assertEqual(marks[0].obtained, "7.0")
        self.assertEqual(marks[0].total, "10.0")
        self.assertEqual(marks[0].key, "Quizzes:9001")
        self.assertEqual(marks[1].section, "Assignments")

    def test_parse_learning_resources_and_empty_state(self) -> None:
        items = parse_learning_resources(RESOURCES_HTML)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Week 1 Slides")
        self.assertEqual(items[0].material_type, "Lecture Notes")
        self.assertEqual(items[0].size, "1.2 MB")
        self.assertEqual(items[0].href, "/LearningResources/Download/55")

        self.assertEqual(parse_learning_resources(RESOURCES_EMPTY_HTML), [])

    def test_parse_announcements(self) -> None:
        anns = parse_announcements(ANNOUNCEMENTS_HTML)
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0].title, "Quiz 2 Postponed")
        self.assertIn("Moved to Friday.", anns[0].body)

    def test_diff_states_detects_new_and_updated_marks_and_resources(self) -> None:
        old_state = {
            "courses": {
                "100001": {
                    "code": "CSC101",
                    "name": "Intro to Computing",
                    "label": "CSC101 — Intro to Computing",
                    "marks": {
                        "Quizzes:9001": {
                            "key": "Quizzes:9001",
                            "section": "Quizzes",
                            "title": "Quiz1",
                            "obtained": "7.0",
                            "total": "10.0",
                            "date_text": "31 Oct 2026",
                        }
                    },
                    "resources": {},
                }
            }
        }
        new_state = {
            "courses": {
                "100001": {
                    "code": "CSC101",
                    "name": "Intro to Computing",
                    "label": "CSC101 — Intro to Computing",
                    "marks": {
                        "Quizzes:9001": {
                            "key": "Quizzes:9001",
                            "section": "Quizzes",
                            "title": "Quiz1",
                            "obtained": "8.0",
                            "total": "10.0",
                            "date_text": "31 Oct 2026",
                        },
                        "Assignments:9002": {
                            "key": "Assignments:9002",
                            "section": "Assignments",
                            "title": "Assign1",
                            "obtained": "9.0",
                            "total": "10.0",
                            "date_text": "01 Nov 2026",
                        },
                    },
                    "resources": {
                        "res-55": {
                            "key": "res-55",
                            "title": "Week 1 Slides",
                            "material_type": "Lecture Notes",
                            "date_text": "02 Nov 2026",
                            "size": "1.2 MB",
                            "href": "/LearningResources/Download/55",
                        }
                    },
                }
            }
        }
        notes = diff_states(old_state, new_state, base_url="https://sis.comsats.edu.pk")
        events = [n.event for n in notes]
        self.assertEqual(events, ["marks_updated", "resource_added"])
        self.assertIn("7.0 / 10.0 -> 8.0 / 10.0", notes[0].message)
        self.assertIn("+ [Assignments] Assign1: 9.0 / 10.0", notes[0].message)
        self.assertIn("Week 1 Slides", notes[1].message)
        self.assertEqual(
            notes[1].url, "https://sis.comsats.edu.pk/LearningResources/Download/55"
        )

    def test_latin1_safe_header_normalization(self) -> None:
        self.assertEqual(_latin1_safe("Marks update: CSC101 — Intro"), "Marks update: CSC101 - Intro")


class _RecordingNotifier(BaseNotifier):
    name = "recording"

    def __init__(self) -> None:
        self.notes: list[Notification] = []

    def send(self, note: Notification) -> None:
        self.notes.append(note)


class _ReconfigurableClient:
    """Explicit-reconfiguration shim shared by ad-hoc daemon test clients.

    ``run_daemon`` hot-reloads through ``client.reconfigure(...)`` instead of assigning
    fields, so fakes need the same method.
    """

    reconfig_calls: list

    def reconfigure(self, session_id=None, base_url=None, user_agent=None, request_delay=None, reset_cookies=False):
        calls = getattr(self, "reconfig_calls", None)
        if calls is None:
            calls = []
            self.reconfig_calls = calls
        calls.append(
            {
                "session_id": session_id,
                "base_url": base_url,
                "user_agent": user_agent,
                "request_delay": request_delay,
                "reset_cookies": reset_cookies,
            }
        )
        if session_id is not None:
            self.session_id = session_id
        if base_url is not None:
            self.base_url = base_url
        if user_agent is not None:
            self.user_agent = user_agent
        if request_delay is not None:
            self.request_delay = max(0.0, request_delay)


def _make_mark(key: str, title: str, obtained: str = "8.0") -> dict:
    return {
        "key": key,
        "section": "Quizzes",
        "title": title,
        "obtained": obtained,
        "total": "10.0",
        "date_text": "01 Nov 2026",
    }


def _make_resource(key: str, title: str) -> dict:
    return {
        "key": key,
        "title": title,
        "material_type": "Slides",
        "date_text": "01 Nov 2026",
        "size": "1.0 MB",
        "href": f"/LearningResources/Download/{key}",
    }


def _make_state(marks_count: int = 2, res_count: int = 2, course_count: int = 1) -> dict:
    courses = {}
    for c_idx in range(course_count):
        cid = str(100001 + c_idx)
        marks = {
            f"Quizzes:{c_idx}_{i}": _make_mark(f"Quizzes:{c_idx}_{i}", f"Quiz{i}")
            for i in range(marks_count)
        }
        resources = {
            f"res-{c_idx}_{i}": _make_resource(f"res-{c_idx}_{i}", f"Slide {i}")
            for i in range(res_count)
        }
        courses[cid] = {
            "code": f"CSC10{c_idx + 1}",
            "name": f"Course {c_idx + 1}",
            "label": f"CSC10{c_idx + 1} — Course {c_idx + 1}",
            "marks": marks,
            "resources": resources,
        }
    return {"courses": courses}


def _auth_shell(inner: str, course_code: str = "", course_name: str = "") -> str:
    course_hdr = ""
    if course_code or course_name:
        course_hdr = (
            f'<div class="page_title_container"><h3>{course_name}</h3>'
            f"<h5>Course Code : {course_code} &nbsp;&nbsp; Credit Hours : 3</h5></div>"
        )
    return (
        '<html><body class="sis-portal-body">'
        '<header class="sis-portal-header">'
        '<a id="Dash_Board" href="/Courses/Index">Dashboard</a>'
        '<a id="Sign_Out" href="/Login/Sign_out">Logout</a>'
        f"{course_hdr}</header>{inner}</body></html>"
    )


class ReliabilityAndBugfixTests(unittest.TestCase):
    def test_bug1_degenerate_scan_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            good_baseline = _make_state(marks_count=6, res_count=6, course_count=1)
            save_state(state_path, good_baseline, max_backups=3)
            baseline = load_state(state_path)
            notifier = _RecordingNotifier()

            class _ZeroMarksClient:
                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    return []

                def get_learning_resources(self, expected_course=None):
                    return []

            courses = [Course(course_id="100001", code="CSC101", name="Course 1")]
            result = run_cycle(
                _ZeroMarksClient(),  # type: ignore[arg-type]
                courses,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertFalse(result.saved)
            self.assertIsNotNone(result.anomaly)
            self.assertEqual(load_state(state_path), baseline)
            self.assertEqual([n.event for n in notifier.notes], ["scan_anomaly"])

            # Sharp >50% drop and 0-course scan are also rejected by validate_snapshot
            self.assertIsNotNone(
                validate_snapshot(
                    baseline,
                    tracker.ScanResult(state=_make_state(marks_count=2, res_count=6, course_count=1)),
                )
            )
            self.assertIsNotNone(
                validate_snapshot(
                    baseline,
                    tracker.ScanResult(state={"courses": {}}),
                )
            )

    def test_bug2_diff_detects_removals_and_legitimate_single_removal_persists(self) -> None:
        old_state = {
            "courses": {
                "100001": {
                    "code": "CSC101",
                    "name": "Intro",
                    "label": "CSC101 — Intro",
                    "marks": {
                        "Quizzes:1": _make_mark("Quizzes:1", "Quiz1", "7.0"),
                        "Quizzes:2": _make_mark("Quizzes:2", "Quiz2", "8.0"),
                    },
                    "resources": {
                        "res-1": _make_resource("res-1", "Week 1"),
                    },
                    "announcements": {
                        "Ann1|10 Nov": {"key": "Ann1|10 Nov", "title": "Ann1", "meta": "10 Nov", "body": "Hi"},
                    },
                },
                "100002": {
                    "code": "MTH104",
                    "name": "Calculus",
                    "label": "MTH104 — Calculus",
                    "marks": {"Quizzes:9": _make_mark("Quizzes:9", "Q9", "5.0")},
                    "resources": {},
                    "announcements": {},
                },
            }
        }
        new_state = {
            "courses": {
                "100001": {
                    "code": "CSC101",
                    "name": "Intro",
                    "label": "CSC101 — Intro",
                    "marks": {
                        "Quizzes:1": _make_mark("Quizzes:1", "Quiz1", "7.0"),
                    },
                    "resources": {},
                    "announcements": {},
                }
            }
        }
        notes = diff_states(
            old_state,
            new_state,
            base_url="https://sis.comsats.edu.pk",
            track_announcements=True,
        )
        events = [n.event for n in notes]
        self.assertEqual(
            events,
            ["marks_removed", "resource_removed", "announcement_removed", "course_removed"],
        )
        self.assertIn("Quiz2", notes[0].message)
        self.assertIn("Week 1", notes[1].message)
        self.assertIn("Ann1", notes[2].message)
        self.assertIn("MTH104 — Calculus", notes[3].message)

        # Legitimate removal of 1 mark (2 -> 1) passes validation and alerts marks_removed
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            baseline = _make_state(marks_count=2, res_count=1, course_count=1)
            save_state(state_path, baseline)
            notifier = _RecordingNotifier()

            class _OneRemovedClient:
                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    from sis_client import MarkItem

                    return [
                        MarkItem(
                            key="Quizzes:0_0",
                            section="Quizzes",
                            title="Quiz0",
                            obtained="8.0",
                            total="10.0",
                            date_text="01 Nov 2026",
                        )
                    ]

                def get_learning_resources(self, expected_course=None):
                    from sis_client import ResourceItem

                    return [
                        ResourceItem(
                            key="res-0_0",
                            title="Slide 0",
                            material_type="Slides",
                            date_text="01 Nov 2026",
                            size="1.0 MB",
                            href="/LearningResources/Download/res-0_0",
                        )
                    ]

            courses = [Course(course_id="100001", code="CSC101", name="Course 1")]
            res = run_cycle(
                _OneRemovedClient(),  # type: ignore[arg-type]
                courses,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertTrue(res.saved)
            self.assertIsNone(res.anomaly)
            self.assertEqual([n.event for n in notifier.notes], ["marks_removed"])

    def test_bug3_session_expiry_resumes_on_env_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_path.write_text(
                "SIS_SESSION_ID=expired_cookie\n"
                "KEEPALIVE_INTERVAL=15\n"
                "FETCH_INTERVAL=30\n"
                "EXPIRED_POLL_INTERVAL=1\n"
                "SESSION_EXPIRED_TIMEOUT=20\n",
                encoding="utf-8",
            )

            notifier = _RecordingNotifier()
            resumed_event = threading.Event()
            courses = [Course(course_id="100001", code="CSC101", name="Intro")]

            class _FakeClient(_ReconfigurableClient):
                def __init__(self, session_id, **kwargs) -> None:
                    self.session_id = session_id
                    self.base_url = kwargs.get("base_url", "https://sis.comsats.edu.pk")
                    self.user_agent = kwargs.get("user_agent", "")
                    self.request_delay = 0.0
                    self.cancel_event = None

                def get_courses(self):
                    if self.session_id == "expired_cookie":
                        raise SessionExpiredError("Expired")
                    resumed_event.set()
                    return courses

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    from sis_client import MarkItem

                    return [MarkItem("Quizzes:1", "Quizzes", "Q1", "9", "10", "")]

                def get_learning_resources(self, expected_course=None):
                    return []

            def _updater() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if hb_path.exists() and "waiting_for_session" in hb_path.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                env_path.write_text(
                    "SIS_SESSION_ID=fresh_cookie\n"
                    "KEEPALIVE_INTERVAL=15\n"
                    "FETCH_INTERVAL=30\n"
                    "EXPIRED_POLL_INTERVAL=1\n"
                    "SESSION_EXPIRED_TIMEOUT=20\n",
                    encoding="utf-8",
                )
                self.assertTrue(resumed_event.wait(timeout=5.0))
                time.sleep(0.2)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_updater, daemon=True)
            t.start()
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _FakeClient
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            events = [n.event for n in notifier.notes]
            self.assertIn("session_expired", events)
            self.assertIn("baseline_saved", events)

    def test_bug3_session_expiry_escalates_exit_1_and_dedups_restart_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_path.write_text(
                "SIS_SESSION_ID=still_expired\n"
                "KEEPALIVE_INTERVAL=15\n"
                "FETCH_INTERVAL=30\n"
                "EXPIRED_POLL_INTERVAL=1\n"
                "SESSION_EXPIRED_TIMEOUT=1\n",
                encoding="utf-8",
            )

            notifier = _RecordingNotifier()

            class _AlwaysExpiredClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event = None

                def get_courses(self):
                    raise SessionExpiredError("Expired")

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            buf = io.StringIO()
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _AlwaysExpiredClient
                ), mock.patch.object(
                    tracker, "build_notifiers", return_value=[notifier]
                ), contextlib.redirect_stdout(buf):
                    rc1 = run_daemon(env_path, state_path, heartbeat_path=hb_path)
                    rc2 = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)

            self.assertEqual(rc1, 1)
            self.assertEqual(rc2, 1)
            self.assertEqual([n.event for n in notifier.notes], ["session_expired"])
            output = buf.getvalue()
            self.assertIn("Waiting for updated SIS_SESSION_ID", output)
            self.assertIn("exiting with code 1 for restart", output)
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            self.assertEqual(hb["status"], "exiting_session_expired")

    def test_bug4_env_precedence_and_inode_replacement_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=disk_cookie_1\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"SIS_SESSION_ID": "stale_process_env"}, clear=False):
                values, sources = load_env_with_sources(env_path)
                self.assertEqual(values["SIS_SESSION_ID"], "disk_cookie_1")
                self.assertIn("overrides process_env", sources["SIS_SESSION_ID"])

                # Fallback to process_env when .env key is blank
                env_path.write_text("SIS_SESSION_ID=\n", encoding="utf-8")
                values2, sources2 = load_env_with_sources(env_path)
                self.assertEqual(values2["SIS_SESSION_ID"], "stale_process_env")
                self.assertEqual(sources2["SIS_SESSION_ID"], "process_env")

            # Watcher detects inode replacement even when value is unchanged, and content changes
            env_path.write_text("SIS_SESSION_ID=cookie_same\n", encoding="utf-8")
            watcher = EnvWatcher(env_path)
            self.assertEqual(watcher.check_changed(), (False, ""))

            tmp_env = Path(tmp) / ".env.tmp"
            tmp_env.write_text("SIS_SESSION_ID=cookie_same\n", encoding="utf-8")
            os.replace(tmp_env, env_path)
            changed1, desc1 = watcher.check_changed()
            self.assertTrue(changed1)
            self.assertIn("inode", desc1)
            self.assertIn("content unchanged", desc1)

            tmp_env2 = Path(tmp) / ".env.tmp2"
            tmp_env2.write_text("SIS_SESSION_ID=cookie_new\n", encoding="utf-8")
            os.replace(tmp_env2, env_path)
            changed2, desc2 = watcher.check_changed()
            self.assertTrue(changed2)
            self.assertIn("content changed", desc2)
            self.assertEqual(load_config(env_path).session_id, "cookie_new")

    def test_bug5_login_detection_and_real_snapshots(self) -> None:
        # 1. Explicit markers, alternative login markup, meta refresh, JS redirect, final URL, missing shell
        self.assertIsNotNone(detect_login_page('<form action="/Login"><input name="User"></form>'))
        self.assertIsNotNone(
            detect_login_page(
                '<html><head><meta http-equiv="refresh" content="0;url=/Login/Index"></head><body></body></html>'
            )
        )
        self.assertIsNotNone(
            detect_login_page('<script>window.location = "/Account/Login?ReturnUrl=/Courses";</script>')
        )
        self.assertIsNotNone(
            detect_login_page(_auth_shell("<div>ok</div>"), final_url="https://sis.comsats.edu.pk/Login/Index")
        )
        self.assertIsNotNone(detect_login_page("<html><body><div>500 Error</div></body></html>"))
        self.assertIsNone(detect_login_page(_auth_shell('<table class="sis-dash-table"></table>')))

        # 2. SisClient login detection through redirects/pages is covered in tests/test_portal.py
        #    (stub HTTP transport adapter); only the pure HTML/URL detectors live here.

        # Real HTML snapshots (if present locally) must pass authenticated shell & not false-positive
        snap_dir = ROOT / "snapshots"
        if snap_dir.exists():
            for html_file in sorted(snap_dir.glob("*.html")):
                html = html_file.read_text(encoding="utf-8", errors="replace")
                self.assertIsNone(detect_login_page(html), msg=f"False positive on {html_file.name}")
                self.assertTrue(has_authenticated_shell(html), msg=f"Missing shell on {html_file.name}")

    def test_bug6_bounded_notifiers_cycle_timeout_and_healthcheck(self) -> None:
        # Slow notifier times out cleanly without blocking fast notifier
        fast = _RecordingNotifier()

        class _HangingNotifier(BaseNotifier):
            name = "hanging"

            def send(self, note: Notification) -> None:
                time.sleep(2.0)

        t0 = time.monotonic()
        dispatch_all(
            [_HangingNotifier(), fast],
            Notification(event="test", title="Hi", message="Body"),
            timeout=0.15,
        )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.8)
        self.assertEqual(len(fast.notes), 1)

        # run_with_timeout flags wedged thread
        gate = threading.Event()
        try:
            with self.assertRaises(CycleTimeoutError) as ctx:
                run_with_timeout(lambda: gate.wait(5.0), timeout=0.1, grace=0.1)
            self.assertTrue(ctx.exception.wedged)
        finally:
            gate.set()

        # run_daemon exits 1 when cycle wedges
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "state.json"
            hb_path = Path(tmp) / "heartbeat.json"
            env_path.write_text("SIS_SESSION_ID=valid\n", encoding="utf-8")
            wedge_gate = threading.Event()

            class _WedgedClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event = None

                def get_courses(self):
                    wedge_gate.wait(10.0)
                    return []

            fast_cfg = load_config(env_path)
            fast_cfg.cycle_timeout = 1  # type: ignore[assignment]
            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _WedgedClient
                ), mock.patch.object(tracker, "load_config", return_value=fast_cfg):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                wedge_gate.set()
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)

            self.assertEqual(rc, 1)
            ok, msg = RuntimeStore(state_path, hb_path).check_healthcheck()
            self.assertFalse(ok)
            self.assertIn("wedged_exiting", msg)

        # Healthcheck checks: missing, fresh ok, stale, waiting_for_session
        with tempfile.TemporaryDirectory() as tmp:
            hb_path = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb_path, keepalive_interval=60)
            self.assertFalse(store.check_healthcheck()[0])

            store.write_heartbeat("ok")
            self.assertTrue(store.check_healthcheck()[0])

            store.write_heartbeat("waiting_for_session")
            self.assertFalse(store.check_healthcheck()[0])

            stale = json.loads(hb_path.read_text(encoding="utf-8"))
            stale["status"] = "ok"
            stale["timestamp"] = time.time() - 500
            hb_path.write_text(json.dumps(stale), encoding="utf-8")
            ok_stale, msg_stale = store.check_healthcheck(max_age_override=30)
            self.assertFalse(ok_stale)
            self.assertIn("stale", msg_stale)

    def test_bug7_per_course_isolation_and_mid_scan_session_expiry(self) -> None:
        c1 = Course(course_id="100001", code="CSC101", name="Course 1")
        c2 = Course(course_id="100002", code="MTH104", name="Course 2")
        prev_state = _make_state(marks_count=2, res_count=1, course_count=2)

        class _PartialFailClient:
            def select_course(self, cid, expected_course=None):
                if cid == "100002":
                    raise PortalHttpError("500 error")
                return ""

            def get_marks_summary(self, expected_course=None):
                from sis_client import MarkItem

                return [MarkItem("Quizzes:100", "Quizzes", "NewQuiz", "9", "10", "")]

            def get_learning_resources(self, expected_course=None):
                return []

        scan = scan_all_courses(
            _PartialFailClient(),  # type: ignore[arg-type]
            [c1, c2],
            previous_state=prev_state,
        )
        self.assertTrue(scan.partial)
        self.assertEqual(scan.failed_courses, [c2.label])
        self.assertIn("100001", scan.state["courses"])
        self.assertEqual(scan.state["courses"]["100002"], prev_state["courses"]["100002"])

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            save_state(state_path, prev_state)
            notifier = _RecordingNotifier()
            res = run_cycle(
                _PartialFailClient(),  # type: ignore[arg-type]
                [c1, c2],
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertFalse(res.saved)
            self.assertEqual(load_state(state_path), prev_state)
            self.assertEqual([n.event for n in notifier.notes], ["scan_anomaly"])

        # Mid-scan SessionExpiredError preserves earlier fetched course in ScanResult & keeps baseline
        class _MidExpireClient:
            def select_course(self, cid, expected_course=None):
                if cid == "100002":
                    raise SessionExpiredError("Expired mid-scan")
                return ""

            def get_marks_summary(self, expected_course=None):
                from sis_client import MarkItem

                return [MarkItem("Quizzes:100", "Quizzes", "NewQuiz", "9", "10", "")]

            def get_learning_resources(self, expected_course=None):
                return []

        scan_exp = scan_all_courses(
            _MidExpireClient(),  # type: ignore[arg-type]
            [c1, c2],
            previous_state=prev_state,
        )
        self.assertTrue(scan_exp.session_expired)
        self.assertIn("Quizzes:100", scan_exp.state["courses"]["100001"]["marks"])
        self.assertEqual(scan_exp.state["courses"]["100002"], prev_state["courses"]["100002"])

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            save_state(state_path, prev_state)
            with self.assertRaises(SessionExpiredError):
                run_cycle(
                    _MidExpireClient(),  # type: ignore[arg-type]
                    [c1, c2],
                    state_path=state_path,
                    base_url="https://sis.comsats.edu.pk",
                    track_announcements=False,
                    notifiers=[],
                )
            self.assertEqual(load_state(state_path), prev_state)

    def test_bug8_active_course_verification_prevents_attribution_drift(self) -> None:
        html_csc325 = _auth_shell(
            '<div class="sis-lr"><span class="sis-lr-count">1 resource</span></div>',
            course_code="CSC325",
            course_name="Computer Organization and Assembly Language",
        )
        info = parse_active_course(html_csc325)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.code, "CSC325")

        expected_325 = Course(course_id="182153", code="CSC325", name="COAL")
        expected_336 = Course(course_id="182154", code="CSC336", name="Web Technologies")
        verify_active_course(html_csc325, expected_325)
        with self.assertRaises(CourseMismatchError):
            verify_active_course(html_csc325, expected_336)

        # Stuck on CSC336 while scanning CSC325 is covered in tests/test_portal.py (stub transport).

    def test_bug9_once_handles_non_session_errors_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "state.json"
            env_path.write_text("SIS_SESSION_ID=valid\n", encoding="utf-8")

            class _ErrClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event = None

                def get_courses(self):
                    raise PortalHttpError("502 Bad Gateway")

            buf = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {"ENV_FILE": str(env_path), "STATE_FILE": str(state_path), "SIS_SESSION_ID": ""},
                clear=False,
            ), mock.patch.object(tracker, "SisClient", _ErrClient), mock.patch.object(
                sys, "argv", ["tracker.py", "--once"]
            ), contextlib.redirect_stdout(buf):
                rc = tracker.main()

            self.assertEqual(rc, 1)
            self.assertIn("[ERROR] Scan failed: 502 Bad Gateway", buf.getvalue())

    def test_bug10_load_before_scan_and_state_locking(self) -> None:
        """The baseline is read under the lock before the portal is scanned."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            baseline = _make_state(marks_count=2, res_count=1, course_count=1)
            save_state(state_path, baseline)

            call_order: list[str] = []
            orig_load = RuntimeStore.load_state

            def _spy_load(store_self):
                call_order.append("load_state")
                return orig_load(store_self)

            def _spy_scan(*args, **kwargs):
                call_order.append("scan_all_courses")
                return tracker.ScanResult(state=_make_state(marks_count=3, res_count=1, course_count=1))

            with mock.patch.object(
                RuntimeStore, "load_state", _spy_load
            ), mock.patch.object(
                tracker, "scan_all_courses", side_effect=_spy_scan
            ):
                res = run_cycle(
                    client=None,  # type: ignore[arg-type]
                    courses=[Course(course_id="100001", code="CSC101", name="Course 1")],
                    state_path=state_path,
                    base_url="https://sis.comsats.edu.pk",
                    track_announcements=False,
                    notifiers=[],
                )

            self.assertTrue(res.saved)
            # The baseline is read before the portal is scanned, inside the lock.
            self.assertEqual(call_order, ["load_state", "scan_all_courses"])

    def test_anomaly_dedup_recovery_and_diagnostics(self) -> None:
        from sis_client import MarkItem, ResourceItem

        marks_empty_html = (
            '<html><body class="sis-portal-body">'
            '<header class="sis-portal-header">'
            '<a id="Dash_Board" href="/Courses/Index">Dashboard</a>'
            '<form id="synopsisForm" method="post" action="https://ww5.comsats.edu.pk/efs/StudentLogin.aspx">'
            '<input name="uid" hidden type="text" value="CIIT/TEST-001/ISB">'
            '<input name="pwd" hidden type="password" value="SUPER_SECRET_PWD">'
            "</form></header>"
            '<div class="main_body"><div class="content_area">'
            '<div class="sis-marks-summary"><p class="sis-marks-empty">No marks available.</p>'
            '<input type="password" value="INNER_SECRET">'
            "</div></div></div>"
            '<footer class="sis-portal-footer"><p>Footer</p></footer>'
            "</body></html>"
        )

        # Verify scrubbing directly and against local real snapshots if present
        scrubbed = scrub_diagnostic_html(marks_empty_html)
        self.assertNotIn("SUPER_SECRET_PWD", scrubbed)
        self.assertNotIn("synopsisForm", scrubbed)
        self.assertNotIn("INNER_SECRET", scrubbed)
        self.assertIn("<!-- password input redacted -->", scrubbed)
        self.assertIn("No marks available.", extract_diagnostic_text(marks_empty_html))

        snap_marks = ROOT / "snapshots" / "marks_summary.html"
        if snap_marks.exists():
            raw_snap = snap_marks.read_text(encoding="utf-8", errors="replace")
            scrubbed_snap = scrub_diagnostic_html(raw_snap)
            self.assertNotIn("synopsisForm", scrubbed_snap)
            self.assertNotIn('type=\'password\'', scrubbed_snap.lower())
            self.assertNotIn('type="password"', scrubbed_snap.lower())

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            baseline = _make_state(marks_count=6, res_count=2, course_count=1)
            save_state(state_path, baseline)
            courses = [Course(course_id="100001", code="CSC101", name="Course 1")]

            class _ModeClient:
                def __init__(self) -> None:
                    self.mode = "zero_marks"
                    self.last_marks_html = ""
                    self.last_set_cookie_names: list[str] = []

                def select_course(self, cid, expected_course=None):
                    self.last_set_cookie_names = ["ARRAffinity"]
                    return ""

                def get_marks_summary(self, expected_course=None):
                    self.last_set_cookie_names = []
                    if self.mode == "zero_marks":
                        self.last_marks_html = marks_empty_html
                        return []
                    if self.mode == "partial_error":
                        raise PortalHttpError("503 Unavailable")
                    return [
                        MarkItem(
                            key=f"Quizzes:0_{i}",
                            section="Quizzes",
                            title=f"Quiz{i}",
                            obtained="8.0",
                            total="10.0",
                            date_text="01 Nov 2026",
                        )
                        for i in range(7)
                    ]

                def get_learning_resources(self, expected_course=None):
                    self.last_set_cookie_names = []
                    return [
                        ResourceItem(
                            key=f"res-0_{i}",
                            title=f"Slide {i}",
                            material_type="Slides",
                            date_text="01 Nov 2026",
                            size="1.0 MB",
                            href=f"/LearningResources/Download/res-0_{i}",
                        )
                        for i in range(2)
                    ]

            client = _ModeClient()

            # Dry-run anomaly should not persist marker or diagnostics files
            dry_notifier = _RecordingNotifier()
            dry_res = run_cycle(
                client,  # type: ignore[arg-type]
                courses,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[dry_notifier],
                dry_run=True,
            )
            self.assertFalse(dry_res.saved)
            self.assertIsNotNone(dry_res.anomaly)
            self.assertFalse(state_path.with_name(".anomaly_alerted").exists())
            self.assertFalse(state_path.with_name("last_anomaly.json").exists())

            # 1st real zero-marks anomaly sends 1 alert and writes diagnostics
            notifier = _RecordingNotifier()
            res1 = run_cycle(
                client,  # type: ignore[arg-type]
                courses,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertFalse(res1.saved)
            self.assertEqual([n.event for n in res1.notifications], ["scan_anomaly"])
            self.assertEqual([n.event for n in notifier.notes], ["scan_anomaly"])
            self.assertIn("Scanned: 1 courses, 0 marks, 2 resources", notifier.notes[0].message)
            self.assertEqual(notifier.notes[0].data["set_cookie_names"], ["ARRAffinity"])  # type: ignore[index]

            diag = json.loads(state_path.with_name("last_anomaly.json").read_text(encoding="utf-8"))
            self.assertEqual(diag["occurrence"], 1)
            self.assertEqual(diag["scanned_totals"], {"courses": 1, "marks": 0, "resources": 2})
            self.assertEqual(diag["baseline_totals"], {"courses": 1, "marks": 6, "resources": 2})
            self.assertEqual(diag["set_cookie_names"], ["ARRAffinity"])
            self.assertIn("No marks available.", diag["sample_marks_preview"])
            self.assertNotIn("SUPER_SECRET_PWD", json.dumps(diag))

            diag_html = state_path.with_name("last_anomaly_marks.html").read_text(encoding="utf-8")
            self.assertNotIn("SUPER_SECRET_PWD", diag_html)
            self.assertNotIn("synopsisForm", diag_html)
            self.assertNotIn("INNER_SECRET", diag_html)
            self.assertIn("No marks available.", diag_html)

            # 2nd identical zero-marks anomaly is deduplicated (no notification)
            res2 = run_cycle(
                client,  # type: ignore[arg-type]
                courses,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertFalse(res2.saved)
            self.assertEqual(res2.notifications, [])
            self.assertEqual([n.event for n in notifier.notes], ["scan_anomaly"])
            marker2 = json.loads(state_path.with_name(".anomaly_alerted").read_text(encoding="utf-8"))
            self.assertEqual(marker2["count"], 2)

            # Different anomaly reason sends a new alert and resets count to 1
            client.mode = "partial_error"
            res3 = run_cycle(
                client,  # type: ignore[arg-type]
                courses,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertFalse(res3.saved)
            self.assertEqual([n.event for n in notifier.notes], ["scan_anomaly", "scan_anomaly"])
            marker3 = json.loads(state_path.with_name(".anomaly_alerted").read_text(encoding="utf-8"))
            self.assertEqual(marker3["count"], 1)

            # Recovery scan with 1 new mark clears marker, saves state, and sends both recovery + marks_updated
            client.mode = "recovered"
            res4 = run_cycle(
                client,  # type: ignore[arg-type]
                courses,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertTrue(res4.saved)
            self.assertIsNone(res4.anomaly)
            self.assertFalse(state_path.with_name(".anomaly_alerted").exists())
            self.assertEqual([n.event for n in res4.notifications], ["scan_recovered", "marks_updated"])
            self.assertEqual(
                [n.event for n in notifier.notes],
                ["scan_anomaly", "scan_anomaly", "scan_recovered", "marks_updated"],
            )
            saved_after = load_state(state_path)
            self.assertIsNotNone(saved_after)
            assert saved_after is not None
            self.assertEqual(len(saved_after["courses"]["100001"]["marks"]), 7)

    def test_daemon_heartbeat_keeps_anomaly_across_keepalive_and_same_cookie_env_wakeup(self) -> None:
        # Part A: Heartbeat keeps status="anomaly" across keep-alive ticks
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_path.write_text("SIS_SESSION_ID=valid_cookie\n", encoding="utf-8")
            save_state(state_path, _make_state(marks_count=6, res_count=1, course_count=1))

            courses = [Course(course_id="100001", code="CSC101", name="Course 1")]
            tick_event = threading.Event()
            get_courses_calls = 0

            class _AnomalousClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event = None
                    self.last_marks_html = ""
                    self.last_set_cookie_names: list[str] = []

                def get_courses(self):
                    nonlocal get_courses_calls
                    get_courses_calls += 1
                    if get_courses_calls >= 2:
                        tick_event.set()
                    return courses

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    return []

                def get_learning_resources(self, expected_course=None):
                    from sis_client import ResourceItem

                    return [
                        ResourceItem(
                            key="res-0_0",
                            title="Slide 0",
                            material_type="Slides",
                            date_text="01 Nov 2026",
                            size="1.0 MB",
                            href="/LearningResources/Download/res-0_0",
                        )
                    ]

            fast_cfg = tracker.TrackerConfig(
                raw={"SIS_SESSION_ID": "valid_cookie"},
                session_id="valid_cookie",
                base_url="https://sis.comsats.edu.pk",
                user_agent="test-agent",
                keepalive_interval=1,
                fetch_interval=60,
                request_delay=0.0,
                track_announcements=False,
                cycle_timeout=10,
                session_expired_timeout=30,
                expired_poll_interval=1,
                notifier_timeout=5.0,
                max_state_backups=3,
                healthcheck_max_age=30,
            )

            def _stopper() -> None:
                self.assertTrue(tick_event.wait(timeout=5.0))
                time.sleep(0.25)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_stopper, daemon=True)
            t.start()
            try:
                with mock.patch.object(tracker, "SisClient", _AnomalousClient), mock.patch.object(
                    tracker, "load_config", return_value=fast_cfg
                ), mock.patch.object(tracker, "build_notifiers", return_value=[_RecordingNotifier()]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            self.assertGreaterEqual(get_courses_calls, 2)
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            self.assertEqual(hb["status"], "anomaly")
            self.assertIn("keep-alive OK", hb["detail"])
            self.assertTrue(RuntimeStore(state_path, hb_path).check_healthcheck(max_age_override=300)[0])

        # Part B: Touching/replacing .env with the SAME SIS_SESSION_ID wakes up expired wait loop immediately
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_content = (
                "SIS_SESSION_ID=same_cookie\n"
                "KEEPALIVE_INTERVAL=15\n"
                "FETCH_INTERVAL=30\n"
                "EXPIRED_POLL_INTERVAL=1\n"
                "SESSION_EXPIRED_TIMEOUT=20\n"
            )
            env_path.write_text(env_content, encoding="utf-8")

            notifier = _RecordingNotifier()
            courses = [Course(course_id="100001", code="CSC101", name="Intro")]
            resumed_event = threading.Event()
            attempt_lock = threading.Lock()
            attempts = 0
            retested_while_expired = threading.Event()

            class _SameCookieClient(_ReconfigurableClient):
                def __init__(self, session_id, **kwargs) -> None:
                    self._session_id = ""
                    self.session_id = session_id
                    self.base_url = kwargs.get("base_url", "https://sis.comsats.edu.pk")
                    self.user_agent = kwargs.get("user_agent", "")
                    self.request_delay = 0.0
                    self.cancel_event = None
                    self.last_marks_html = ""
                    self.last_set_cookie_names: list[str] = []

                @property
                def session_id(self) -> str:
                    return self._session_id

                @session_id.setter
                def session_id(self, value: str) -> None:
                    self._session_id = value

                def get_courses(self):
                    nonlocal attempts
                    with attempt_lock:
                        attempts += 1
                        cur = attempts
                    if cur == 1:
                        raise SessionExpiredError("Expired #1")
                    if cur == 2:
                        retested_while_expired.set()
                        raise SessionExpiredError("Still expired #2")
                    resumed_event.set()
                    return courses

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    from sis_client import MarkItem

                    return [MarkItem("Quizzes:1", "Quizzes", "Q1", "9", "10", "")]

                def get_learning_resources(self, expected_course=None):
                    return []

            def _wait_for_waiting() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if hb_path.exists():
                        try:
                            data = json.loads(hb_path.read_text(encoding="utf-8"))
                            if data.get("status") == "waiting_for_session":
                                return
                        except (json.JSONDecodeError, OSError):
                            pass
                    time.sleep(0.05)
                self.fail("Daemon never entered waiting_for_session")

            def _same_cookie_updater() -> None:
                _wait_for_waiting()
                # Atomic inode replacement with identical SIS_SESSION_ID while still expired
                tmp1 = Path(tmp) / ".env.tmp1"
                tmp1.write_text(env_content, encoding="utf-8")
                os.replace(tmp1, env_path)
                self.assertTrue(retested_while_expired.wait(timeout=4.0))

                _wait_for_waiting()
                # Second replacement once server accepts the reused cookie
                tmp2 = Path(tmp) / ".env.tmp2"
                tmp2.write_text(env_content, encoding="utf-8")
                os.replace(tmp2, env_path)
                self.assertTrue(resumed_event.wait(timeout=4.0))
                time.sleep(0.2)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_same_cookie_updater, daemon=True)
            t.start()
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _SameCookieClient
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            self.assertEqual([n.event for n in notifier.notes], ["session_expired", "baseline_saved"])

    def test_probe_respects_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            env_path.write_text("SIS_SESSION_ID=probe_cookie_from_env_file\n", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"ENV_FILE": str(env_path), "SIS_SESSION_ID": ""},
                clear=False,
            ), mock.patch.object(sys, "argv", ["probe.py"]), mock.patch.object(
                probe, "run_probe", return_value=0
            ) as mock_run:
                rc = probe.main()

            self.assertEqual(rc, 0)
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[0][0], "probe_cookie_from_env_file")

    def test_env_parser_boms_and_encodings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False):
                # 1. UTF-8 BOM directly before SIS_SESSION_ID on line 1
                p1 = Path(tmp) / "utf8_bom.env"
                p1.write_bytes(b"\xef\xbb\xbfSIS_SESSION_ID=bom_sid_1\n")
                self.assertEqual(load_config(p1).session_id, "bom_sid_1")

                # 2. UTF-8 BOM directly before comment containing '=' on line 1
                p2 = Path(tmp) / "utf8_bom_comment.env"
                p2.write_bytes(b"\xef\xbb\xbf# comment with = sign\nSIS_SESSION_ID=bom_sid_2\n")
                self.assertEqual(load_config(p2).session_id, "bom_sid_2")

                # 3. UTF-16 LE & UTF-16 BE with BOM (and UTF-32 LE with BOM)
                p3 = Path(tmp) / "utf16le.env"
                p3.write_bytes(b"\xff\xfe" + "SIS_SESSION_ID=u16le_sid\n".encode("utf-16-le"))
                self.assertEqual(load_config(p3).session_id, "u16le_sid")

                p4 = Path(tmp) / "utf16be.env"
                p4.write_bytes(b"\xfe\xff" + "SIS_SESSION_ID=u16be_sid\n".encode("utf-16-be"))
                self.assertEqual(load_config(p4).session_id, "u16be_sid")

                p5 = Path(tmp) / "utf32le.env"
                p5.write_bytes(b"\xff\xfe\x00\x00" + "SIS_SESSION_ID=u32le_sid\n".encode("utf-32-le"))
                self.assertEqual(load_config(p5).session_id, "u32le_sid")

                # 4. Windows-1252 (cp1252) em-dash 0x97 in comment
                p6 = Path(tmp) / "cp1252.env"
                p6.write_bytes(b"# COMSATS \x97 Portal\nSIS_SESSION_ID=cp1252_sid\n")
                self.assertEqual(load_config(p6).session_id, "cp1252_sid")

    def test_env_parser_export_inline_comments_and_quotes(self) -> None:
        # export prefix variations and non-export keys starting with export/EXPORT
        parsed_export = parse_env_text(
            "export SIS_SESSION_ID=abc\n"
            "EXPORT_FLAG=1\n"
            "export_mode=2\n"
        )
        self.assertEqual(parsed_export["SIS_SESSION_ID"], "abc")
        self.assertEqual(parsed_export["EXPORT_FLAG"], "1")
        self.assertEqual(parsed_export["export_mode"], "2")
        self.assertEqual(parse_env_text('export   SIS_SESSION_ID="abc"\n')["SIS_SESSION_ID"], "abc")
        self.assertEqual(parse_env_text("export\tSIS_SESSION_ID='abc' # note\n")["SIS_SESSION_ID"], "abc")

        # Unquoted inline # comments vs unquoted '#' without preceding whitespace
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "SIS_SESSION_ID=abc123 # morning\n"
                "KEEPALIVE_INTERVAL=60 # 1 min\n"
                "TRACK_ANNOUNCEMENTS=true # on\n"
                "WEBHOOK_URL=https://example.com/hook#frag\n"
                "NTFY_TOKEN=tok#123 # real comment\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"SIS_SESSION_ID": "", "KEEPALIVE_INTERVAL": "", "TRACK_ANNOUNCEMENTS": ""},
                clear=False,
            ):
                cfg = load_config(env_path)
            self.assertEqual(cfg.session_id, "abc123")
            self.assertEqual(cfg.keepalive_interval, 60)
            self.assertTrue(cfg.track_announcements)
            self.assertEqual(cfg.raw["WEBHOOK_URL"], "https://example.com/hook#frag")
            self.assertEqual(cfg.raw["NTFY_TOKEN"], "tok#123")

        self.assertEqual(parse_env_text("SIS_SESSION_ID=# comment\n")["SIS_SESSION_ID"], "")
        self.assertEqual(parse_env_text("SIS_SESSION_ID= # comment\n")["SIS_SESSION_ID"], "")

        # Quoted values with trailing # comments, preserved #, empty quotes, nested quotes, escapes
        quoted_cases = [
            ('K="abc" # c', "abc"),
            ('K="abc"#c', "abc"),
            ("K='abc' # c", "abc"),
            ("K='abc'#c", "abc"),
            ('K="abc # 123" # c', "abc # 123"),
            ("K='abc # 123' # c", "abc # 123"),
            ('K="" # empty', ""),
            ("K='' # empty", ""),
            ("""K="a'b'" """, "a'b'"),
            ("""K='a"b"' """, 'a"b"'),
            (r'K="a\"b\\c"', 'a"b\\c'),
        ]
        for line, expected in quoted_cases:
            self.assertEqual(parse_env_text(line)["K"], expected, msg=f"Failed for {line!r}")

        # Duplicate keys: last assignment wins, including blanking an earlier .env key
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "SIS_SESSION_ID=first\nSIS_SESSION_ID=second\nNTFY_URL=https://ntfy.sh/t\nNTFY_URL=\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"SIS_SESSION_ID": "", "NTFY_URL": ""}, clear=False):
                vals, _ = load_env_with_sources(env_path)
            self.assertEqual(vals["SIS_SESSION_ID"], "second")
            self.assertEqual(vals["NTFY_URL"], "")

    def test_session_normalization_and_secret_free_diagnostics(self) -> None:
        self.assertEqual(normalize_session_id("ASP.NET_SessionId=abc"), "abc")
        self.assertEqual(normalize_session_id("asp.net_sessionid=abc; Path=/; HttpOnly"), "abc")
        self.assertEqual(normalize_session_id("Cookie: foo=1; ASP.NET_SessionId=abc; bar=2"), "abc")
        self.assertEqual(normalize_session_id("ASP.NET_SessionId="), "")
        placeholder_parsed = parse_env_text('SIS_SESSION_ID="paste_your_ASP_NET_SessionId_here" # placeholder')
        self.assertEqual(normalize_session_id(placeholder_parsed["SIS_SESSION_ID"]), "")

        with tempfile.TemporaryDirectory() as tmp:
            # Directory passed as ENV_FILE must not raise IsADirectoryError
            dir_path = Path(tmp) / "env_dir"
            dir_path.mkdir()
            with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False):
                self.assertEqual(parse_env_file(dir_path), {})
                with self.assertRaises(SystemExit) as dir_ctx:
                    load_config(dir_path, require_session=True)
            self.assertIn("directory", str(dir_ctx.exception))

            # Missing SIS_SESSION_ID with typo'd key surfaces key name without leaking any values
            typo_env = Path(tmp) / ".env"
            typo_env.write_text(
                "SIS_SESSIONID=super_secret_val\nNTFY_TOKEN=secret_token\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"SIS_SESSION_ID": "", "NTFY_TOKEN": ""}, clear=False):
                with self.assertRaises(SystemExit) as typo_ctx:
                    load_config(typo_env, require_session=True)
            err_msg = str(typo_ctx.exception)
            self.assertIn("SIS_SESSIONID", err_msg)
            self.assertIn("NTFY_TOKEN", err_msg)
            self.assertNotIn("super_secret_val", err_msg)
            self.assertNotIn("secret_token", err_msg)

    def test_daemon_and_probe_with_bom_export_and_inline_comment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_path.write_bytes(
                b"\xef\xbb\xbfexport SIS_SESSION_ID=\"expired_cookie\" # initial\n"
                b"KEEPALIVE_INTERVAL=15 # sec\n"
                b"FETCH_INTERVAL=30\n"
                b"EXPIRED_POLL_INTERVAL=1\n"
                b"SESSION_EXPIRED_TIMEOUT=20\n"
            )

            # Verify probe.load_env & probe.load_config handle BOM + export + quoted inline comment
            with mock.patch.dict(os.environ, {"SIS_SESSION_ID": "", "SIS_USER_AGENT": ""}, clear=False):
                self.assertEqual(probe.load_env(env_path)["SIS_SESSION_ID"], "expired_cookie")
                probe_sid, _ = probe.load_config(env_path)
                self.assertEqual(probe_sid, "expired_cookie")

            notifier = _RecordingNotifier()
            resumed_event = threading.Event()
            courses = [Course(course_id="100001", code="CSC101", name="Intro")]

            class _FakeClient(_ReconfigurableClient):
                def __init__(self, session_id, **kwargs) -> None:
                    self.session_id = session_id
                    self.base_url = kwargs.get("base_url", "https://sis.comsats.edu.pk")
                    self.user_agent = kwargs.get("user_agent", "")
                    self.request_delay = 0.0
                    self.cancel_event = None

                def get_courses(self):
                    if self.session_id == "expired_cookie":
                        raise SessionExpiredError("Expired")
                    if self.session_id == "fresh_cookie":
                        resumed_event.set()
                        return courses
                    raise AssertionError(f"Unexpected session_id: {self.session_id!r}")

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    from sis_client import MarkItem

                    return [MarkItem("Quizzes:1", "Quizzes", "Q1", "9", "10", "")]

                def get_learning_resources(self, expected_course=None):
                    return []

            def _updater() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if hb_path.exists() and "waiting_for_session" in hb_path.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                env_path.write_bytes(
                    b"\xef\xbb\xbfexport SIS_SESSION_ID=\"fresh_cookie\" # updated morning\n"
                    b"KEEPALIVE_INTERVAL=15 # sec\n"
                    b"FETCH_INTERVAL=30\n"
                    b"EXPIRED_POLL_INTERVAL=1\n"
                    b"SESSION_EXPIRED_TIMEOUT=20\n"
                )
                self.assertTrue(resumed_event.wait(timeout=5.0))
                time.sleep(0.2)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_updater, daemon=True)
            t.start()
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _FakeClient
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            events = [n.event for n in notifier.notes]
            self.assertIn("session_expired", events)
            self.assertIn("baseline_saved", events)

    def test_daemon_survives_value_error_from_portal(self) -> None:
        # Header sanitization, invalid base URL, and transport-exception translation are
        # covered in tests/test_portal.py through the stub transport adapter.
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_path.write_text("SIS_SESSION_ID=valid\nKEEPALIVE_INTERVAL=15\n", encoding="utf-8")
            errored_event = threading.Event()

            class _ValueErrorClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event = None

                def get_courses(self):
                    errored_event.set()
                    raise ValueError("unknown url type: bad")

            def _stopper() -> None:
                self.assertTrue(errored_event.wait(timeout=5.0))
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if hb_path.exists() and "portal_error" in hb_path.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_stopper, daemon=True)
            t.start()
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _ValueErrorClient
                ), mock.patch.object(tracker, "build_notifiers", return_value=[_RecordingNotifier()]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            self.assertEqual(hb["status"], "portal_error")
            self.assertIn("unknown url type", hb["detail"])

    def test_sigterm_cancels_inflight_scan_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_path.write_text(
                "SIS_SESSION_ID=valid\nKEEPALIVE_INTERVAL=15\nCYCLE_TIMEOUT=60\n",
                encoding="utf-8",
            )
            courses = [Course(course_id="100001", code="CSC101", name="Course 1")]
            in_scan = threading.Event()
            observed_cancel: list[threading.Event] = []

            class _SlowScanClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event: Optional[threading.Event] = kwargs.get("cancel_event")
                    self.last_semester = "Fall 2026"

                def get_courses(self):
                    return courses

                def select_course(self, cid, expected_course=None):
                    if self.cancel_event is not None:
                        observed_cancel.append(self.cancel_event)
                    in_scan.set()
                    if self.cancel_event is not None:
                        self.cancel_event.wait(10.0)
                        if self.cancel_event.is_set():
                            raise PortalHttpError("Request cancelled.")
                    return ""

                def get_marks_summary(self, expected_course=None):
                    return []

                def get_learning_resources(self, expected_course=None):
                    return []

            sig_t0: list[float] = []

            def _stopper() -> None:
                self.assertTrue(in_scan.wait(timeout=5.0))
                sig_t0.append(time.monotonic())
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_stopper, daemon=True)
            t.start()
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _SlowScanClient
                ), mock.patch.object(tracker, "build_notifiers", return_value=[_RecordingNotifier()]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            elapsed_after_sig = time.monotonic() - sig_t0[0]
            self.assertEqual(rc, 0)
            self.assertLess(elapsed_after_sig, 2.0)
            self.assertTrue(observed_cancel and observed_cancel[0].is_set())
            self.assertFalse(state_path.exists())

    def test_semester_tracking_rollover_retake_legacy_and_reset_state(self) -> None:
        from sis_client import MarkItem

        self.assertEqual(
            parse_semester("<div><p>Current session: Fall 2026  . Open a course.</p></div>"),
            "Fall 2026",
        )
        self.assertEqual(parse_semester("<div><span>Spring 2027</span></div>"), "Spring 2027")
        snap_courses = ROOT / "snapshots" / "courses.html"
        if snap_courses.exists():
            self.assertTrue(
                bool(parse_semester(snap_courses.read_text(encoding="utf-8", errors="replace")))
            )

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            fall_state = _make_state(marks_count=6, res_count=2, course_count=2)
            fall_state["semester"] = "Fall 2026"
            save_state(state_path, fall_state)

            class _RolloverClient:
                def __init__(self, semester: str, marks: list[MarkItem]) -> None:
                    self.last_semester = semester
                    self._marks = marks

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    return list(self._marks)

                def get_learning_resources(self, expected_course=None):
                    return []

            # 1. Same semester (Fall 2026 -> Fall 2026) with 6 -> 0 marks STILL blocked as anomaly
            notifier = _RecordingNotifier()
            c_same = [
                Course(course_id="100001", code="CSC101", name="Course 1"),
                Course(course_id="100002", code="CSC102", name="Course 2"),
            ]
            res_anom = run_cycle(
                _RolloverClient("Fall 2026", []),  # type: ignore[arg-type]
                c_same,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertFalse(res_anom.saved)
            self.assertIsNotNone(res_anom.anomaly)

            # 2. New semester (Fall 2026 -> Spring 2027) WITH retaken course 100001 and 0 marks rolls over
            notifier.notes.clear()
            c_spring = [
                Course(course_id="100001", code="CSC101", name="Course 1 (Retake)"),
                Course(course_id="200003", code="CSC203", name="Course 3"),
            ]
            res_roll = run_cycle(
                _RolloverClient("Spring 2027", []),  # type: ignore[arg-type]
                c_spring,
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[notifier],
            )
            self.assertTrue(res_roll.saved)
            self.assertIsNone(res_roll.anomaly)
            events = [n.event for n in notifier.notes]
            self.assertIn("semester_rollover", events)
            self.assertNotIn("course_removed", events)
            saved_spring = load_state(state_path)
            assert saved_spring is not None
            self.assertEqual(saved_spring.get("semester"), "Spring 2027")
            self.assertEqual(
                json.loads(state_path.with_name("state.json.bak").read_text(encoding="utf-8")).get("semester"),
                "Fall 2026",
            )

            # 3. Legacy state without "semester": same courses upgrade cleanly without rollover;
            #    disjoint courses roll over cleanly even with 0 marks
            legacy_state = _make_state(marks_count=6, res_count=1, course_count=1)
            self.assertNotIn("semester", legacy_state)
            save_state(state_path, legacy_state)
            legacy_same_marks = [
                MarkItem(f"Quizzes:0_{i}", "Quizzes", f"Quiz{i}", "8.0", "10.0", "01 Nov")
                for i in range(6)
            ]
            res_legacy_same = run_cycle(
                _RolloverClient("Fall 2026", legacy_same_marks),  # type: ignore[arg-type]
                [Course(course_id="100001", code="CSC101", name="Course 1")],
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[_RecordingNotifier()],
            )
            self.assertTrue(res_legacy_same.saved)
            self.assertEqual((load_state(state_path) or {}).get("semester"), "Fall 2026")
            self.assertIsNone(detect_semester_rollover(legacy_state, load_state(state_path) or {}))

            save_state(state_path, legacy_state)
            disjoint_notifier = _RecordingNotifier()
            res_legacy_disjoint = run_cycle(
                _RolloverClient("", []),  # type: ignore[arg-type]
                [Course(course_id="999001", code="CSC499", name="New Term Course")],
                state_path=state_path,
                base_url="https://sis.comsats.edu.pk",
                track_announcements=False,
                notifiers=[disjoint_notifier],
            )
            self.assertTrue(res_legacy_disjoint.saved)
            self.assertEqual([n.event for n in disjoint_notifier.notes], ["semester_rollover"])

            # 4. --once --reset-state overrides same-semester >50% drop, plus SIS_SEMESTER override;
            #    --reset-state without --once returns 1
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=valid\nSIS_SEMESTER=Summer 2027\n", encoding="utf-8")
            big_state = _make_state(marks_count=6, res_count=2, course_count=1)
            big_state["semester"] = "Summer 2027"
            save_state(state_path, big_state)

            class _ResetCliClient:
                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event = None
                    self.last_semester = ""

                def get_courses(self):
                    return [Course(course_id="100001", code="CSC101", name="Course 1")]

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    return []

                def get_learning_resources(self, expected_course=None):
                    return []

            reset_notifier = _RecordingNotifier()
            with mock.patch.dict(
                os.environ,
                {"ENV_FILE": str(env_path), "STATE_FILE": str(state_path), "SIS_SESSION_ID": ""},
                clear=False,
            ), mock.patch.object(tracker, "SisClient", _ResetCliClient), mock.patch.object(
                tracker, "build_notifiers", return_value=[reset_notifier]
            ):
                with mock.patch.object(sys, "argv", ["tracker.py", "--reset-state"]):
                    self.assertEqual(tracker.main(), 1)
                with mock.patch.object(sys, "argv", ["tracker.py", "--once", "--reset-state"]):
                    rc_reset = tracker.main()

            self.assertEqual(rc_reset, 0)
            self.assertEqual([n.event for n in reset_notifier.notes], ["baseline_saved"])
            reset_saved = load_state(state_path)
            assert reset_saved is not None
            self.assertEqual(reset_saved.get("semester"), "Summer 2027")
            self.assertEqual(len(reset_saved["courses"]["100001"]["marks"]), 0)

    def test_session_alerted_cleared_on_get_courses_even_if_scan_hits_anomaly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_content = (
                "SIS_SESSION_ID=reused_cookie\n"
                "KEEPALIVE_INTERVAL=1\n"
                "FETCH_INTERVAL=30\n"
                "EXPIRED_POLL_INTERVAL=1\n"
                "SESSION_EXPIRED_TIMEOUT=20\n"
            )
            env_path.write_text(env_content, encoding="utf-8")
            save_state(state_path, _make_state(marks_count=6, res_count=1, course_count=1))

            notifier = _RecordingNotifier()
            courses = [Course(course_id="100001", code="CSC101", name="Course 1")]
            attempts = 0
            attempt_lock = threading.Lock()
            second_expire_seen = threading.Event()

            class _FlakyReusedClient(_ReconfigurableClient):
                def __init__(self, session_id, **kwargs) -> None:
                    self._session_id = session_id
                    self.cancel_event = None
                    self.last_marks_html = ""
                    self.last_set_cookie_names: list[str] = []
                    self.last_semester = ""

                @property
                def session_id(self) -> str:
                    return self._session_id

                @session_id.setter
                def session_id(self, value: str) -> None:
                    self._session_id = value

                def get_courses(self):
                    nonlocal attempts
                    with attempt_lock:
                        attempts += 1
                        cur = attempts
                    if cur == 1:
                        raise SessionExpiredError("Expired #1")
                    if cur == 2:
                        return courses
                    second_expire_seen.set()
                    raise SessionExpiredError("Expired #2")

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    return []

                def get_learning_resources(self, expected_course=None):
                    return []

            def _driver() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if hb_path.exists() and "waiting_for_session" in hb_path.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                # Touch .env with same cookie -> attempt #2 passes get_courses(), hits anomaly
                env_path.write_text(env_content + "# refreshed\n", encoding="utf-8")
                self.assertTrue(second_expire_seen.wait(timeout=5.0))
                time.sleep(0.2)
                signal.raise_signal(signal.SIGTERM)

            orig_load_config = tracker.load_config

            def _fast_load_config(ep: Path, require_session: bool = True) -> tracker.TrackerConfig:
                cfg_obj = orig_load_config(ep, require_session=require_session)
                cfg_obj.keepalive_interval = 1
                return cfg_obj

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_driver, daemon=True)
            t.start()
            try:
                with mock.patch.dict(os.environ, {"SIS_SESSION_ID": ""}, clear=False), mock.patch.object(
                    tracker, "SisClient", _FlakyReusedClient
                ), mock.patch.object(
                    tracker, "load_config", side_effect=_fast_load_config
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            events = [n.event for n in notifier.notes]
            self.assertEqual(events.count("session_expired"), 2)
            self.assertIn("scan_anomaly", events)


if __name__ == "__main__":
    unittest.main()
