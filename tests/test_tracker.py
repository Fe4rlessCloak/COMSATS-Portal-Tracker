#!/usr/bin/env python3
"""Unit tests for COMSATS Portal Tracker parsers, diffing, and notifiers."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notifiers import _latin1_safe  # noqa: E402
from sis_client import (  # noqa: E402
    parse_announcements,
    parse_courses,
    parse_learning_resources,
    parse_marks_summary,
)
from tracker import diff_states, load_state, save_state  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
