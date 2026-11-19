#!/usr/bin/env python3
"""Unit tests for COMSATS Portal Tracker parsers, diffing, and notifiers."""

from __future__ import annotations

import contextlib
import email.message
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
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
    PortalParseError,
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
    scrub_diagnostic_html,
    verify_active_course,
)
from tracker import (  # noqa: E402
    CycleTimeoutError,
    EnvWatcher,
    StateLockError,
    check_healthcheck,
    diff_states,
    load_config,
    load_env_with_sources,
    load_state,
    run_cycle,
    run_daemon,
    run_with_timeout,
    save_state,
    scan_all_courses,
    state_lock,
    validate_snapshot,
    write_heartbeat,
)

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

    def test_save_and_load_state_creates_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            payload = {"courses": {"1": {"marks": {}, "resources": {}}}}
            save_state(state_path, payload)
            self.assertEqual(load_state(state_path), payload)


class _RecordingNotifier(BaseNotifier):
    name = "recording"

    def __init__(self) -> None:
        self.notes: list[Notification] = []

    def send(self, note: Notification) -> None:
        self.notes.append(note)


class _FakeHttpResponse:
    def __init__(
        self,
        body: str,
        url: str = "https://sis.comsats.edu.pk/Courses",
        headers=None,
        status: int = 200,
    ) -> None:
        self._body = body.encode("utf-8")
        self._url = url
        self.headers = headers
        self.status = status

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


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
    def test_bug1_degenerate_scan_rejected_and_rolling_backups_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            good_baseline = _make_state(marks_count=6, res_count=6, course_count=1)
            save_state(state_path, good_baseline, max_backups=3)

            # Rotate backups across updates and verify .bak -> .bak.3 retention
            for i in range(4):
                updated = _make_state(marks_count=6 + i + 1, res_count=6, course_count=1)
                save_state(state_path, updated, max_backups=3)

            self.assertTrue(state_path.with_name("state.json.bak").exists())
            self.assertTrue(state_path.with_name("state.json.bak.2").exists())
            self.assertTrue(state_path.with_name("state.json.bak.3").exists())
            self.assertFalse(state_path.with_name("state.json.bak.4").exists())

            # Corrupt primary state.json -> load_state recovers from .bak
            latest_before_corrupt = load_state(state_path)
            bak_state = json.loads(state_path.with_name("state.json.bak").read_text(encoding="utf-8"))
            state_path.write_text("{corrupt json", encoding="utf-8")
            recovered = load_state(state_path)
            self.assertEqual(recovered, bak_state)

            # Restore latest good baseline and verify degenerate 0-marks scan does NOT overwrite
            save_state(state_path, latest_before_corrupt, max_backups=3)
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
            self.assertEqual(load_state(state_path), latest_before_corrupt)
            self.assertEqual([n.event for n in notifier.notes], ["scan_anomaly"])

            # Sharp >50% drop and 0-course scan are also rejected by validate_snapshot
            self.assertIsNotNone(
                validate_snapshot(
                    latest_before_corrupt,
                    tracker.ScanResult(state=_make_state(marks_count=2, res_count=6, course_count=1)),
                )
            )
            self.assertIsNotNone(
                validate_snapshot(
                    latest_before_corrupt,
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

            class _FakeClient:
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

        # 2. HTTP 302 redirect to /Login and 200 alternate login via SisClient.get_html
        client = SisClient("abc", request_delay=0.0)
        redirect_err = urllib.error.HTTPError(
            "https://sis.comsats.edu.pk/Courses",
            302,
            "Found",
            {"Location": "/Login/Index"},  # type: ignore[arg-type]
            None,
        )
        try:
            with mock.patch.object(client._opener, "open", side_effect=redirect_err):
                with self.assertRaises(SessionExpiredError):
                    client.get_html("/Courses")
        finally:
            redirect_err.close()

        with mock.patch.object(
            client._opener,
            "open",
            return_value=_FakeHttpResponse(
                '<html><body class="sis-login-page"><form action="/Login"><button>Go</button></form></body></html>'
            ),
        ):
            with self.assertRaises(SessionExpiredError):
                client.get_html("/Courses")

        # Missing page markup on authenticated shell raises PortalParseError
        with mock.patch.object(
            client._opener,
            "open",
            return_value=_FakeHttpResponse(_auth_shell("<div>Empty error shell</div>")),
        ):
            with self.assertRaises(PortalParseError):
                client.get_marks_summary()
            with self.assertRaises(PortalParseError):
                client.get_learning_resources()

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
            ok, msg = check_healthcheck(hb_path)
            self.assertFalse(ok)
            self.assertIn("wedged_exiting", msg)

        # Healthcheck checks: missing, fresh ok, stale, waiting_for_session
        with tempfile.TemporaryDirectory() as tmp:
            hb_path = Path(tmp) / "heartbeat.json"
            self.assertFalse(check_healthcheck(hb_path)[0])

            write_heartbeat(hb_path, "ok", keepalive_interval=60, fetch_interval=480)
            self.assertTrue(check_healthcheck(hb_path)[0])

            write_heartbeat(hb_path, "waiting_for_session", keepalive_interval=60, fetch_interval=480)
            self.assertFalse(check_healthcheck(hb_path)[0])

            stale = json.loads(hb_path.read_text(encoding="utf-8"))
            stale["status"] = "ok"
            stale["timestamp"] = time.time() - 500
            hb_path.write_text(json.dumps(stale), encoding="utf-8")
            ok_stale, msg_stale = check_healthcheck(hb_path, max_age_override=30)
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

        # Stuck on CSC336 while scanning CSC325 via SisClient raises CourseMismatchError
        client = SisClient("valid", request_delay=0.0)
        stuck_html = _auth_shell(
            '<div class="sis-lr"><span class="sis-lr-count">0 resources</span></div>',
            course_code="CSC336",
            course_name="Web Technologies",
        )
        with mock.patch.object(
            client._opener, "open", return_value=_FakeHttpResponse(stuck_html)
        ):
            with self.assertRaises(CourseMismatchError):
                client.select_course("182153", expected_course=expected_325)
            with self.assertRaises(CourseMismatchError):
                client.get_learning_resources(expected_course=expected_325)

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
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            baseline = _make_state(marks_count=2, res_count=1, course_count=1)
            save_state(state_path, baseline)

            call_order: list[str] = []
            orig_load = tracker.load_state

            def _spy_load(path: Path):
                call_order.append("load_state")
                return orig_load(path)

            def _spy_scan(*args, **kwargs):
                call_order.append("scan_all_courses")
                return tracker.ScanResult(state=_make_state(marks_count=3, res_count=1, course_count=1))

            with mock.patch.object(tracker, "load_state", side_effect=_spy_load), mock.patch.object(
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
            self.assertEqual(call_order, ["load_state", "scan_all_courses"])

            # Concurrent lock contention raises StateLockError
            import fcntl

            lock_path = state_path.with_name("state.json.lock")
            with lock_path.open("a+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    with self.assertRaises(StateLockError):
                        with state_lock(state_path, timeout=0.2):
                            pass
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

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

    def test_sis_client_cookie_jar_and_set_cookie_rotation(self) -> None:
        client = SisClient("init_sid", request_delay=0.0)
        seen_cookies: list[str] = []

        def _msg(*cookies: str) -> email.message.Message:
            m = email.message.Message()
            for c in cookies:
                m.add_header("Set-Cookie", c)
            return m

        auth_body = _auth_shell('<table class="sis-dash-table"></table>')

        # 1. Authenticated 200 setting ARRAffinity persists on next request
        def _open_step1(req, timeout=20):
            seen_cookies.append(req.headers.get("Cookie", ""))
            if len(seen_cookies) == 1:
                return _FakeHttpResponse(
                    auth_body,
                    headers=_msg("ARRAffinity=node1; Path=/; HttpOnly"),
                )
            return _FakeHttpResponse(auth_body)

        with mock.patch.object(client._opener, "open", side_effect=_open_step1):
            client.get_html("/Courses")
            client.get_html("/Courses")

        self.assertEqual(seen_cookies[0], "ASP.NET_SessionId=init_sid")
        self.assertIn("ASP.NET_SessionId=init_sid", seen_cookies[1])
        self.assertIn("ARRAffinity=node1", seen_cookies[1])
        self.assertEqual(client.session_id, "init_sid")

        # 2. Non-login 302 redirect sets cookie for hop 2 and commits after hop 2 passes login detection
        seen_cookies.clear()
        redir_headers = _msg("PortalAffinity=edge2; Path=/")
        redir_headers["Location"] = "/Courses/Index"
        redir_err = urllib.error.HTTPError(
            "https://sis.comsats.edu.pk/Courses",
            302,
            "Found",
            redir_headers,  # type: ignore[arg-type]
            None,
        )

        call_idx = 0

        def _open_step2(req, timeout=20):
            nonlocal call_idx
            call_idx += 1
            seen_cookies.append(req.headers.get("Cookie", ""))
            if call_idx == 1:
                raise redir_err
            return _FakeHttpResponse(auth_body)

        try:
            with mock.patch.object(client._opener, "open", side_effect=_open_step2):
                client.get_html("/Courses")
                client.get_html("/Courses")
        finally:
            redir_err.close()

        self.assertIn("PortalAffinity=edge2", seen_cookies[1])
        self.assertIn("PortalAffinity=edge2", seen_cookies[2])
        self.assertEqual(client.last_set_cookie_names, [])

        # 3. Authenticated 200 rotating ASP.NET_SessionId updates client.session_id and subsequent requests
        seen_cookies.clear()

        def _open_step3(req, timeout=20):
            seen_cookies.append(req.headers.get("Cookie", ""))
            if len(seen_cookies) == 1:
                return _FakeHttpResponse(
                    auth_body,
                    headers=_msg(
                        "ASP.NET_SessionId=rotated_sid; Path=/; HttpOnly",
                        "PortalAffinity=; Expires=Thu, 01-Jan-1970 00:00:00 GMT",
                    ),
                )
            return _FakeHttpResponse(auth_body)

        with mock.patch.object(client._opener, "open", side_effect=_open_step3):
            client.get_html("/Courses")
            client.get_html("/Courses")

        self.assertEqual(client.session_id, "rotated_sid")
        self.assertIn("ASP.NET_SessionId=rotated_sid", seen_cookies[1])
        self.assertIn("ARRAffinity=node1", seen_cookies[1])
        self.assertNotIn("PortalAffinity", seen_cookies[1])

        # 4. Login 302 or 200 login page with Set-Cookie must NOT mutate cookie jar or session_id
        login_redir_hdr = _msg("ASP.NET_SessionId=evil_redir; Path=/")
        login_redir_hdr["Location"] = "/Login/Index"
        login_redir_err = urllib.error.HTTPError(
            "https://sis.comsats.edu.pk/Courses",
            302,
            "Found",
            login_redir_hdr,  # type: ignore[arg-type]
            None,
        )
        try:
            with mock.patch.object(client._opener, "open", side_effect=login_redir_err):
                with self.assertRaises(SessionExpiredError):
                    client.get_html("/Courses")
        finally:
            login_redir_err.close()

        self.assertEqual(client.session_id, "rotated_sid")

        with mock.patch.object(
            client._opener,
            "open",
            return_value=_FakeHttpResponse(
                '<html><body class="sis-login-page"><form action="/Login"></form></body></html>',
                headers=_msg("ASP.NET_SessionId=evil_200; Path=/"),
            ),
        ):
            with self.assertRaises(SessionExpiredError):
                client.get_html("/Courses")

        self.assertEqual(client.session_id, "rotated_sid")

        # 5. Manual session_id assignment resets old auxiliary cookies
        client.session_id = "brand_new"
        seen_cookies.clear()
        with mock.patch.object(
            client._opener,
            "open",
            side_effect=lambda req, timeout=20: (
                seen_cookies.append(req.headers.get("Cookie", "")),
                _FakeHttpResponse(auth_body),
            )[1],
        ):
            client.get_html("/Courses")
        self.assertEqual(seen_cookies[0], "ASP.NET_SessionId=brand_new")

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
            self.assertTrue(check_healthcheck(hb_path)[0])

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

            class _SameCookieClient:
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


if __name__ == "__main__":
    unittest.main()
