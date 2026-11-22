#!/usr/bin/env python3
"""Zero-seam tests for the pure Snapshot Transition module.

Every test here is plain Python in, plain Python out: no temp files, no sockets, no
locking, no notifier dispatch, no monkey-patching of behaviour. The only instrumentation
is passive: :class:`ProbeDict` / :class:`ProbeCourses` are ordinary dict subclasses that
count how many times their contents were read, which is how "totals once" and "rollover
once" are proven without a mock.
"""

from __future__ import annotations

import ast
import copy
import sys
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import snapshot_transition as st  # noqa: E402
from notifiers import Notification  # noqa: E402
from snapshot_transition import (  # noqa: E402
    ANOMALY_CLEAR,
    ANOMALY_NONE,
    ANOMALY_RECORD,
    KIND_ANOMALY_REJECTED,
    KIND_BASELINE_SAVED,
    KIND_DIFF_APPLIED,
    KIND_SEMESTER_ROLLOVER,
    ScanResult,
    TransitionOptions,
    compute_transition,
    detect_semester_rollover,
    diff_states,
    normalize_semester,
    summarize_totals,
    validate_snapshot,
)

BASE = "https://sis.comsats.edu.pk"
NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
OPTIONS = TransitionOptions(base_url=BASE)


# --------------------------------------------------------------------- fixtures


def mark(key: str, title: str, obtained: str = "8.0", total: str = "10.0",
         section: str = "Quizzes", date_text: str = "01 Nov 2026") -> Dict[str, Any]:
    return {
        "key": key,
        "section": section,
        "title": title,
        "obtained": obtained,
        "total": total,
        "date_text": date_text,
    }


def resource(key: str, title: str, href: Optional[str] = None,
             material_type: str = "Slides", size: str = "1.0 MB",
             date_text: str = "01 Nov 2026") -> Dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "material_type": material_type,
        "date_text": date_text,
        "size": size,
        "href": href if href is not None else f"/LearningResources/Download/{key}",
    }


def announcement(key: str, title: str, body: str = "Body") -> Dict[str, Any]:
    return {"key": key, "title": title, "meta": "10 Nov", "body": body}


def course_entry(cid: str, marks: int = 2, resources: int = 2, code: str = "CSC101",
                 name: str = "Intro", label: Optional[str] = None,
                 announcements: int = 0) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "code": code,
        "name": name,
        "label": label if label is not None else f"{code} — {name}",
        "marks": {
            f"Quizzes:{cid}_{i}": mark(f"Quizzes:{cid}_{i}", f"Quiz{i}") for i in range(marks)
        },
        "resources": {
            f"res-{cid}_{i}": resource(f"res-{cid}_{i}", f"Slide {i}")
            for i in range(resources)
        },
    }
    if announcements:
        entry["announcements"] = {
            f"Ann{cid}_{i}|10 Nov": announcement(f"Ann{cid}_{i}|10 Nov", f"Ann{i}")
            for i in range(announcements)
        }
    return entry


def state(courses: int = 1, marks: int = 2, resources: int = 2,
          semester: Optional[str] = None, course_ids: Optional[list] = None) -> Dict[str, Any]:
    ids = course_ids if course_ids is not None else [str(100001 + i) for i in range(courses)]
    data: Dict[str, Any] = {
        "courses": {
            cid: course_entry(cid, marks=marks, resources=resources, code=f"CSC{idx + 100}")
            for idx, cid in enumerate(ids)
        }
    }
    if semester is not None:
        data["semester"] = semester
    return data


def scan_of(snapshot: Optional[Dict[str, Any]] = None, **kwargs: Any) -> ScanResult:
    return ScanResult(state=snapshot if snapshot is not None else state(), **kwargs)


# -------------------------------------------------------------- instrumentation


class ProbeCourses(dict):
    """A courses mapping that counts how many times its contents were walked.

    ``summarize_totals`` is the only function in the module that iterates ``values()``
    (once for marks, once for resources); diffing walks ``items()``. So
    ``totals_computed`` is exactly "how many times were this snapshot's totals computed".
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.values_calls = 0

    def values(self):  # type: ignore[override]
        self.values_calls += 1
        return super().values()

    @property
    def totals_computed(self) -> int:
        return self.values_calls // 2


class ProbeDict(dict):
    """A state mapping that counts key reads, so re-evaluation is observable."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reads: Counter = Counter()

    def get(self, key, default=None):  # type: ignore[override]
        self.reads[key] += 1
        return super().get(key, default)

    def __getitem__(self, key):  # type: ignore[override]
        self.reads[key] += 1
        return super().__getitem__(key)


def probe_previous(snapshot: Dict[str, Any]) -> ProbeDict:
    """Wrap a snapshot so both its semester key and its totals are observable."""
    probe = ProbeDict(snapshot)
    probe["courses"] = ProbeCourses(snapshot["courses"])
    return probe


def probe_scan(snapshot: Dict[str, Any]) -> ScanResult:
    wrapped = dict(snapshot)
    wrapped["courses"] = ProbeCourses(snapshot["courses"])
    return ScanResult(state=wrapped)


def courses_of(data: Dict[str, Any]) -> ProbeCourses:
    courses = data["courses"]
    assert isinstance(courses, ProbeCourses)
    return courses


# ---------------------------------------------------------------- module purity


class ModulePurityTests(unittest.TestCase):
    def test_imports_only_stdlib_and_inert_notification_dataclass(self) -> None:
        source = (ROOT / "snapshot_transition.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import
                    self.fail("snapshot_transition must not use relative imports")
                imported.add((node.module or "").split(".")[0])
        allowed = {
            "__future__",
            "dataclasses",
            "datetime",
            "typing",
            "urllib",
            "notifiers",
        }
        self.assertLessEqual(imported, allowed)
        for forbidden in ("tracker", "runtime_store", "os", "io", "json", "socket",
                          "http", "urllib.request", "subprocess", "logging", "tempfile"):
            self.assertNotIn(forbidden, imported)

    def test_no_io_primitives_in_the_module_source(self) -> None:
        tree = ast.parse((ROOT / "snapshot_transition.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # Drop every docstring so prose about what the module must NOT do is not
            # mistaken for code that does it.
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant
                ) and isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
        source = ast.unparse(tree)
        for needle in (
            "open(",
            "os.environ",
            "Path(",
            "dispatch_all",
            "RuntimeStore",
            "save_state",
            "write_heartbeat",
            "flock",
            "print(",
        ):
            self.assertNotIn(needle, source, msg=f"snapshot_transition must stay pure: {needle!r}")

    def test_importing_it_does_not_drag_in_the_impure_modules(self) -> None:
        # Re-import in a subprocess-free way: the modules were imported by the test run
        # anyway, so inspect the recorded dependency set instead.
        self.assertNotIn("tracker", getattr(st, "__dict__", {}))
        self.assertFalse(hasattr(st, "RuntimeStore"))
        self.assertFalse(hasattr(st, "dispatch_all"))

    def test_notification_dataclass_seam_is_the_inert_one(self) -> None:
        note = st.Notification(event="e", title="t", message="m")
        self.assertIsInstance(note, Notification)
        self.assertEqual(note.priority, "default")


# --------------------------------------------------------------------- totals


class TotalsAndNormalizationTests(unittest.TestCase):
    def test_summarize_totals(self) -> None:
        self.assertEqual(summarize_totals({"courses": {}}), (0, 0, 0))
        self.assertEqual(summarize_totals({}), (0, 0, 0))
        self.assertEqual(summarize_totals(state(courses=2, marks=3, resources=4)), (2, 6, 8))
        self.assertEqual(
            summarize_totals({"courses": {"a": {"label": "A"}}}), (1, 0, 0)
        )

    def test_normalize_semester(self) -> None:
        self.assertEqual(normalize_semester("  Fall   2026 "), "fall 2026")
        self.assertEqual(normalize_semester("FALL\t2026"), "fall 2026")
        self.assertEqual(normalize_semester(""), "")
        self.assertEqual(st._norm_semester("Spring  2027"), "spring 2027")


# ------------------------------------------------------------------- rollover


class SemesterRolloverTests(unittest.TestCase):
    def test_no_previous_state_never_rolls_over(self) -> None:
        self.assertIsNone(detect_semester_rollover(None, state(semester="Fall 2026")))

    def test_identical_label_is_not_a_rollover(self) -> None:
        self.assertIsNone(
            detect_semester_rollover(state(semester="Fall 2026"), state(semester="Fall 2026"))
        )

    def test_labels_differing_only_in_case_and_spacing_are_not_a_rollover(self) -> None:
        self.assertIsNone(
            detect_semester_rollover(
                state(semester="Fall  2026"), state(semester=" fall 2026 ")
            )
        )

    def test_changed_label_rolls_over_and_reports_raw_labels(self) -> None:
        self.assertEqual(
            detect_semester_rollover(
                state(semester=" Fall 2026 "), state(semester="Spring 2027 ")
            ),
            ("Fall 2026", "Spring 2027"),
        )

    def test_label_omitted_on_either_side_falls_through_to_course_ids(self) -> None:
        overlapping = state(semester=None)
        self.assertIsNone(
            detect_semester_rollover(state(semester="Fall 2026"), overlapping)
        )
        self.assertIsNone(
            detect_semester_rollover(overlapping, state(semester="Spring 2027"))
        )

    def test_disjoint_course_ids_roll_over_with_placeholder_labels(self) -> None:
        old = state(course_ids=["100001"], semester=None)
        new = state(course_ids=["999001"], semester=None)
        self.assertEqual(
            detect_semester_rollover(old, new), ("previous semester", "new semester")
        )

    def test_disjoint_ids_with_one_label_present_use_that_label(self) -> None:
        old = state(course_ids=["100001"], semester="Fall 2026")
        new = state(course_ids=["999001"], semester=None)
        self.assertEqual(
            detect_semester_rollover(old, new), ("Fall 2026", "new semester")
        )

    def test_overlapping_ids_and_empty_states_never_roll_over(self) -> None:
        self.assertIsNone(
            detect_semester_rollover(
                state(course_ids=["100001", "100002"], semester=None),
                state(course_ids=["100001"], semester=None, marks=1),
            )
        )
        self.assertIsNone(detect_semester_rollover({"courses": {}}, {"courses": {}}))

    def test_rollover_beats_a_degenerate_drop(self) -> None:
        old = state(courses=3, marks=6, resources=6, semester="Fall 2026")
        new = state(courses=3, marks=0, resources=0, semester="Spring 2027")
        self.assertIsNone(validate_snapshot(old, scan_of(new)))


# ----------------------------------------------------------------- validation


class SnapshotValidationTests(unittest.TestCase):
    def test_session_expiry_is_rejected_first(self) -> None:
        reason = validate_snapshot(
            state(),
            scan_of(state(), session_expired=True, session_expired_error="cookie stale"),
        )
        self.assertEqual(reason, "Session expired mid-scan: cookie stale")

    def test_session_expiry_is_rejected_by_the_standalone_gate_only(self) -> None:
        """A2: expiry has ONE owner. validate_snapshot() names it; compute_transition()
        refuses an expired scan outright so there is no second, unreachable branch."""
        expired = scan_of(
            state(), session_expired=True, session_expired_error="cookie stale"
        )
        self.assertEqual(
            validate_snapshot(state(), expired), "Session expired mid-scan: cookie stale"
        )
        with self.assertRaises(ValueError):
            transition(state(), expired)

    def test_partial_scan_is_rejected_and_names_the_failed_courses(self) -> None:
        reason = validate_snapshot(
            state(),
            scan_of(state(), partial=True, failed_courses=["CSC101 — Intro", "MTH104"]),
        )
        self.assertEqual(
            reason, "Partial scan (2 course(s) failed: CSC101 — Intro, MTH104)"
        )

    def test_zero_course_scan_is_rejected_even_without_a_baseline(self) -> None:
        self.assertEqual(
            validate_snapshot(None, scan_of({"courses": {}})), "Scan returned 0 courses."
        )

    def test_marks_to_zero_is_a_degenerate_drop(self) -> None:
        self.assertEqual(
            validate_snapshot(state(marks=6), scan_of(state(marks=0))),
            "Total marks dropped from 6 to 0 (degenerate scan).",
        )

    def test_resources_reach_zero_without_rejection_below_the_threshold(self) -> None:
        # The sharp-drop rule needs >=5 baseline resources; 4 -> 0 is a legitimate cleanup.
        self.assertIsNone(
            validate_snapshot(state(marks=2, resources=4), scan_of(state(marks=2, resources=0)))
        )
        # At the threshold it is rejected like any other sharp drop.
        self.assertEqual(
            validate_snapshot(state(marks=2, resources=6), scan_of(state(marks=2, resources=0))),
            "Total resources dropped sharply from 6 to 0.",
        )

    def test_course_count_sharp_drop_requires_three_or_more_baseline_courses(self) -> None:
        self.assertEqual(
            validate_snapshot(
                state(courses=4, marks=6, resources=6),
                scan_of(state(courses=1, marks=6, resources=6)),
            ),
            "Course count dropped sharply from 4 to 1.",
        )
        # Exactly half is not "less than half".
        self.assertIsNone(
            validate_snapshot(
                state(courses=4, marks=6, resources=6),
                scan_of(state(courses=2, marks=6, resources=6)),
            )
        )
        # Two baseline courses is below the threshold, however sharp the drop.
        self.assertIsNone(
            validate_snapshot(
                state(courses=2, marks=6, resources=6),
                scan_of(state(courses=1, marks=6, resources=6)),
            )
        )

    def test_marks_sharp_drop_requires_five_or_more_baseline_marks(self) -> None:
        self.assertEqual(
            validate_snapshot(state(marks=6), scan_of(state(marks=2))),
            "Total marks dropped sharply from 6 to 2.",
        )
        self.assertIsNone(validate_snapshot(state(marks=4), scan_of(state(marks=2))))

    def test_resources_sharp_drop_requires_five_or_more_baseline_resources(self) -> None:
        self.assertEqual(
            validate_snapshot(
                state(resources=6), scan_of(state(resources=2, marks=2))
            ),
            "Total resources dropped sharply from 6 to 2.",
        )
        self.assertIsNone(
            validate_snapshot(state(resources=4), scan_of(state(resources=2)))
        )

    def test_small_legitimate_removals_pass(self) -> None:
        self.assertIsNone(
            validate_snapshot(state(marks=2, resources=1), scan_of(state(marks=1, resources=0)))
        )

    def test_reset_state_bypasses_every_drop_rule(self) -> None:
        old = state(marks=6, resources=6)
        degenerate = scan_of(state(marks=0, resources=0))
        self.assertIsNotNone(validate_snapshot(old, degenerate))
        self.assertIsNone(validate_snapshot(old, degenerate, reset_state=True))

    def test_no_baseline_accepts_anything_plausible(self) -> None:
        self.assertIsNone(validate_snapshot(None, scan_of(state())))

    def test_validation_computes_totals_once_per_snapshot(self) -> None:
        previous = probe_previous(state(marks=6))
        scanned = probe_scan(state(marks=6))
        self.assertIsNone(validate_snapshot(previous, scanned))
        self.assertEqual(courses_of(previous).totals_computed, 1)
        self.assertEqual(courses_of(scanned.state).totals_computed, 1)


# ----------------------------------------------------------------------- diff


OLD_DIFF_STATE = {
    "courses": {
        "100001": {
            "code": "CSC101",
            "name": "Intro",
            "label": "CSC101 — Intro",
            "marks": {
                "Quizzes:1": mark("Quizzes:1", "Quiz1", "7.0", date_text="31 Oct 2026"),
                "Quizzes:2": mark("Quizzes:2", "Quiz2", "8.0"),
            },
            "resources": {"res-1": resource("res-1", "Week 1")},
            "announcements": {"Ann1|10 Nov": announcement("Ann1|10 Nov", "Ann1", "Hi")},
        },
        "100002": {
            "code": "MTH104",
            "name": "Calculus",
            "label": "MTH104 — Calculus",
            "marks": {
                "Assignments:9": mark(
                    "Assignments:9", "Q9", "5.0", section="Assignments"
                )
            },
            "resources": {},
            "announcements": {},
        },
    }
}

NEW_DIFF_STATE = {
    "courses": {
        "100001": {
            "code": "CSC101",
            "name": "Intro",
            "label": "CSC101 — Intro",
            "marks": {
                "Quizzes:1": mark("Quizzes:1", "Quiz1", "9.0", date_text="31 Oct 2026"),
                "Assignments:3": mark(
                    "Assignments:3", "Assign1", "9.0", section="Assignments"
                ),
            },
            "resources": {
                "res-2": resource(
                    "res-2", "Week 2", material_type="Handout", size="2.0 MB",
                    date_text="03 Nov 2026",
                )
            },
            "announcements": {"Ann2|12 Nov": announcement("Ann2|12 Nov", "Ann2", "New")},
        },
    }
}


class DiffTests(unittest.TestCase):
    def test_identical_states_produce_no_notifications(self) -> None:
        self.assertEqual(
            diff_states(OLD_DIFF_STATE, copy.deepcopy(OLD_DIFF_STATE), BASE,
                        track_announcements=True),
            [],
        )

    def test_marks_updated_notification_is_byte_identical(self) -> None:
        notes = diff_states(OLD_DIFF_STATE, NEW_DIFF_STATE, BASE)
        self.assertEqual(
            [n.event for n in notes],
            ["marks_updated", "marks_removed", "resource_added", "resource_removed",
             "course_removed"],
        )
        note = notes[0]
        self.assertEqual(note.title, "Marks update: CSC101 — Intro")
        self.assertEqual(note.priority, "high")
        self.assertEqual(list(note.tags), ["mortar_board", "chart_with_upwards_trend"])
        self.assertEqual(note.url, f"{BASE}/Courses/SetCourse/100001")
        self.assertEqual(
            note.message,
            "~ [Quizzes] Quiz1: 7.0 / 10.0 -> 9.0 / 10.0\n"
            "+ [Assignments] Assign1: 9.0 / 10.0",
        )
        self.assertEqual(note.data["course_id"], "100001")
        self.assertEqual(note.data["course"], "CSC101 — Intro")
        self.assertEqual(note.data["items"][0]["change"], "updated")
        self.assertEqual(note.data["items"][0]["previous"]["obtained"], "7.0")
        self.assertEqual(note.data["items"][0]["obtained"], "9.0")
        self.assertEqual(note.data["items"][1]["change"], "added")

    def test_marks_removed_notification_is_byte_identical(self) -> None:
        note = diff_states(OLD_DIFF_STATE, NEW_DIFF_STATE, BASE)[1]
        self.assertEqual(note.event, "marks_removed")
        self.assertEqual(note.title, "Marks removed: CSC101 — Intro")
        self.assertEqual(note.priority, "high")
        self.assertEqual(list(note.tags), ["warning", "chart_with_downwards_trend"])
        self.assertEqual(note.url, f"{BASE}/Courses/SetCourse/100001")
        self.assertEqual(note.message, "- [Quizzes] Quiz2: 8.0 / 10.0")
        self.assertEqual(note.data["items"][0]["change"], "removed")

    def test_resource_added_resolves_relative_href_and_keeps_metadata(self) -> None:
        note = diff_states(OLD_DIFF_STATE, NEW_DIFF_STATE, BASE)[2]
        self.assertEqual(note.event, "resource_added")
        self.assertEqual(note.title, "New material: CSC101 — Intro")
        self.assertEqual(note.priority, "default")
        self.assertEqual(list(note.tags), ["books", "page_facing_up"])
        self.assertEqual(note.url, f"{BASE}/LearningResources/Download/res-2")
        self.assertEqual(
            note.message,
            "+ Week 2 (Handout, 2.0 MB, 03 Nov 2026)\n"
            f"  {BASE}/LearningResources/Download/res-2",
        )

    def test_resource_added_without_href_falls_back_to_the_course_url(self) -> None:
        old = {"courses": {"1": {"label": "C", "resources": {}}}}
        new = {"courses": {"1": {"label": "C", "resources": {"r": {"key": "r", "title": "Doc", "href": ""}}}}}
        note = diff_states(old, new, BASE)[0]
        self.assertEqual(note.event, "resource_added")
        self.assertEqual(note.url, f"{BASE}/Courses/SetCourse/1")
        self.assertEqual(note.message, "+ Doc")

    def test_resource_removed_notification_is_byte_identical(self) -> None:
        note = diff_states(OLD_DIFF_STATE, NEW_DIFF_STATE, BASE)[3]
        self.assertEqual(note.event, "resource_removed")
        self.assertEqual(note.title, "Material removed: CSC101 — Intro")
        self.assertEqual(list(note.tags), ["warning", "books"])
        self.assertEqual(note.message, "- Week 1 (Slides, 1.0 MB, 01 Nov 2026)")
        self.assertEqual(note.url, f"{BASE}/Courses/SetCourse/100001")

    def test_course_removed_notification_is_byte_identical(self) -> None:
        note = diff_states(OLD_DIFF_STATE, NEW_DIFF_STATE, BASE)[-1]
        self.assertEqual(note.event, "course_removed")
        self.assertEqual(note.title, "Course removed: MTH104 — Calculus")
        self.assertEqual(note.priority, "high")
        self.assertEqual(list(note.tags), ["warning"])
        self.assertEqual(note.url, f"{BASE}/Courses")
        self.assertEqual(
            note.message,
            "Course MTH104 — Calculus is no longer present (had 1 marks, 0 resources).",
        )
        self.assertEqual(
            note.data,
            {
                "course_id": "100002",
                "course": "MTH104 — Calculus",
                "marks_count": 1,
                "resources_count": 0,
            },
        )

    def test_announcements_are_diffed_only_when_tracking_is_on(self) -> None:
        without = diff_states(OLD_DIFF_STATE, NEW_DIFF_STATE, BASE)
        self.assertNotIn("announcement_added", [n.event for n in without])
        self.assertNotIn("announcement_removed", [n.event for n in without])

        with_ann = diff_states(
            OLD_DIFF_STATE, NEW_DIFF_STATE, BASE, track_announcements=True
        )
        events = [n.event for n in with_ann]
        self.assertEqual(
            events,
            ["marks_updated", "marks_removed", "resource_added", "resource_removed",
             "announcement_added", "announcement_removed", "course_removed"],
        )
        added = with_ann[4]
        self.assertEqual(added.title, "Announcement: CSC101 — Intro")
        self.assertEqual(list(added.tags), ["loudspeaker"])
        self.assertEqual(added.message, "+ Ann2\n  New")
        removed = with_ann[5]
        self.assertEqual(removed.title, "Announcement removed: CSC101 — Intro")
        self.assertEqual(removed.message, "- Ann1")

    def test_announcement_snippet_is_truncated_to_180_characters(self) -> None:
        old = {"courses": {"1": {"label": "C", "announcements": {}}}}
        new = {
            "courses": {
                "1": {
                    "label": "C",
                    "announcements": {"a": {"key": "a", "title": "T", "body": "x" * 300}},
                }
            }
        }
        note = diff_states(old, new, BASE, track_announcements=True)[0]
        self.assertEqual(note.message, "+ T\n  " + "x" * 180)

    def test_first_baseline_produces_no_noise(self) -> None:
        # Everything is "added", but nothing is removed, and additions are expected.
        notes = diff_states({"courses": {}}, NEW_DIFF_STATE, BASE, track_announcements=True)
        self.assertNotIn("course_removed", [n.event for n in notes])
        self.assertEqual(len(notes), 3)


# ------------------------------------------------------- compute_transition


def transition(previous, scan, prior=None, options=OPTIONS, now=NOW) -> "st.TransitionOutcome":
    return compute_transition(previous, scan, prior, now, options)


class BaselineTransitionTests(unittest.TestCase):
    def test_first_run_saves_a_baseline(self) -> None:
        outcome = transition(None, scan_of(state(semester="Fall 2026")))
        self.assertEqual(outcome.kind, KIND_BASELINE_SAVED)
        self.assertTrue(outcome.persist_state)
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_NONE)
        self.assertEqual(outcome.baseline_totals, (0, 0, 0))
        self.assertEqual(outcome.scanned_totals, (1, 2, 2))
        self.assertEqual([n.event for n in outcome.notifications], ["baseline_saved"])
        note = outcome.notifications[0]
        self.assertEqual(note.title, "COMSATS Portal Tracker started")
        self.assertEqual(note.message, "Baseline saved for 1 courses (Fall 2026: 2 marks, 2 resources).")
        self.assertEqual(note.priority, "low")
        self.assertEqual(list(note.tags), ["white_check_mark"])
        self.assertEqual(note.url, f"{BASE}/Courses")
        self.assertEqual(note.data, {"courses": 1, "marks": 2, "resources": 2,
                                     "semester": "Fall 2026"})
        self.assertEqual(outcome.state, state(semester="Fall 2026"))
        self.assertEqual(outcome.summary["status"], "ok")
        self.assertIsNone(outcome.summary["anomaly"])

    def test_baseline_without_a_semester_label(self) -> None:
        outcome = transition(None, scan_of(state()))
        note = outcome.notifications[0]
        self.assertEqual(note.message, "Baseline saved for 1 courses (2 marks, 2 resources).")
        self.assertEqual(note.data, {"courses": 1, "marks": 2, "resources": 2})

    def test_reset_state_rebaselines_over_an_existing_snapshot(self) -> None:
        options = TransitionOptions(base_url=BASE, reset_state=True)
        previous = state(marks=6, resources=6, semester="Summer 2027")
        outcome = transition(previous, scan_of(state(marks=0, resources=0,
                                                    semester="Summer 2027")), options=options)
        self.assertEqual(outcome.kind, KIND_BASELINE_SAVED)
        self.assertTrue(outcome.persist_state)
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_NONE)
        self.assertEqual([n.event for n in outcome.notifications], ["baseline_saved"])
        self.assertEqual(outcome.baseline_totals, (1, 6, 6))

    def test_reset_state_increments_no_anomaly_marker(self) -> None:
        options = TransitionOptions(base_url=BASE, reset_state=True)
        prior = {"key": "Scan returned 0 courses.", "reason": "Scan returned 0 courses.",
                 "count": 3, "first_seen": "2026-01-01T00:00:00+00:00"}
        outcome = transition(state(marks=6, resources=6), scan_of(state(marks=0, resources=0)),
                             prior, options)
        self.assertEqual(outcome.kind, KIND_BASELINE_SAVED)
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_CLEAR)


class DiffTransitionTests(unittest.TestCase):
    def test_no_change_transition_has_no_notifications_but_still_commits(self) -> None:
        previous = state(marks=2, resources=1)
        outcome = transition(previous, scan_of(copy.deepcopy(previous)))
        self.assertEqual(outcome.kind, KIND_DIFF_APPLIED)
        self.assertTrue(outcome.persist_state)
        self.assertEqual(outcome.notifications, [])
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_NONE)
        self.assertEqual(
            outcome.summary["message"],
            "Scanned 1 courses (2 marks, 1 resources) — no changes.",
        )
        self.assertFalse(outcome.summary["changed"])

    def test_changed_snapshot_emits_the_diff_notifications_in_order(self) -> None:
        outcome = transition(OLD_DIFF_STATE, scan_of(NEW_DIFF_STATE))
        self.assertEqual(outcome.kind, KIND_DIFF_APPLIED)
        self.assertEqual(
            [n.event for n in outcome.notifications],
            ["marks_updated", "marks_removed", "resource_added", "resource_removed",
             "course_removed"],
        )
        self.assertIsNone(outcome.summary["message"])
        self.assertTrue(outcome.summary["changed"])

    def test_semester_label_is_carried_forward_when_the_scan_omits_it(self) -> None:
        previous = state(semester="Fall 2026")
        outcome = transition(previous, scan_of(state(marks=1, resources=1)))
        assert outcome.state is not None
        self.assertEqual(outcome.state["semester"], "Fall 2026")
        self.assertEqual(outcome.kind, KIND_DIFF_APPLIED)

    def test_a_present_semester_label_is_not_rewritten(self) -> None:
        previous = state(semester="Fall 2026")
        outcome = transition(previous, scan_of(state(semester="Fall 2026", marks=1)))
        assert outcome.state is not None
        self.assertEqual(outcome.state["semester"], "Fall 2026")

    def test_announcement_tracking_reaches_the_diff(self) -> None:
        options = TransitionOptions(base_url=BASE, track_announcements=True)
        outcome = transition(OLD_DIFF_STATE, scan_of(NEW_DIFF_STATE), options=options)
        self.assertIn("announcement_added", [n.event for n in outcome.notifications])
        self.assertIn("announcement_removed", [n.event for n in outcome.notifications])


class RolloverTransitionTests(unittest.TestCase):
    def test_changed_label_rolls_over_once(self) -> None:
        outcome = transition(
            state(marks=6, semester="Fall 2026"),
            scan_of(state(marks=6, semester="Spring 2027")),
        )
        self.assertEqual(outcome.kind, KIND_SEMESTER_ROLLOVER)
        self.assertTrue(outcome.persist_state)
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_NONE)
        self.assertEqual([n.event for n in outcome.notifications], ["semester_rollover"])
        note = outcome.notifications[0]
        self.assertEqual(note.title, "New semester detected: Spring 2027")
        self.assertEqual(
            note.message,
            "Transition: Fall 2026 -> Spring 2027\n"
            "Saved fresh baseline for 1 courses (6 marks, 2 resources).\n"
            "- CSC100 — Intro",
        )
        self.assertEqual(note.priority, "default")
        self.assertEqual(list(note.tags), ["mortar_board", "white_check_mark"])
        self.assertEqual(note.url, f"{BASE}/Courses")
        self.assertEqual(
            note.data,
            {"previous_semester": "Fall 2026", "semester": "Spring 2027",
             "courses": 1, "marks": 6, "resources": 2},
        )

    def test_disjoint_ids_roll_over_with_the_generic_title(self) -> None:
        outcome = transition(
            state(course_ids=["100001"], semester=None),
            scan_of(state(course_ids=["999001"], semester=None, marks=0, resources=0)),
        )
        self.assertEqual(outcome.kind, KIND_SEMESTER_ROLLOVER)
        note = outcome.notifications[0]
        self.assertEqual(note.title, "New semester detected")
        self.assertTrue(note.message.startswith("Transition: previous semester -> new semester"))

    def test_rollover_beats_every_drop_rule(self) -> None:
        outcome = transition(
            state(courses=4, marks=6, resources=6, semester="Fall 2026"),
            scan_of({"courses": {"999001": course_entry("999001", marks=0, resources=0)},
                     "semester": "Spring 2027"}),
        )
        self.assertEqual(outcome.kind, KIND_SEMESTER_ROLLOVER)
        self.assertIsNone(outcome.summary["anomaly"])

    def test_reset_state_suppresses_rollover_detection(self) -> None:
        options = TransitionOptions(base_url=BASE, reset_state=True)
        outcome = transition(state(marks=6, semester="Fall 2026"),
                             scan_of(state(marks=6, semester="Spring 2027")), options=options)
        self.assertEqual(outcome.kind, KIND_BASELINE_SAVED)
        self.assertEqual([n.event for n in outcome.notifications], ["baseline_saved"])


class AnomalyTransitionTests(unittest.TestCase):
    def test_rejected_snapshot_records_marker_and_diagnostics(self) -> None:
        scan = scan_of(
            state(marks=0, resources=1),
            set_cookie_names=["ARRAffinity"],
            sample_marks_html="<html>scrubbed</html>",
            sample_marks_preview="No marks available.",
            sample_marks_course="CSC101 — Intro",
        )
        outcome = transition(state(marks=6, resources=1), scan)
        self.assertEqual(outcome.kind, KIND_ANOMALY_REJECTED)
        self.assertFalse(outcome.persist_state)
        self.assertIsNone(outcome.state)
        self.assertEqual(outcome.baseline_totals, (1, 6, 1))
        self.assertEqual(outcome.scanned_totals, (1, 0, 1))

        action = outcome.anomaly_action
        self.assertEqual(action.kind, ANOMALY_RECORD)
        self.assertEqual(
            action.marker,
            {
                "key": "Total marks dropped from 6 to 0 (degenerate scan).",
                "reason": "Total marks dropped from 6 to 0 (degenerate scan).",
                "first_seen": NOW.isoformat(),
                "last_seen": NOW.isoformat(),
                "count": 1,
                "scanned": {"courses": 1, "marks": 0, "resources": 1},
            },
        )
        diag = action.diagnostics
        assert diag is not None
        self.assertEqual(diag.occurrence, 1)
        self.assertEqual(diag.scanned_totals, {"courses": 1, "marks": 0, "resources": 1})
        self.assertEqual(diag.baseline_totals, {"courses": 1, "marks": 6, "resources": 1})
        self.assertEqual(diag.set_cookie_names, ("ARRAffinity",))
        self.assertEqual(diag.sample_marks_course, "CSC101 — Intro")
        self.assertEqual(diag.marks_html, "<html>scrubbed</html>")

    def test_anomaly_notification_is_byte_identical(self) -> None:
        scan = scan_of(
            state(marks=0, resources=1),
            set_cookie_names=["ARRAffinity"],
            sample_marks_preview="No marks available.",
        )
        note = transition(state(marks=6, resources=1), scan).notifications[0]
        self.assertEqual(note.event, "scan_anomaly")
        self.assertEqual(note.title, "Portal scan anomaly (state preserved)")
        self.assertEqual(note.priority, "high")
        self.assertEqual(list(note.tags), ["warning"])
        self.assertEqual(note.url, f"{BASE}/Courses")
        self.assertEqual(
            note.message,
            "Total marks dropped from 6 to 0 (degenerate scan).\n"
            "Scanned: 1 courses, 0 marks, 1 resources (baseline: 1 courses, 6 marks, 1 resources).\n"
            "Set-Cookie names observed: ARRAffinity\n"
            "Page preview: No marks available.\n"
            "Existing baseline was NOT overwritten. "
            "Further identical alerts are suppressed until recovery.",
        )
        self.assertEqual(
            note.data,
            {
                "reason": "Total marks dropped from 6 to 0 (degenerate scan).",
                "occurrence": 1,
                "failed_courses": [],
                "scanned": {"courses": 1, "marks": 0, "resources": 1},
                "baseline": {"courses": 1, "marks": 6, "resources": 1},
                "set_cookie_names": ["ARRAffinity"],
            },
        )

    def test_identical_reason_is_deduplicated_and_the_occurrence_increments(self) -> None:
        prior = {
            "key": "Scan returned 0 courses.",
            "reason": "Scan returned 0 courses.",
            "first_seen": "2026-11-01T00:00:00+00:00",
            "last_seen": "2026-11-01T00:00:00+00:00",
            "count": 2,
            "scanned": {"courses": 0, "marks": 0, "resources": 0},
        }
        outcome = transition(state(), scan_of({"courses": {}}), prior)
        self.assertEqual(outcome.kind, KIND_ANOMALY_REJECTED)
        self.assertEqual(outcome.notifications, [])
        action = outcome.anomaly_action
        self.assertEqual(action.kind, ANOMALY_RECORD)
        assert action.marker is not None
        self.assertEqual(action.marker["count"], 3)
        self.assertEqual(action.marker["first_seen"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(action.marker["last_seen"], NOW.isoformat())
        assert action.diagnostics is not None
        self.assertEqual(action.diagnostics.occurrence, 3)
        self.assertTrue(outcome.summary["duplicate_suppressed"])
        self.assertFalse(outcome.summary["notified"])

    def test_a_new_reason_restarts_the_occurrence_count(self) -> None:
        prior = {"key": "Scan returned 0 courses.", "reason": "Scan returned 0 courses.",
                 "count": 4, "first_seen": "2026-11-01T00:00:00+00:00"}
        outcome = transition(state(marks=6), scan_of(state(marks=0)), prior)
        action = outcome.anomaly_action
        assert action.marker is not None
        self.assertEqual(action.marker["count"], 1)
        self.assertEqual(action.marker["first_seen"], NOW.isoformat())
        self.assertEqual([n.event for n in outcome.notifications], ["scan_anomaly"])

    def test_notify_anomaly_off_records_but_never_alerts(self) -> None:
        options = TransitionOptions(base_url=BASE, notify_anomaly=False)
        outcome = transition(state(marks=6), scan_of(state(marks=0)), options=options)
        self.assertEqual(outcome.kind, KIND_ANOMALY_REJECTED)
        self.assertEqual(outcome.notifications, [])
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_RECORD)
        self.assertFalse(outcome.summary["duplicate_suppressed"])

    def test_preview_is_truncated_to_240_characters(self) -> None:
        long_preview = ("abcdefghij" * 40).rstrip()
        outcome = transition(
            state(marks=6),
            scan_of(state(marks=0), sample_marks_preview=long_preview),
        )
        preview = outcome.summary["preview"]
        self.assertEqual(len(preview), 240)
        self.assertTrue(preview.endswith("..."))
        self.assertIn(f"Page preview: {preview}", outcome.notifications[0].message)

    def test_preview_course_defaults_to_marks_summary(self) -> None:
        outcome = transition(
            state(marks=6), scan_of(state(marks=0), sample_marks_preview="empty")
        )
        self.assertEqual(outcome.summary["preview_course"], "MarksSummary")

    def test_partial_scan_records_failed_courses_in_diagnostics(self) -> None:
        outcome = transition(
            state(),
            scan_of(state(), partial=True, failed_courses=["CSC101 — Intro"]),
        )
        self.assertEqual(outcome.kind, KIND_ANOMALY_REJECTED)
        diag = outcome.anomaly_action.diagnostics
        assert diag is not None
        self.assertEqual(diag.failed_courses, ("CSC101 — Intro",))

    def test_anomaly_marker_is_untouched_when_nothing_was_wrong(self) -> None:
        outcome = transition(None, scan_of(state()))
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_NONE)


class RecoveryOrderingTests(unittest.TestCase):
    PRIOR = {
        "key": "Scan returned 0 courses.",
        "reason": "Scan returned 0 courses.",
        "count": 2,
        "first_seen": "2026-11-01T00:00:00+00:00",
    }

    def test_recovery_precedes_the_diff_notifications(self) -> None:
        outcome = transition(OLD_DIFF_STATE, scan_of(NEW_DIFF_STATE), self.PRIOR)
        self.assertEqual(outcome.kind, KIND_DIFF_APPLIED)
        events = [n.event for n in outcome.notifications]
        self.assertEqual(events[0], "scan_recovered")
        self.assertEqual(events[1:], ["marks_updated", "marks_removed", "resource_added",
                                      "resource_removed", "course_removed"])
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_CLEAR)
        recovered = outcome.notifications[0]
        self.assertEqual(recovered.title, "Portal scan recovered")
        self.assertEqual(recovered.priority, "default")
        self.assertEqual(list(recovered.tags), ["white_check_mark"])
        self.assertEqual(recovered.url, f"{BASE}/Courses")
        self.assertEqual(
            recovered.message,
            "Normal portal data returned after 2 rejected scan(s) (Scan returned 0 courses.).\n"
            "Current totals: 1 courses, 2 marks, 1 resources.",
        )
        self.assertEqual(
            recovered.data,
            {"previous_reason": "Scan returned 0 courses.", "rejected_scans": 2,
             "courses": 1, "marks": 2, "resources": 1},
        )

    def test_recovery_precedes_a_fresh_baseline(self) -> None:
        outcome = transition(None, scan_of(state()), self.PRIOR)
        self.assertEqual([n.event for n in outcome.notifications],
                         ["scan_recovered", "baseline_saved"])

    def test_recovery_precedes_a_rollover(self) -> None:
        outcome = transition(
            state(marks=6, semester="Fall 2026"),
            scan_of(state(marks=6, semester="Spring 2027")),
            self.PRIOR,
        )
        self.assertEqual([n.event for n in outcome.notifications],
                         ["scan_recovered", "semester_rollover"])

    def test_recovery_alone_when_the_snapshot_is_unchanged(self) -> None:
        previous = state(marks=2, resources=1)
        outcome = transition(previous, scan_of(copy.deepcopy(previous)), self.PRIOR)
        self.assertEqual([n.event for n in outcome.notifications], ["scan_recovered"])
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_CLEAR)
        # A lone recovery alert is a real event: the "nothing happened" line stays off.
        self.assertIsNone(outcome.summary["message"])
        self.assertTrue(outcome.summary["recovered"])

    def test_notify_anomaly_off_suppresses_recovery_but_still_clears(self) -> None:
        options = TransitionOptions(base_url=BASE, notify_anomaly=False)
        outcome = transition(OLD_DIFF_STATE, scan_of(NEW_DIFF_STATE), self.PRIOR, options)
        self.assertEqual(outcome.notifications[0].event, "marks_updated")
        self.assertNotIn("scan_recovered", [n.event for n in outcome.notifications])
        self.assertEqual(outcome.anomaly_action.kind, ANOMALY_CLEAR)


class OnceOnlyComputationTests(unittest.TestCase):
    """Rollover and totals are each evaluated exactly once per transition."""

    def test_baseline_computes_totals_once_for_the_scan_only(self) -> None:
        scanned = probe_scan(state())
        outcome = compute_transition(None, scanned, None, NOW, OPTIONS)
        self.assertEqual(outcome.kind, KIND_BASELINE_SAVED)
        self.assertEqual(courses_of(scanned.state).totals_computed, 1)

    def test_diff_computes_totals_once_per_snapshot(self) -> None:
        previous = probe_previous(state(marks=2, resources=1))
        scanned = probe_scan(state(marks=3, resources=1))
        outcome = compute_transition(previous, scanned, None, NOW, OPTIONS)
        self.assertEqual(outcome.kind, KIND_DIFF_APPLIED)
        self.assertEqual(courses_of(previous).totals_computed, 1)
        self.assertEqual(courses_of(scanned.state).totals_computed, 1)

    def test_anomaly_computes_totals_once_per_snapshot(self) -> None:
        previous = probe_previous(state(marks=6))
        scanned = probe_scan(state(marks=0))
        outcome = compute_transition(previous, scanned, None, NOW, OPTIONS)
        self.assertEqual(outcome.kind, KIND_ANOMALY_REJECTED)
        self.assertEqual(courses_of(previous).totals_computed, 1)
        self.assertEqual(courses_of(scanned.state).totals_computed, 1)

    def test_rollover_is_evaluated_once(self) -> None:
        previous = probe_previous(state(marks=6, semester="Fall 2026"))
        scanned = probe_scan(state(marks=6, semester="Spring 2027"))
        outcome = compute_transition(previous, scanned, None, NOW, OPTIONS)
        self.assertEqual(outcome.kind, KIND_SEMESTER_ROLLOVER)
        # detect_semester_rollover is the only reader of the baseline's semester key on
        # this path; a second evaluation (the pre-extraction double call) would read it
        # a second time.
        self.assertEqual(previous.reads["semester"], 1)

    def test_rollover_is_evaluated_once_on_the_diff_path_too(self) -> None:
        previous = probe_previous(state(marks=6, semester="Fall 2026"))
        scanned = probe_scan(state(marks=6, semester="Fall 2026"))
        outcome = compute_transition(previous, scanned, None, NOW, OPTIONS)
        self.assertEqual(outcome.kind, KIND_DIFF_APPLIED)
        self.assertEqual(previous.reads["semester"], 1)


class OutcomeContractTests(unittest.TestCase):
    def test_kind_constants_are_exposed(self) -> None:
        self.assertEqual(
            {KIND_BASELINE_SAVED, KIND_SEMESTER_ROLLOVER, KIND_DIFF_APPLIED,
             KIND_ANOMALY_REJECTED},
            set(st.TRANSITION_KINDS),
        )
        self.assertEqual({ANOMALY_NONE, ANOMALY_RECORD, ANOMALY_CLEAR},
                         set(st.ANOMALY_ACTIONS))

    def test_outcomes_are_immutable_value_objects(self) -> None:
        outcome = transition(None, scan_of(state()))
        with self.assertRaises(Exception):
            outcome.kind = "nope"  # type: ignore[misc]
        self.assertIsInstance(outcome.notifications, list)
        self.assertIsInstance(outcome.baseline_totals, tuple)

    def test_unknown_kinds_are_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            st.TransitionOutcome(
                kind="not-a-kind",
                persist_state=False,
                state=None,
                anomaly_action=st.AnomalyAction(),
                notifications=[],
                baseline_totals=(0, 0, 0),
                scanned_totals=(0, 0, 0),
                summary={},
            )
        with self.assertRaises(ValueError):
            st.AnomalyAction(kind="delete-everything")

    def test_transition_options_defaults(self) -> None:
        options = TransitionOptions(base_url=BASE)
        self.assertFalse(options.track_announcements)
        self.assertFalse(options.reset_state)
        self.assertTrue(options.notify_anomaly)

    def test_scan_result_carrier_keeps_its_field_names(self) -> None:
        scan = ScanResult(
            state={"courses": {}},
            partial=True,
            failed_courses=["A"],
            session_expired=True,
            session_expired_error="boom",
            sample_marks_html="<html></html>",
            sample_marks_preview="empty",
            sample_marks_course="C1",
            set_cookie_names=["c"],
        )
        self.assertEqual(
            (scan.state, scan.partial, scan.failed_courses, scan.session_expired,
             scan.session_expired_error, scan.sample_marks_html,
             scan.sample_marks_preview, scan.sample_marks_course, scan.set_cookie_names),
            ({"courses": {}}, True, ["A"], True, "boom", "<html></html>", "empty",
             "C1", ["c"]),
        )
        self.assertEqual(ScanResult(state={}).sample_marks_preview, "")

    def test_cycle_result_carrier_keeps_its_field_names(self) -> None:
        result = st.CycleResult(saved=True, anomaly="x", notifications=[])
        self.assertTrue(result.saved)
        self.assertEqual(result.anomaly, "x")
        self.assertEqual(result.notifications, [])

    def test_inputs_are_not_mutated(self) -> None:
        previous = state(marks=6, semester="Fall 2026")
        scanned = scan_of(state(marks=6))
        before_previous = copy.deepcopy(previous)
        before_scanned = copy.deepcopy(scanned.state)
        compute_transition(previous, scanned, None, NOW, OPTIONS)
        self.assertEqual(previous, before_previous)
        self.assertEqual(scanned.state, before_scanned)


if __name__ == "__main__":
    unittest.main()
