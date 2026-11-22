#!/usr/bin/env python3
"""Behavioral tests for the Runtime Store.

All tests run against real temporary directories: real ``fcntl.flock`` locks, real
``os.replace`` atomic writes, real rolling backup chains. Nothing here mocks the
storage layer itself -- only ``os.replace`` / ``Path.write_text`` are patched to
simulate a failing disk while asserting that no ``*.tmp*`` leftovers survive.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime_store  # noqa: E402
from runtime_store import RuntimeStore, StateLockError, default_heartbeat_path  # noqa: E402

UNSAFE_HTML = (
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
    "</body></html>"
)


def _state(marks: int = 2) -> Dict[str, Any]:
    return {
        "semester": "Fall 2026",
        "courses": {
            "100001": {
                "label": "CSC101 - Intro",
                "marks": {f"m{i}": {"title": f"M{i}"} for i in range(marks)},
                "resources": {"r0": {"title": "Slide 0"}},
            }
        },
    }


def _temp_leftovers(directory: Path) -> List[Path]:
    return sorted(p for p in directory.iterdir() if ".tmp" in p.name)


class RuntimeStorePathTests(unittest.TestCase):
    def test_paths_derive_from_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            self.assertEqual(store.state_path, state_path)
            self.assertEqual(store.heartbeat_path, state_path.with_name("heartbeat.json"))
            self.assertEqual(store.lock_path, state_path.with_name("state.json.lock"))
            self.assertEqual(store.backup_path, state_path.with_name("state.json.bak"))
            self.assertEqual(
                store.session_alert_path, state_path.with_name(".session_alerted")
            )
            self.assertEqual(store.anomaly_alert_path, state_path.with_name(".anomaly_alerted"))
            self.assertEqual(
                store.anomaly_diagnostics_path, state_path.with_name("last_anomaly.json")
            )
            self.assertEqual(
                store.anomaly_marks_path, state_path.with_name("last_anomaly_marks.html")
            )

    def test_explicit_heartbeat_path_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            hb = Path(tmp) / "custom" / "hb.json"
            self.assertEqual(RuntimeStore(state_path, hb).heartbeat_path, hb)
            self.assertEqual(
                default_heartbeat_path(state_path), state_path.with_name("heartbeat.json")
            )
            self.assertEqual(
                default_heartbeat_path(state_path, {"HEARTBEAT_FILE": "/var/run/hb.json"}),
                Path("/var/run/hb.json"),
            )
            self.assertEqual(
                default_heartbeat_path(state_path, {"HEARTBEAT_FILE": "  "}),
                state_path.with_name("heartbeat.json"),
            )

    def test_constructor_does_not_create_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            RuntimeStore(state_path)
            self.assertFalse(state_path.parent.exists())


class RuntimeStoreLockTests(unittest.TestCase):
    def test_lock_creates_parent_dir_and_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            with store.lock(timeout=1.0):
                self.assertTrue(store.lock_path.exists())
            # Lock is released afterwards: a second acquisition succeeds immediately.
            with store.lock(timeout=0.1):
                pass

    def test_lock_contention_times_out_with_state_lock_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = RuntimeStore(state_path)
            holder = threading.Event()
            release = threading.Event()
            acquired = threading.Event()

            def _hold() -> None:
                with store.lock(timeout=5.0):
                    holder.set()
                    release.wait(10.0)

            worker = threading.Thread(target=_hold, daemon=True)
            worker.start()
            try:
                self.assertTrue(holder.wait(5.0))
                started = time.monotonic()
                with self.assertRaises(StateLockError):
                    with store.lock(timeout=0.3):
                        acquired.set()
                elapsed = time.monotonic() - started
                self.assertFalse(acquired.is_set())
                self.assertGreaterEqual(elapsed, 0.25)
                self.assertLess(elapsed, 2.0)
            finally:
                release.set()
                worker.join(timeout=5.0)

            # Once the holder releases, the lock is acquirable again.
            with store.lock(timeout=1.0):
                pass

    def test_lock_is_released_when_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "state.json")
            with self.assertRaises(RuntimeError):
                with store.lock(timeout=1.0):
                    raise RuntimeError("boom")
            with store.lock(timeout=0.2):
                pass


class RuntimeStoreAtomicWriteTests(unittest.TestCase):
    """Every runtime file type must survive a failing disk without temp leftovers."""

    def _assert_no_leftovers(self, directory: Path) -> None:
        self.assertEqual(_temp_leftovers(directory), [])

    def test_atomic_write_creates_parent_dirs_and_leaves_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "deep" / "nested" / "data" / "state.json"
            store = RuntimeStore(state_path)
            store.save_state(_state())
            self.assertEqual(store.load_state(), _state())
            self._assert_no_leftovers(state_path.parent)

    def test_primary_state_write_failure_cleans_temp_and_keeps_old_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            store.save_state(_state(marks=1))
            real_replace = __import__("os").replace

            def _fail_primary(src, dst, *args, **kwargs):
                if Path(dst).name == state_path.name:
                    raise OSError("simulated disk failure")
                return real_replace(src, dst, *args, **kwargs)

            with mock.patch("runtime_store.os.replace", side_effect=_fail_primary):
                with self.assertRaises(OSError):
                    store.save_state(_state(marks=2))

            self._assert_no_leftovers(state_path.parent)
            # The old baseline is still readable and uncorrupted.
            self.assertEqual(store.load_state(), _state(marks=1))

    def test_backup_rotation_write_failure_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            store.save_state(_state(marks=1))
            real_replace = __import__("os").replace

            def _fail_backup(src, dst, *args, **kwargs):
                if Path(dst).name == "state.json.bak":
                    raise OSError("simulated disk failure")
                return real_replace(src, dst, *args, **kwargs)

            with mock.patch("runtime_store.os.replace", side_effect=_fail_backup):
                with self.assertRaises(OSError):
                    store.save_state(_state(marks=2))

            self._assert_no_leftovers(state_path.parent)

    def test_heartbeat_write_failure_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path, Path(tmp) / "data" / "heartbeat.json")
            with mock.patch(
                "runtime_store.os.replace", side_effect=OSError("simulated disk failure")
            ):
                with self.assertRaises(OSError):
                    store.write_heartbeat("ok")
            self._assert_no_leftovers(store.heartbeat_path.parent)
            self.assertFalse(store.heartbeat_path.exists())

    def test_temp_write_failure_cleans_partial_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path, Path(tmp) / "data" / "heartbeat.json")
            real_write_bytes = Path.write_bytes

            def _fail_temp(self, *args, **kwargs):
                if ".tmp." in self.name:
                    # Create a partial file, then die like a full disk would.
                    real_write_bytes(self, b"{partial")
                    raise OSError("simulated full disk")
                return real_write_bytes(self, *args, **kwargs)

            with mock.patch.object(Path, "write_bytes", _fail_temp):
                with self.assertRaises(OSError):
                    store.write_heartbeat("ok")
                with self.assertRaises(OSError):
                    store.save_state(_state())

            self._assert_no_leftovers(store.heartbeat_path.parent)

    def test_keyboard_interrupt_mid_replace_cleans_temp(self) -> None:
        """BaseException (Ctrl-C) must not leak the temp file either."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            with mock.patch(
                "runtime_store.os.replace", side_effect=KeyboardInterrupt("^C")
            ):
                with self.assertRaises(KeyboardInterrupt):
                    store.save_state(_state())
                with self.assertRaises(KeyboardInterrupt):
                    store.write_heartbeat("ok")
            self._assert_no_leftovers(state_path.parent)

    def test_session_marker_write_is_atomic_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            with mock.patch(
                "runtime_store.os.replace", side_effect=OSError("simulated disk failure")
            ):
                # Marker writes are best-effort: failures are swallowed, temps cleaned.
                store.mark_session_alerted("cookie-value")
            self._assert_no_leftovers(state_path.parent)
            self.assertFalse(store.session_alert_path.exists())

    def test_anomaly_marker_write_failure_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            with mock.patch(
                "runtime_store.os.replace", side_effect=OSError("simulated disk failure")
            ):
                store.save_anomaly({"key": "zero_marks", "count": 1})
            self._assert_no_leftovers(state_path.parent)
            self.assertFalse(store.anomaly_alert_path.exists())

    def test_diagnostics_write_failure_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            real_replace = __import__("os").replace

            def _fail_marks_html(src, dst, *args, **kwargs):
                if Path(dst).name == "last_anomaly_marks.html":
                    raise OSError("simulated disk failure")
                return real_replace(src, dst, *args, **kwargs)

            with mock.patch("runtime_store.os.replace", side_effect=_fail_marks_html):
                store.write_anomaly_diagnostics(
                    reason="zero_marks",
                    occurrence=1,
                    scanned_totals={"courses": 1, "marks": 0, "resources": 1},
                    baseline_totals={"courses": 1, "marks": 6, "resources": 1},
                    marks_html=UNSAFE_HTML,
                )
            self._assert_no_leftovers(state_path.parent)
            # The JSON summary was written before the HTML failure.
            self.assertTrue(store.anomaly_diagnostics_path.exists())
            self.assertFalse(store.anomaly_marks_path.exists())

    def test_diagnostics_summary_write_failure_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            real_replace = __import__("os").replace

            def _fail_summary(src, dst, *args, **kwargs):
                if Path(dst).name == "last_anomaly.json":
                    raise OSError("simulated disk failure")
                return real_replace(src, dst, *args, **kwargs)

            with mock.patch("runtime_store.os.replace", side_effect=_fail_summary):
                store.write_anomaly_diagnostics(
                    reason="zero_marks", occurrence=1, marks_html=UNSAFE_HTML
                )
            self._assert_no_leftovers(state_path.parent)
            self.assertFalse(store.anomaly_diagnostics_path.exists())


class RuntimeStoreBackupTests(unittest.TestCase):
    def test_rolling_backups_are_bounded_by_max_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            store.save_state(_state(marks=1), max_backups=3)
            for i in range(2, 6):
                store.save_state(_state(marks=i), max_backups=3)

            self.assertTrue(store.backup_path.exists())
            self.assertTrue(state_path.with_name("state.json.bak.2").exists())
            self.assertTrue(state_path.with_name("state.json.bak.3").exists())
            self.assertFalse(state_path.with_name("state.json.bak.4").exists())

            # Newest -> oldest: .bak holds the previous primary, .bak.2 the one before.
            def _marks(path: Path) -> int:
                return len(json.loads(path.read_text(encoding="utf-8"))["courses"]["100001"]["marks"])

            self.assertEqual(_marks(store.backup_path), 4)
            self.assertEqual(_marks(state_path.with_name("state.json.bak.2")), 3)
            self.assertEqual(_marks(state_path.with_name("state.json.bak.3")), 2)

    def test_backup_paths_ordering_newest_to_oldest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = RuntimeStore(state_path)
            for i in range(1, 5):
                store.save_state(_state(marks=i), max_backups=3)
            self.assertEqual(
                [p.name for p in store.backup_paths()],
                ["state.json.bak", "state.json.bak.2", "state.json.bak.3"],
            )

    def test_max_backups_zero_skips_backups_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = RuntimeStore(state_path)
            store.save_state(_state(marks=1), max_backups=0)
            store.save_state(_state(marks=2), max_backups=0)
            self.assertFalse(store.backup_path.exists())
            self.assertEqual(store.load_state(), _state(marks=2))

    def test_load_state_returns_none_when_primary_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "state.json")
            self.assertIsNone(store.load_state())

    def test_corrupt_recovery_walks_backup_chain_and_skips_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            store = RuntimeStore(state_path)
            s1, s2, s3, s4 = (_state(marks=i) for i in range(1, 5))

            store.save_state(s1, max_backups=3)
            store.save_state(s2, max_backups=3)
            store.save_state(s3, max_backups=3)
            # state.json=s3, .bak=s2, .bak.2=s1

            state_path.write_text("{bad primary", encoding="utf-8")
            store.backup_path.write_text("{bad bak", encoding="utf-8")
            # Corrupt primary and corrupt newest backup -> falls back to .bak.2.
            self.assertEqual(store.load_state(), s1)

            # Restore .bak to s2 while the primary is still corrupt: rotation is skipped,
            # so the healthy .bak chain is never overwritten by corrupt state.
            RuntimeStore(store.backup_path).save_state(s2, max_backups=0)
            store.save_state(s4, max_backups=3)

            self.assertEqual(store.load_state(), s4)
            self.assertEqual(
                json.loads(store.backup_path.read_text(encoding="utf-8")),
                s2,
            )

    def test_corrupt_primary_with_no_healthy_backup_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            store = RuntimeStore(state_path)
            state_path.write_text("{bad json", encoding="utf-8")
            self.assertIsNone(store.load_state())


class RuntimeStoreSessionMarkerTests(unittest.TestCase):
    def test_marker_stores_sha256_not_the_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            store.mark_session_alerted("cookie-abc")
            raw = store.session_alert_path.read_text(encoding="utf-8")
            self.assertEqual(raw, hashlib.sha256(b"cookie-abc").hexdigest())
            self.assertNotIn("cookie-abc", raw)

    def test_marker_dedupes_per_session_and_clears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            self.assertFalse(store.was_session_alerted("cookie-abc"))

            store.mark_session_alerted("cookie-abc")
            self.assertTrue(store.was_session_alerted("cookie-abc"))
            self.assertFalse(store.was_session_alerted("cookie-other"))

            store.mark_session_alerted("cookie-other")
            self.assertTrue(store.was_session_alerted("cookie-other"))
            self.assertFalse(store.was_session_alerted("cookie-abc"))

            store.clear_session_alerted()
            self.assertFalse(store.session_alert_path.exists())
            self.assertFalse(store.was_session_alerted("cookie-other"))
            # Clearing twice is harmless.
            store.clear_session_alerted()

    def test_empty_session_id_never_writes_a_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            store.mark_session_alerted("")
            self.assertFalse(store.session_alert_path.exists())
            self.assertFalse(store.was_session_alerted(""))

    def test_unreadable_marker_is_treated_as_not_alerted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            store.mark_session_alerted("cookie-abc")
            store.session_alert_path.unlink()
            store.session_alert_path.mkdir()
            self.assertFalse(store.was_session_alerted("cookie-abc"))


class RuntimeStoreAnomalyTests(unittest.TestCase):
    def test_anomaly_marker_round_trip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            self.assertIsNone(store.load_anomaly())

            payload = {
                "key": "zero_marks",
                "reason": "zero_marks",
                "first_seen": "2026-11-22T00:00:00+00:00",
                "last_seen": "2026-11-22T00:05:00+00:00",
                "count": 2,
                "scanned": {"courses": 1, "marks": 0, "resources": 1},
            }
            store.save_anomaly(payload)
            self.assertEqual(store.load_anomaly(), payload)

            store.clear_anomaly()
            self.assertFalse(store.anomaly_alert_path.exists())
            self.assertIsNone(store.load_anomaly())
            store.clear_anomaly()

    def test_non_dict_anomaly_marker_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            store.anomaly_alert_path.parent.mkdir(parents=True, exist_ok=True)
            store.anomaly_alert_path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertIsNone(store.load_anomaly())


class RuntimeStoreDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_write_summary_and_scrubbed_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            store.write_anomaly_diagnostics(
                reason="degenerate marks",
                occurrence=3,
                scanned_totals={"courses": 1, "marks": 0, "resources": 1},
                baseline_totals={"courses": 1, "marks": 6, "resources": 2},
                failed_courses=["100002"],
                set_cookie_names=["ASP.NET_SessionId"],
                sample_marks_course="CSC101 - Intro",
                marks_html=UNSAFE_HTML,
            )

            payload = json.loads(store.anomaly_diagnostics_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["reason"], "degenerate marks")
            self.assertEqual(payload["occurrence"], 3)
            self.assertEqual(
                payload["scanned_totals"], {"courses": 1, "marks": 0, "resources": 1}
            )
            self.assertEqual(
                payload["baseline_totals"], {"courses": 1, "marks": 6, "resources": 2}
            )
            self.assertEqual(payload["failed_courses"], ["100002"])
            self.assertEqual(payload["set_cookie_names"], ["ASP.NET_SessionId"])
            self.assertEqual(payload["sample_marks_course"], "CSC101 - Intro")
            self.assertIn("No marks available.", payload["sample_marks_preview"])
            self.assertIn("timestamp", payload)

            html = store.anomaly_marks_path.read_text(encoding="utf-8")
            self.assertNotIn("SUPER_SECRET_PWD", html)
            self.assertNotIn("INNER_SECRET", html)
            self.assertNotIn("synopsisForm", html)
            self.assertIn("No marks available.", html)
            self.assertEqual(_temp_leftovers(store.state_path.parent), [])

    def test_diagnostics_skip_html_when_nothing_to_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStore(Path(tmp) / "data" / "state.json")
            store.write_anomaly_diagnostics(
                reason="partial",
                occurrence=1,
                scanned_totals={"courses": 1, "marks": 2, "resources": 1},
                baseline_totals={"courses": 1, "marks": 6, "resources": 2},
            )
            self.assertTrue(store.anomaly_diagnostics_path.exists())
            self.assertFalse(store.anomaly_marks_path.exists())


class RuntimeStoreHeartbeatTests(unittest.TestCase):
    HEARTBEAT_KEYS = {
        "timestamp",
        "iso",
        "pid",
        "status",
        "keepalive_interval",
        "fetch_interval",
        "healthcheck_max_age",
        "detail",
        "last_ok_timestamp",
    }

    def test_short_form_uses_bound_timing_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "data" / "heartbeat.json"
            store = RuntimeStore(
                Path(tmp) / "data" / "state.json",
                hb,
                keepalive_interval=45,
                fetch_interval=90,
                healthcheck_max_age=300,
            )
            store.write_heartbeat("ok", detail="2 courses", last_ok=1700000000.0)

            payload = json.loads(hb.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), self.HEARTBEAT_KEYS)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["detail"], "2 courses")
            self.assertEqual(payload["keepalive_interval"], 45)
            self.assertEqual(payload["fetch_interval"], 90)
            self.assertEqual(payload["healthcheck_max_age"], 300)
            self.assertEqual(payload["last_ok_timestamp"], 1700000000.0)
            self.assertEqual(payload["pid"], __import__("os").getpid())
            self.assertIsInstance(payload["timestamp"], float)

    def test_bind_heartbeat_refreshes_timing_and_keeps_unspecified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb)
            store.write_heartbeat("starting")
            payload = json.loads(hb.read_text(encoding="utf-8"))
            self.assertEqual(payload["keepalive_interval"], 120)
            self.assertEqual(payload["fetch_interval"], 480)
            self.assertEqual(payload["healthcheck_max_age"], 0)
            self.assertEqual(payload["detail"], "")
            self.assertIsNone(payload["last_ok_timestamp"])

            store.bind_heartbeat(keepalive_interval=15, fetch_interval=30, healthcheck_max_age=60)
            store.write_heartbeat("ok")
            payload = json.loads(hb.read_text(encoding="utf-8"))
            self.assertEqual(payload["keepalive_interval"], 15)
            self.assertEqual(payload["fetch_interval"], 30)
            self.assertEqual(payload["healthcheck_max_age"], 60)

            # Reload without a max-age override leaves the previous value in place.
            store.bind_heartbeat(keepalive_interval=20, fetch_interval=40)
            store.write_heartbeat("ok")
            payload = json.loads(hb.read_text(encoding="utf-8"))
            self.assertEqual(payload["keepalive_interval"], 20)
            self.assertEqual(payload["fetch_interval"], 40)
            self.assertEqual(payload["healthcheck_max_age"], 60)

    def test_legacy_keyword_wrapper_still_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb, keepalive_interval=45)
            store.write_heartbeat(
                "ok",
                keepalive_interval=60,
                fetch_interval=480,
                healthcheck_max_age=30,
                detail="override",
                last_ok_ts=1234.5,
            )
            payload = json.loads(hb.read_text(encoding="utf-8"))
            self.assertEqual(payload["keepalive_interval"], 60)
            self.assertEqual(payload["fetch_interval"], 480)
            self.assertEqual(payload["healthcheck_max_age"], 30)
            self.assertEqual(payload["detail"], "override")
            self.assertEqual(payload["last_ok_timestamp"], 1234.5)

            # The bound value is untouched by a one-off override.
            store.write_heartbeat("ok")
            payload = json.loads(hb.read_text(encoding="utf-8"))
            self.assertEqual(payload["keepalive_interval"], 45)


class RuntimeStoreHealthcheckTests(unittest.TestCase):
    def test_missing_heartbeat_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb)
            ok, msg = store.check_healthcheck()
            self.assertFalse(ok)
            self.assertIn("Heartbeat file missing", msg)

    def test_fresh_ok_heartbeat_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb, keepalive_interval=60)
            store.write_heartbeat("ok", detail="1 courses")
            ok, msg = store.check_healthcheck()
            self.assertTrue(ok, msg)
            self.assertIn("status=ok", msg)

    def test_unhealthy_statuses_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb, keepalive_interval=600)
            for status in ("waiting_for_session", "exiting_session_expired", "wedged_exiting"):
                with self.subTest(status=status):
                    store.write_heartbeat(status, detail="because")
                    ok, msg = store.check_healthcheck()
                    self.assertFalse(ok)
                    self.assertIn(status, msg)
                    self.assertIn("because", msg)

    def test_anomaly_and_timeout_statuses_stay_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb, keepalive_interval=600)
            for status in ("anomaly", "timeout", "portal_error", "scanning"):
                with self.subTest(status=status):
                    store.write_heartbeat(status)
                    ok, msg = store.check_healthcheck()
                    self.assertTrue(ok, msg)

    def test_stale_heartbeat_fails_and_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb, keepalive_interval=600)
            store.write_heartbeat("ok")
            payload = json.loads(hb.read_text(encoding="utf-8"))
            payload["timestamp"] = time.time() - 500
            hb.write_text(json.dumps(payload), encoding="utf-8")

            ok, msg = store.check_healthcheck(max_age_override=30)
            self.assertFalse(ok)
            self.assertIn("stale", msg)

            # Without an override, max(KEEPALIVE_INTERVAL, 60) = 600 keeps it fresh.
            ok, _ = store.check_healthcheck()
            self.assertTrue(ok)

    def test_healthcheck_max_age_bound_overrides_keepalive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(
                Path(tmp) / "state.json", hb, keepalive_interval=600, healthcheck_max_age=60
            )
            store.write_heartbeat("ok")
            payload = json.loads(hb.read_text(encoding="utf-8"))
            payload["timestamp"] = time.time() - 120
            hb.write_text(json.dumps(payload), encoding="utf-8")
            ok, msg = store.check_healthcheck()
            self.assertFalse(ok)
            self.assertIn("stale", msg)

    def test_unreadable_or_invalid_payloads_fail_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "heartbeat.json"
            store = RuntimeStore(Path(tmp) / "state.json", hb)
            hb.write_text("{not json", encoding="utf-8")
            ok, msg = store.check_healthcheck()
            self.assertFalse(ok)
            self.assertIn("unreadable", msg)

            hb.write_text('{"no_timestamp": 1}', encoding="utf-8")
            ok, msg = store.check_healthcheck()
            self.assertFalse(ok)
            self.assertIn("invalid", msg)

            hb.write_text('{"timestamp": "not-a-number"}', encoding="utf-8")
            ok, msg = store.check_healthcheck()
            self.assertFalse(ok)
            self.assertIn("timestamp invalid", msg)

            hb.write_text('"a string"', encoding="utf-8")
            ok, msg = store.check_healthcheck()
            self.assertFalse(ok)
            self.assertIn("invalid", msg)


class RuntimeStoreImportSurfaceTests(unittest.TestCase):
    def test_module_exports_single_writer_and_lock_error(self) -> None:
        self.assertTrue(hasattr(runtime_store, "_atomic_write_text"))
        self.assertTrue(issubclass(StateLockError, RuntimeError))
        self.assertTrue(callable(runtime_store.default_heartbeat_path))


if __name__ == "__main__":
    unittest.main()
