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
import urllib.parse
from pathlib import Path
from typing import Optional
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import probe  # noqa: E402
import tracker  # noqa: E402
from notifiers import BaseNotifier, Notification, _latin1_safe, dispatch_all  # noqa: E402
from sis_client import (  # noqa: E402
    Course,
    CourseMismatchError,
    MarkItem,
    PortalHttpError,
    ResourceItem,
    SessionExpiredError,
    SisClient,
    detect_login_page,
    has_authenticated_shell,
    parse_active_course,
    parse_announcements,
    parse_courses,
    parse_learning_resources,
    parse_marks_summary,
    parse_semester,
    verify_active_course,
)
from support_portal_pages import SimulatedPortal  # noqa: E402
from support_stub_transport import StubResponse, StubTransport, StubTransportError  # noqa: E402
from runtime_store import RuntimeStore  # noqa: E402
from tracker import (  # noqa: E402
    CycleOptions,
    CycleTimeoutError,
    load_config,
    run_cycle,
    run_daemon,
    run_with_timeout,
    scan_all_courses,
)

PORTAL_BASE = "https://sis.comsats.edu.pk"


def portal_client(portal, session_id: str = "valid_cookie", **kwargs) -> SisClient:
    """Real ``SisClient`` wired to an in-memory portal simulator."""
    kwargs.setdefault("request_delay", 0.0)
    return SisClient(session_id, base_url=PORTAL_BASE, transport=portal, **kwargs)


def portal_factory(portal, clients: Optional[list] = None):
    """Patch target for ``tracker.SisClient``: every constructed client is a real one.

    ``run_daemon`` builds its own client from the freshly parsed ``.env``, so daemon tests
    swap the class for this factory and keep the whole real client/transport stack.
    """

    def _factory(*args, **kwargs):
        session_id = kwargs.get("session_id")
        if session_id is None and args:
            session_id = args[0]
        client = SisClient(
            session_id or "valid_cookie",
            base_url=kwargs.get("base_url", PORTAL_BASE),
            user_agent=kwargs.get("user_agent", ""),
            request_delay=0.0,
            cancel_event=kwargs.get("cancel_event"),
            transport=portal,
        )
        if clients is not None:
            clients.append(client)
        return client

    return _factory

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

    def test_latin1_safe_header_normalization(self) -> None:
        self.assertEqual(_latin1_safe("Marks update: CSC101 — Intro"), "Marks update: CSC101 - Intro")


class _RecordingNotifier(BaseNotifier):
    name = "recording"

    def __init__(self) -> None:
        self.notes: list[Notification] = []

    def send(self, note: Notification) -> None:
        self.notes.append(note)


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


class CycleIntegrationTests(unittest.TestCase):
    """End-to-end `run_cycle` through every real layer.

    Wiring is deliberately complete: the real `SisClient` over `SimulatedPortal` (itself a
    `StubTransport`, so every outbound request is recorded at the HTTP edge), a real
    `RuntimeStore` over a temp directory, the real `snapshot_transition` module, and the
    recording notifier. Nothing here patches Portal behaviour, the policy, or the store.
    """

    BASE = "https://sis.comsats.edu.pk"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "data" / "state.json"
        self.data_dir = self.state_path.parent
        self.store = RuntimeStore(self.state_path)
        self.notifier = _RecordingNotifier()
        self.course = Course(course_id="100001", code="CSC101", name="Intro to Computing")

    # -- helpers -------------------------------------------------------------
    def portal(self, marks=(), resources=(), semester="Fall 2026", **kwargs):
        return SimulatedPortal(
            courses=[self.course],
            semester=semester,
            marks={self.course.course_id: list(marks)},
            resources={self.course.course_id: list(resources)},
            **kwargs,
        )

    def cycle(self, portal, courses=None, **kwargs):
        return run_cycle(
            portal_client(portal),
            courses if courses is not None else [self.course],
            self.store,
            CycleOptions(base_url=self.BASE, semester=kwargs.pop("semester", portal.semester), **kwargs),
            [self.notifier],
        )

    def events(self) -> list:
        return [n.event for n in self.notifier.notes]

    def saved(self):
        return self.store.load_state()

    # -- baseline ------------------------------------------------------------
    def test_first_cycle_saves_a_baseline(self) -> None:
        res = self.cycle(self.portal(marks=[MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", "")]))
        self.assertTrue(res.saved)
        self.assertIsNone(res.anomaly)
        self.assertEqual(self.events(), ["baseline_saved"])
        self.assertEqual(self.store.load_state()["semester"], "Fall 2026")
        self.assertEqual(self.store.load_state()["courses"]["100001"]["code"], "CSC101")
        self.assertFalse(self.data_dir.joinpath(".anomaly_alerted").exists())

    def test_dry_run_baseline_writes_nothing(self) -> None:
        res = self.cycle(
            self.portal(marks=[MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", "")]),
            dry_run=True,
        )
        self.assertFalse(res.saved)
        self.assertEqual(self.events(), ["baseline_saved"])
        self.assertFalse(self.state_path.exists())

    # -- unchanged / incremental --------------------------------------------
    def test_unchanged_cycle_reports_but_does_not_notify(self) -> None:
        marks = [MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", "")]
        self.cycle(self.portal(marks=marks))
        self.notifier.notes.clear()

        res = self.cycle(self.portal(marks=marks))
        self.assertTrue(res.saved)
        self.assertIsNone(res.anomaly)
        self.assertEqual(self.notifier.notes, [])
        self.assertEqual(len(self.saved()["courses"]["100001"]["marks"]), 1)

    def test_new_marks_are_announced_and_persisted(self) -> None:
        self.cycle(self.portal(marks=[MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", "")]))
        self.notifier.notes.clear()

        res = self.cycle(
            self.portal(
                marks=[
                    MarkItem("Quizzes:1", "Quizzes", "Quiz1", "9.0", "10.0", ""),
                    MarkItem("Quizzes:2", "Quizzes", "Quiz2", "5.0", "10.0", ""),
                ],
                resources=[
                    ResourceItem("res-1", "Slides", "Week 1", "01 Nov", "1.0 MB",
                                 "/LearningResources/Download/res-1")
                ],
            )
        )
        self.assertTrue(res.saved)
        self.assertEqual(self.events(), ["marks_updated", "resource_added"])
        self.assertEqual(self.notifier.notes[0].data["items"][0]["change"], "updated")
        self.assertEqual(self.notifier.notes[0].data["items"][1]["change"], "added")
        self.assertEqual(len(self.saved()["courses"]["100001"]["marks"]), 2)

    def test_legitimate_small_removal_persists_and_alerts(self) -> None:
        self.cycle(
            self.portal(
                marks=[
                    MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", ""),
                    MarkItem("Quizzes:2", "Quizzes", "Quiz2", "7.0", "10.0", ""),
                ],
                resources=[ResourceItem("res-1", "Slides", "Week 1", "01 Nov", "1.0 MB", "/x")],
            )
        )
        self.notifier.notes.clear()

        res = self.cycle(
            self.portal(
                marks=[MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", "")],
                resources=[ResourceItem("res-1", "Slides", "Week 1", "01 Nov", "1.0 MB", "/x")],
            )
        )
        self.assertTrue(res.saved)
        self.assertEqual(self.events(), ["marks_removed"])
        self.assertEqual(len(self.saved()["courses"]["100001"]["marks"]), 1)

    # -- rollover ------------------------------------------------------------
    def test_semester_rollover_by_label_saves_a_fresh_baseline(self) -> None:
        self.cycle(self.portal(marks=[MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", "")]))
        self.notifier.notes.clear()

        res = self.cycle(self.portal(marks=[], semester="Spring 2027"))
        self.assertTrue(res.saved)
        self.assertEqual(self.events(), ["semester_rollover"])
        self.assertEqual(self.saved()["semester"], "Spring 2027")
        self.assertEqual(
            json.loads(self.data_dir.joinpath("state.json.bak").read_text(encoding="utf-8"))["semester"],
            "Fall 2026",
        )

    def test_semester_rollover_by_disjoint_course_ids(self) -> None:
        legacy = _make_state(marks_count=2, res_count=0, course_count=1)
        self.assertNotIn("semester", legacy)
        self.store.save_state(legacy)
        new_course = Course(course_id="999001", code="CSC499", name="New Term Course")
        other = SimulatedPortal(courses=[new_course], semester="")

        res = run_cycle(
            portal_client(other),
            [new_course],
            self.store,
            CycleOptions(base_url=self.BASE, semester=""),
            [self.notifier],
        )
        self.assertTrue(res.saved)
        self.assertEqual(self.events(), ["semester_rollover"])
        self.assertIn("999001", self.saved()["courses"])

    def test_legacy_state_gains_a_semester_label_without_rolling_over(self) -> None:
        legacy = _make_state(marks_count=2, res_count=0, course_count=1)
        self.store.save_state(legacy)

        res = self.cycle(
            self.portal(
                marks=[
                    MarkItem(f"Quizzes:0_{i}", "Quizzes", f"Quiz{i}", "8.0", "10.0", "01 Nov")
                    for i in range(2)
                ]
            )
        )
        self.assertTrue(res.saved)
        self.assertEqual(self.notifier.notes, [])
        self.assertEqual(self.saved()["semester"], "Fall 2026")

    def test_reset_state_rebaselines_over_a_degenerate_snapshot(self) -> None:
        self.cycle(
            self.portal(
                marks=[
                    MarkItem(f"Quizzes:{i}", "Quizzes", f"Q{i}", "8.0", "10.0", "")
                    for i in range(6)
                ]
            )
        )
        self.notifier.notes.clear()

        res = self.cycle(self.portal(marks=[]), reset_state=True)
        self.assertTrue(res.saved)
        self.assertEqual(self.events(), ["baseline_saved"])
        self.assertEqual(self.saved()["courses"]["100001"]["marks"], {})

    # -- anomalies -----------------------------------------------------------
    def test_anomaly_rejection_dedupes_then_recovers(self) -> None:
        self.cycle(
            self.portal(
                marks=[
                    MarkItem(f"Quizzes:{i}", "Quizzes", f"Q{i}", "8.0", "10.0", "")
                    for i in range(6)
                ],
                resources=[ResourceItem("res-1", "Slides", "Week 1", "01 Nov", "1.0 MB", "/x")],
                set_cookie_on_switch="ARRAffinity",
            )
        )
        baseline = self.saved()
        self.notifier.notes.clear()

        # 1st degenerate scan: rejected, alerted once, diagnostics written.
        empty_marks = self.portal(marks=[], resources=[])
        empty_marks.set_cookie_on_switch = "ARRAffinity"
        res1 = self.cycle(empty_marks)
        self.assertFalse(res1.saved)
        self.assertIsNotNone(res1.anomaly)
        self.assertEqual(self.events(), ["scan_anomaly"])
        self.assertEqual(self.saved(), baseline)
        self.assertIn("ARRAffinity", self.notifier.notes[0].data["set_cookie_names"])

        marker = json.loads(self.data_dir.joinpath(".anomaly_alerted").read_text(encoding="utf-8"))
        self.assertEqual(marker["count"], 1)
        diag = json.loads(self.data_dir.joinpath("last_anomaly.json").read_text(encoding="utf-8"))
        self.assertEqual(diag["occurrence"], 1)
        self.assertEqual(diag["scanned_totals"], {"courses": 1, "marks": 0, "resources": 0})
        self.assertEqual(diag["baseline_totals"], {"courses": 1, "marks": 6, "resources": 1})
        self.assertIn("No marks available.", diag["sample_marks_preview"])
        diag_first_seen = marker["first_seen"]
        saved_html = self.data_dir.joinpath("last_anomaly_marks.html").read_text(encoding="utf-8")
        self.assertIn("No marks available.", saved_html)

        # 2nd identical anomaly: deduplicated (no notification) but counted.
        res2 = self.cycle(self.portal(marks=[], resources=[]))
        self.assertFalse(res2.saved)
        self.assertEqual(res2.notifications, [])
        self.assertEqual(self.events(), ["scan_anomaly"])
        marker = json.loads(self.data_dir.joinpath(".anomaly_alerted").read_text(encoding="utf-8"))
        self.assertEqual(marker["count"], 2)
        self.assertEqual(marker["first_seen"], diag_first_seen)

        # Recovery: state saved, one recovery alert ahead of the diff alert, marker cleared.
        res3 = self.cycle(
            self.portal(
                marks=[
                    MarkItem(f"Quizzes:{i}", "Quizzes", f"Q{i}", "8.0", "10.0", "")
                    for i in range(7)
                ],
                resources=[],
            )
        )
        self.assertTrue(res3.saved)
        self.assertIsNone(res3.anomaly)
        # The recovery alert always precedes the diff alerts; the scan below also drops
        # the baseline's single resource, which is a legitimate removal.
        self.assertEqual(
            self.events(),
            ["scan_anomaly", "scan_recovered", "marks_updated", "resource_removed"],
        )
        self.assertFalse(self.data_dir.joinpath(".anomaly_alerted").exists())
        self.assertEqual(len(self.saved()["courses"]["100001"]["marks"]), 7)

    def test_dry_run_anomaly_persists_no_marker_or_diagnostics(self) -> None:
        self.cycle(
            self.portal(
                marks=[
                    MarkItem(f"Quizzes:{i}", "Quizzes", f"Q{i}", "8.0", "10.0", "")
                    for i in range(6)
                ]
            )
        )
        self.notifier.notes.clear()

        res = self.cycle(self.portal(marks=[]), dry_run=True)
        self.assertFalse(res.saved)
        self.assertIsNotNone(res.anomaly)
        self.assertEqual(self.events(), ["scan_anomaly"])
        self.assertFalse(self.data_dir.joinpath(".anomaly_alerted").exists())
        self.assertFalse(self.data_dir.joinpath("last_anomaly.json").exists())

    def test_partial_scan_is_rejected_and_the_baseline_survives(self) -> None:
        second = Course(course_id="100002", code="MTH104", name="Calculus")
        portal = SimulatedPortal(
            courses=[self.course, second],
            semester="Fall 2026",
            marks={"100001": [MarkItem("Quizzes:1", "Quizzes", "Quiz1", "8.0", "10.0", "")],
                   "100002": [MarkItem("Quizzes:9", "Quizzes", "Q9", "5.0", "10.0", "")]},
        )
        self.cycle(portal, courses=[self.course, second])
        baseline = self.saved()
        self.notifier.notes.clear()

        portal.failures["/Courses/SetCourse/100002"] = PortalHttpError("HTTP 500")
        res = self.cycle(portal, courses=[self.course, second])
        self.assertFalse(res.saved)
        self.assertIn("Partial scan", res.anomaly or "")
        self.assertEqual(self.saved(), baseline)
        self.assertEqual(self.events(), ["scan_anomaly"])

    def test_mid_scan_session_expiry_leaves_state_untouched(self) -> None:
        second = Course(course_id="100002", code="MTH104", name="Calculus")
        self.store.save_state(_make_state(marks_count=2, res_count=1, course_count=2))
        baseline_bytes = self.state_path.read_bytes()

        portal = SimulatedPortal(
            courses=[self.course, second],
            semester="Fall 2026",
            marks={"100001": [MarkItem("Quizzes:100", "Quizzes", "New", "9.0", "10.0", "")]},
            failures={
                "/Courses/SetCourse/100002": StubResponse(
                    "", status=302, headers={"Location": "/Login/Index"}
                )
            },
        )
        with self.assertRaises(SessionExpiredError):
            self.cycle(portal, courses=[self.course, second])

        self.assertEqual(self.state_path.read_bytes(), baseline_bytes)
        self.assertEqual(self.notifier.notes, [])
        self.assertFalse(self.data_dir.joinpath(".anomaly_alerted").exists())

    # -- store discipline ----------------------------------------------------
    def test_baseline_is_loaded_before_the_portal_is_scanned(self) -> None:
        self.store.save_state(_make_state(marks_count=2, res_count=1, course_count=1))
        order: list = []
        orig_load = RuntimeStore.load_state

        def _spy_load(store_self):
            order.append("load_state")
            return orig_load(store_self)

        def _spy_scan(*args, **kwargs):
            order.append("scan_all_courses")
            return tracker.ScanResult(
                state=_make_state(marks_count=3, res_count=1, course_count=1)
            )

        with mock.patch.object(RuntimeStore, "load_state", _spy_load), mock.patch.object(
            tracker, "scan_all_courses", side_effect=_spy_scan
        ):
            res = run_cycle(
                None,  # type: ignore[arg-type]
                [self.course],
                self.store,
                CycleOptions(base_url=self.BASE),
                [],
            )

        self.assertTrue(res.saved)
        self.assertEqual(order, ["load_state", "scan_all_courses"])

    def test_cycle_options_and_legacy_kwargs_are_mutually_exclusive(self) -> None:
        with self.assertRaises(TypeError):
            run_cycle(
                None,  # type: ignore[arg-type]
                [self.course],
                self.store,
                CycleOptions(base_url=self.BASE),
                [],
                base_url=self.BASE,
            )
        with self.assertRaises(TypeError):
            run_cycle(None, [self.course], self.store, None, [], base_url=self.BASE,
                      not_a_real_option=1)

    def test_a_positional_string_in_place_of_options_is_rejected_loudly(self) -> None:
        # The old 6-argument positional signature would land base_url here; failing loudly
        # beats silently scanning with the wrong policy.
        with self.assertRaises(TypeError) as ctx:
            run_cycle(
                portal_client(self.portal(marks=[])),
                [self.course],
                self.store,
                self.BASE,  # type: ignore[arg-type]
                [],
            )
        self.assertIn("CycleOptions", str(ctx.exception))

    def test_legacy_keyword_call_style_still_works(self) -> None:
        res = run_cycle(
            portal_client(self.portal(marks=[])),
            [self.course],
            state_path=self.state_path,
            base_url=self.BASE,
            track_announcements=False,
            notifiers=[self.notifier],
            semester="Fall 2026",
        )
        self.assertTrue(res.saved)
        self.assertEqual(self.events(), ["baseline_saved"])
        self.assertTrue(self.state_path.exists())


class ReliabilityAndBugfixTests(unittest.TestCase):
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

            portal = SimulatedPortal(
                courses=courses,
                marks={
                    "100001": [
                        MarkItem("Quizzes:1", "Quizzes", "Q1", "9", "10", ""),
                    ]
                },
                expire_sessions={"expired_cookie"},
            )

            def _note_resume(path, headers, sim):
                if path == "/Courses" and sim._session_cookie(headers) != "expired_cookie":
                    resumed_event.set()
                return sim._respond(path, headers)

            portal.on_path["/Courses"] = _note_resume

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
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and not state_path.exists():
                    time.sleep(0.05)
                time.sleep(0.3)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_updater, daemon=True)
            t.start()
            try:
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal)
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ={"SIS_SESSION_ID": ""}
                    )
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

            # Every dashboard fetch for this session is a login redirect.
            portal = SimulatedPortal(expire_sessions={"still_expired"})

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            buf = io.StringIO()
            try:
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal)
                ), mock.patch.object(
                    tracker, "build_notifiers", return_value=[notifier]
                ), contextlib.redirect_stdout(buf):
                    fast_env = {"SIS_SESSION_ID": ""}
                    rc1 = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ=fast_env
                    )
                    rc2 = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ=fast_env
                    )
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

            # A genuinely wedged network: the transport blocks inside fetch() forever, which
            # is exactly the failure the cycle watchdog exists for.
            class _WedgedTransport(StubTransport):
                def fetch(self, url, headers, timeout=None):
                    wedge_gate.wait(10.0)
                    raise StubTransportError("never answered")

            portal = _WedgedTransport()

            fast_cfg = load_config(env_path)
            fast_cfg.cycle_timeout = 1  # type: ignore[assignment]
            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            try:
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal)
                ), mock.patch.object(tracker, "load_config", return_value=fast_cfg):
                    rc = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ={"SIS_SESSION_ID": ""}
                    )
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
        new_mark = MarkItem("Quizzes:100", "Quizzes", "NewQuiz", "9", "10", "")

        def _portal(**kwargs) -> SimulatedPortal:
            return SimulatedPortal(
                courses=[c1, c2], marks={"100001": [new_mark]}, **kwargs
            )

        # Course 2's SetCourse returns 500: course 1 lands, course 2 carries forward.
        partial_portal = _portal(
            failures={"/Courses/SetCourse/100002": PortalHttpError(
                "HTTP 500 fetching https://sis.comsats.edu.pk/Courses/SetCourse/100002"
            )}
        )
        scan = scan_all_courses(
            portal_client(partial_portal), [c1, c2], previous_state=prev_state
        )
        self.assertTrue(scan.partial)
        self.assertEqual(scan.failed_courses, [c2.label])
        self.assertIn("100001", scan.state["courses"])
        self.assertIn("Quizzes:100", scan.state["courses"]["100001"]["marks"])
        self.assertEqual(scan.state["courses"]["100002"], prev_state["courses"]["100002"])



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

            # A dashboard that answers 502 at the transport edge.
            portal = SimulatedPortal(failures={"/Courses": StubResponse("boom", status=502)})

            buf = io.StringIO()
            main_env = {
                "ENV_FILE": str(env_path),
                "STATE_FILE": str(state_path),
                "SIS_SESSION_ID": "",
            }
            with mock.patch.object(tracker, "SisClient", portal_factory(portal)), mock.patch.object(
                sys, "argv", ["tracker.py", "--once"]
            ), contextlib.redirect_stdout(buf):
                rc = tracker.main(environ=main_env)

            self.assertEqual(rc, 1)
            self.assertIn("[ERROR] Scan failed:", buf.getvalue())
            self.assertIn("502", buf.getvalue())

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
            dashboard_fetch_count = 0

            # Real client over a simulator that answers with the same dashboard every time.
            portal = SimulatedPortal(
                courses=courses,
                marks={"100001": []},
                resources={
                    "100001": [
                        ResourceItem(
                            key="res-0_0",
                            title="Slide 0",
                            material_type="Slides",
                            date_text="01 Nov 2026",
                            size="1.0 MB",
                            href="/LearningResources/Download/res-0_0",
                        )
                    ]
                },
            )

            def _count_dashboard(path, headers, sim):
                nonlocal dashboard_fetch_count
                if path == "/Courses":
                    dashboard_fetch_count += 1
                    if dashboard_fetch_count >= 2:
                        tick_event.set()
                return sim._respond(path, headers)

            portal.on_path["/Courses"] = _count_dashboard

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
                with mock.patch.object(tracker, "SisClient", portal_factory(portal)), mock.patch.object(
                    tracker, "load_config", return_value=fast_cfg
                ), mock.patch.object(tracker, "build_notifiers", return_value=[_RecordingNotifier()]):
                    rc = run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            self.assertGreaterEqual(dashboard_fetch_count, 2)
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

            # The portal keeps rejecting the *same* session cookie until the second .env
            # save; each dashboard fetch is one "attempt".
            portal = SimulatedPortal(
                courses=courses,
                marks={"100001": [MarkItem("Quizzes:1", "Quizzes", "Q1", "9", "10", "")]},
            )
            accepting = threading.Event()

            def _count_attempt(path, headers, sim):
                nonlocal attempts
                if path == "/Courses":
                    with attempt_lock:
                        attempts += 1
                        current = attempts
                    if current == 2:
                        retested_while_expired.set()
                    if accepting.is_set():
                        resumed_event.set()
                        return sim._respond(path, headers)
                    return sim._login_redirect()
                return sim._respond(path, headers)

            portal.on_path["/Courses"] = _count_attempt

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
                accepting.set()
                tmp2 = Path(tmp) / ".env.tmp2"
                tmp2.write_text(env_content, encoding="utf-8")
                os.replace(tmp2, env_path)
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and not state_path.exists():
                    time.sleep(0.05)
                time.sleep(0.3)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_same_cookie_updater, daemon=True)
            t.start()
            try:
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal)
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ={"SIS_SESSION_ID": ""}
                    )
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            self.assertEqual([n.event for n in notifier.notes], ["session_expired", "baseline_saved"])

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

            # Verify probe.load_config handles BOM + export + quoted inline comment
            probe_sid, _ = probe.load_config(
                env_path, environ={"SIS_SESSION_ID": "", "SIS_USER_AGENT": ""}
            )
            self.assertEqual(probe_sid, "expired_cookie")

            notifier = _RecordingNotifier()
            resumed_event = threading.Event()
            courses = [Course(course_id="100001", code="CSC101", name="Intro")]

            # Only "fresh_cookie" is accepted; "expired_cookie" gets the login page.
            portal = SimulatedPortal(
                courses=courses,
                marks={"100001": [MarkItem("Quizzes:1", "Quizzes", "Q1", "9", "10", "")]},
                expire_sessions={"expired_cookie"},
            )

            def _note_fresh(path, headers, sim):
                if (
                    path == "/Courses"
                    and sim._session_cookie(headers) == "fresh_cookie"
                ):
                    resumed_event.set()
                return sim._respond(path, headers)

            portal.on_path["/Courses"] = _note_fresh

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
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and not state_path.exists():
                    time.sleep(0.05)
                time.sleep(0.3)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_updater, daemon=True)
            t.start()
            try:
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal)
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ={"SIS_SESSION_ID": ""}
                    )
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

            # Raising ValueError out of the transport edge proves the daemon survives the
            # same class of bad-URL error urllib used to leak.
            class _ValueErrorTransport(StubTransport):
                def fetch(self, url, headers, timeout=None):
                    errored_event.set()
                    raise ValueError("unknown url type: bad")

            portal = _ValueErrorTransport()

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
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal)
                ), mock.patch.object(tracker, "build_notifiers", return_value=[_RecordingNotifier()]):
                    rc = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ={"SIS_SESSION_ID": ""}
                    )
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
            created: list = []

            # The SetCourse hop blocks until the daemon's cancel_event fires — the real
            # cooperative-cancellation path through the transport seam.
            class _SlowTransport(SimulatedPortal):
                def fetch(self, url, headers, timeout=None):
                    if urllib.parse.urlparse(url).path.startswith("/Courses/SetCourse/"):
                        client = created[-1]
                        cancel = getattr(client, "cancel_event", None)
                        if cancel is not None:
                            observed_cancel.append(cancel)
                        in_scan.set()
                        if cancel is not None:
                            cancel.wait(10.0)
                            if cancel.is_set():
                                raise StubTransportError("Request cancelled.")
                    return super().fetch(url, headers, timeout)

            portal = _SlowTransport(courses=courses)

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
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal, created)
                ), mock.patch.object(tracker, "build_notifiers", return_value=[_RecordingNotifier()]):
                    rc = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ={"SIS_SESSION_ID": ""}
                    )
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

        # In-cycle rollover / retake / legacy-upgrade scenarios are covered end-to-end by
        # CycleIntegrationTests and purely by tests/test_snapshot_transition.py; what stays
        # here is the CLI contract around --reset-state.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            # 4. --once --reset-state overrides same-semester >50% drop, plus SIS_SEMESTER override;
            #    --reset-state without --once returns 1
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=valid\nSIS_SEMESTER=Summer 2027\n", encoding="utf-8")
            big_state = _make_state(marks_count=6, res_count=2, course_count=1)
            big_state["semester"] = "Summer 2027"
            save_state(state_path, big_state)

            reset_portal = SimulatedPortal(
                courses=[Course(course_id="100001", code="CSC101", name="Course 1")]
            )
            reset_notifier = _RecordingNotifier()
            main_env = {
                "ENV_FILE": str(env_path),
                "STATE_FILE": str(state_path),
                "SIS_SESSION_ID": "",
            }
            with mock.patch.object(
                tracker, "SisClient", portal_factory(reset_portal)
            ), mock.patch.object(
                tracker, "build_notifiers", return_value=[reset_notifier]
            ):
                with mock.patch.object(sys, "argv", ["tracker.py", "--reset-state"]):
                    self.assertEqual(tracker.main(environ=main_env), 1)
                with mock.patch.object(sys, "argv", ["tracker.py", "--once", "--reset-state"]):
                    rc_reset = tracker.main(environ=main_env)

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
            accepted_once = threading.Event()

            # Dashboard fetch #1 expires, #2 succeeds (then the scan hits an anomaly),
            # #3 onward expire again — the exact sequence that proves a successful
            # roster fetch clears .session_alerted even when the scan then fails.
            portal = SimulatedPortal(
                courses=courses,
                marks={"100001": []},
                resources={"100001": []},
                semester="",
            )

            def _flaky_dashboard(path, headers, sim):
                nonlocal attempts
                if path != "/Courses":
                    return sim._respond(path, headers)
                with attempt_lock:
                    attempts += 1
                    current = attempts
                if current == 1:
                    return sim._login_redirect()
                if current == 2:
                    accepted_once.set()
                    return sim._respond(path, headers)
                second_expire_seen.set()
                return sim._login_redirect()

            portal.on_path["/Courses"] = _flaky_dashboard

            def _driver() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if hb_path.exists() and "waiting_for_session" in hb_path.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                # Touch .env with same cookie -> attempt #2 passes fetch_roster(), hits anomaly
                env_path.write_text(env_content + "# refreshed\n", encoding="utf-8")
                self.assertTrue(second_expire_seen.wait(timeout=5.0))
                time.sleep(0.2)
                signal.raise_signal(signal.SIGTERM)

            orig_load_config = tracker.load_config

            def _fast_load_config(
                ep: Path, require_session: bool = True, **kwargs: object
            ) -> tracker.TrackerConfig:
                cfg_obj = orig_load_config(ep, require_session=require_session, **kwargs)
                cfg_obj.keepalive_interval = 1
                return cfg_obj

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_driver, daemon=True)
            t.start()
            try:
                with mock.patch.object(
                    tracker, "SisClient", portal_factory(portal)
                ), mock.patch.object(
                    tracker, "load_config", side_effect=_fast_load_config
                ), mock.patch.object(tracker, "build_notifiers", return_value=[notifier]):
                    rc = run_daemon(
                        env_path, state_path, heartbeat_path=hb_path, environ={"SIS_SESSION_ID": ""}
                    )
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            self.assertTrue(accepted_once.is_set())
            events = [n.event for n in notifier.notes]
            self.assertEqual(events.count("session_expired"), 2)
            self.assertIn("scan_anomaly", events)


if __name__ == "__main__":
    unittest.main()
