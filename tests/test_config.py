#!/usr/bin/env python3
"""Offline unit tests for the leaf Config module.

Covers `.env` byte decoding, text parsing, session normalization, env-vs-file
precedence with provenance, secret-free diagnostics, the single canonical
``NOTIFIER_TIMEOUT`` rule, single-read file watching, notifier-config change
detection, and prober config loading.

Every test injects a plain ``environ`` mapping and/or a real temporary ``.env``
file; nothing here mutates or patches ``os.environ``.
"""

from __future__ import annotations

import io
import os
import signal
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

import config  # noqa: E402
import probe  # noqa: E402
import tracker  # noqa: E402
from config import (  # noqa: E402
    EnvWatcher,
    normalize_session_id,
    notifier_config_key,
    parse_env_file,
    parse_env_text,
    parse_notifier_timeout,
    resolve_env_file,
    resolve_heartbeat_file,
    resolve_state_file,
)
from notifiers import BaseNotifier, build_notifiers  # noqa: E402
from sis_client import Course, CourseSnapshotResult, RosterResult  # noqa: E402

NO_ENV: Dict[str, str] = {}


class _RecordingNotifier(BaseNotifier):
    name = "recording"

    def __init__(self) -> None:
        self.notes: List[Any] = []

    def send(self, note) -> None:  # type: ignore[override]
        self.notes.append(note)


class EnvDecodingTests(unittest.TestCase):
    """Byte decoding and text parsing for every supported .env flavour."""

    def test_bom_and_encoding_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            utf8_bom = tmpdir / "utf8_bom.env"
            utf8_bom.write_bytes(b"\xef\xbb\xbfSIS_SESSION_ID=bom_sid_1\n")
            self.assertEqual(
                config.load_config(utf8_bom, environ=NO_ENV).session_id, "bom_sid_1"
            )

            bom_comment = tmpdir / "bom_comment.env"
            bom_comment.write_bytes(b"\xef\xbb\xbf# comment with = sign\nSIS_SESSION_ID=bom_sid_2\n")
            self.assertEqual(
                config.load_config(bom_comment, environ=NO_ENV).session_id, "bom_sid_2"
            )

            utf16le = tmpdir / "utf16le.env"
            utf16le.write_bytes(b"\xff\xfe" + "SIS_SESSION_ID=u16le_sid\n".encode("utf-16-le"))
            self.assertEqual(
                config.load_config(utf16le, environ=NO_ENV).session_id, "u16le_sid"
            )

            utf16be = tmpdir / "utf16be.env"
            utf16be.write_bytes(b"\xfe\xff" + "SIS_SESSION_ID=u16be_sid\n".encode("utf-16-be"))
            self.assertEqual(
                config.load_config(utf16be, environ=NO_ENV).session_id, "u16be_sid"
            )

            utf32le = tmpdir / "utf32le.env"
            utf32le.write_bytes(b"\xff\xfe\x00\x00" + "SIS_SESSION_ID=u32le_sid\n".encode("utf-32-le"))
            self.assertEqual(
                config.load_config(utf32le, environ=NO_ENV).session_id, "u32le_sid"
            )

            utf32be = tmpdir / "utf32be.env"
            utf32be.write_bytes(b"\x00\x00\xfe\xff" + "SIS_SESSION_ID=u32be_sid\n".encode("utf-32-be"))
            self.assertEqual(
                config.load_config(utf32be, environ=NO_ENV).session_id, "u32be_sid"
            )

            cp1252 = tmpdir / "cp1252.env"
            cp1252.write_bytes(b"# COMSATS \x97 Portal\nSIS_SESSION_ID=cp1252_sid\n")
            self.assertEqual(
                config.load_config(cp1252, environ=NO_ENV).session_id, "cp1252_sid"
            )

    def test_export_prefix_quotes_escapes_and_inline_comments(self) -> None:
        parsed = parse_env_text(
            "export SIS_SESSION_ID=abc\n"
            "EXPORT_FLAG=1\n"
            'export   SPACED="abc"\n'
            "export\tTABBED='abc' # note\n"
        )
        self.assertEqual(parsed["SIS_SESSION_ID"], "abc")
        self.assertEqual(parsed["EXPORT_FLAG"], "1")
        self.assertEqual(parsed["SPACED"], "abc")
        self.assertEqual(parsed["TABBED"], "abc")

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
            (r'K="line\nbreak"', "line\nbreak"),
            (r'K="tab\there"', "tab\there"),
        ]
        for line, expected in quoted_cases:
            self.assertEqual(parse_env_text(line)["K"], expected, msg=f"Failed for {line!r}")

        self.assertEqual(parse_env_text("SIS_SESSION_ID=# comment\n")["SIS_SESSION_ID"], "")
        self.assertEqual(parse_env_text("SIS_SESSION_ID= # comment\n")["SIS_SESSION_ID"], "")
        # '#' without preceding whitespace is part of the value
        self.assertEqual(
            parse_env_text("WEBHOOK_URL=https://example.com/hook#frag\n")["WEBHOOK_URL"],
            "https://example.com/hook#frag",
        )
        self.assertEqual(
            parse_env_text("NTFY_TOKEN=tok#123 # real comment\n")["NTFY_TOKEN"], "tok#123"
        )

    def test_duplicate_keys_last_assignment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "SIS_SESSION_ID=first\nSIS_SESSION_ID=second\nNTFY_URL=https://ntfy.sh/t\nNTFY_URL=\n",
                encoding="utf-8",
            )
            values, _ = config.load_env_with_sources(env_path, environ=NO_ENV)
            self.assertEqual(values["SIS_SESSION_ID"], "second")
            self.assertEqual(values["NTFY_URL"], "")

    def test_missing_and_directory_env_files_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.env"
            self.assertEqual(parse_env_file(missing), {})

            directory = Path(tmp) / "env_dir"
            directory.mkdir()
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(parse_env_file(directory), {})
            self.assertIn("directory", out.getvalue())


class SessionAndPrecedenceTests(unittest.TestCase):
    """SIS_SESSION_ID normalization, precedence, provenance, and diagnostics."""

    def test_session_normalization_and_placeholder_rejection(self) -> None:
        self.assertEqual(normalize_session_id("  abc  "), "abc")
        self.assertEqual(normalize_session_id("ASP.NET_SessionId=abc"), "abc")
        self.assertEqual(normalize_session_id("asp.net_sessionid=abc; Path=/; HttpOnly"), "abc")
        self.assertEqual(normalize_session_id("Cookie: ASP.NET_SessionId=abc"), "abc")
        self.assertEqual(normalize_session_id("ASP.NET_SessionId="), "")
        self.assertEqual(normalize_session_id(""), "")
        self.assertEqual(normalize_session_id(config.SESSION_PLACEHOLDER), "")
        self.assertEqual(normalize_session_id('"abc"'), "abc")
        # multiline pasted Cookie header
        pasted = "Cookie: a=1; ASP.NET_SessionId=cookie_value; b=2"
        self.assertEqual(normalize_session_id(pasted), "cookie_value")

        parsed = parse_env_text(
            'SIS_SESSION_ID="paste_your_ASP_NET_SessionId_here" # placeholder\n'
        )
        self.assertEqual(normalize_session_id(parsed["SIS_SESSION_ID"]), "")

    def test_env_vs_file_precedence_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=disk_cookie_1\n", encoding="utf-8")

            values, sources = config.load_env_with_sources(
                env_path, environ={"SIS_SESSION_ID": "stale_process_env"}
            )
            self.assertEqual(values["SIS_SESSION_ID"], "disk_cookie_1")
            self.assertIn("overrides process_env", sources["SIS_SESSION_ID"])

            # Blank .env value falls back to the process environment.
            env_path.write_text("SIS_SESSION_ID=\n", encoding="utf-8")
            values2, sources2 = config.load_env_with_sources(
                env_path, environ={"SIS_SESSION_ID": "stale_process_env"}
            )
            self.assertEqual(values2["SIS_SESSION_ID"], "stale_process_env")
            self.assertEqual(sources2["SIS_SESSION_ID"], "process_env")

            # Process-only key (absent from the file) is reported as process_env.
            values3, sources3 = config.load_env_with_sources(
                env_path, environ={"KEEPALIVE_INTERVAL": "60"}
            )
            self.assertEqual(values3["KEEPALIVE_INTERVAL"], "60")
            self.assertEqual(sources3["KEEPALIVE_INTERVAL"], "process_env")

            # Neither source: key absent, no provenance entry.
            values4, sources4 = config.load_env_with_sources(
                env_path, environ={}
            )
            self.assertNotIn("NTFY_URL", values4)
            self.assertNotIn("NTFY_URL", sources4)

    def test_typed_config_from_injected_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "KEEPALIVE_INTERVAL=60 # 1 min\n"
                "TRACK_ANNOUNCEMENTS=true # on\n"
                "REQUEST_DELAY=0.25\n",
                encoding="utf-8",
            )
            cfg = config.load_config(
                env_path,
                environ={
                    "SIS_SESSION_ID": "env_cookie",
                    "FETCH_INTERVAL": "120",
                    "SIS_SEMESTER": "Fall 2026",
                },
            )
            self.assertEqual(cfg.session_id, "env_cookie")
            self.assertEqual(cfg.keepalive_interval, 60)
            self.assertEqual(cfg.fetch_interval, 120)
            self.assertEqual(cfg.request_delay, 0.25)
            self.assertTrue(cfg.track_announcements)
            self.assertEqual(cfg.semester, "Fall 2026")
            self.assertEqual(cfg.sources["SIS_SESSION_ID"], "process_env")

            # Minimums are clamped.
            clamp_env = Path(tmp) / "clamp.env"
            clamp_env.write_text("SIS_SESSION_ID=s\n", encoding="utf-8")
            clamped = config.load_config(
                clamp_env,
                require_session=False,
                environ={"KEEPALIVE_INTERVAL": "1", "FETCH_INTERVAL": "1"},
            )
            self.assertEqual(clamped.keepalive_interval, 15)
            self.assertEqual(clamped.fetch_interval, 30)

    def test_missing_session_diagnostics_are_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "SIS_SESSIONID=super_secret_val\nNTFY_TOKEN=secret_token\n",
                encoding="utf-8",
            )
            with self.assertRaises(SystemExit) as ctx:
                config.load_config(env_path, require_session=True, environ={})
            message = str(ctx.exception)
            self.assertIn("SIS_SESSIONID", message)
            self.assertIn("NTFY_TOKEN", message)
            self.assertIn("key absent", message)
            self.assertNotIn("super_secret_val", message)
            self.assertNotIn("secret_token", message)

            # Placeholder session is reported as such, without echoing it.
            placeholder_env = Path(tmp) / "placeholder.env"
            placeholder_env.write_text(
                "SIS_SESSION_ID=paste_your_ASP_NET_SessionId_here\n", encoding="utf-8"
            )
            with self.assertRaises(SystemExit) as ctx2:
                config.load_config(placeholder_env, require_session=True, environ={})
            message2 = str(ctx2.exception)
            self.assertIn("example placeholder", message2)

            # Missing file and directory paths both produce safe diagnostics.
            missing_diag = config.describe_missing_session_context(
                Path(tmp) / "nope.env", {}
            )
            self.assertIn("does not exist", missing_diag)

            directory = Path(tmp) / "env_dir"
            directory.mkdir()
            with self.assertRaises(SystemExit) as ctx3:
                config.load_config(directory, require_session=True, environ={})
            self.assertIn("directory", str(ctx3.exception))

            # require_session=False never raises.
            cfg = config.load_config(directory, require_session=False, environ={})
            self.assertEqual(cfg.session_id, "")


class NotifierTimeoutTests(unittest.TestCase):
    """The one canonical NOTIFIER_TIMEOUT rule, verified through notifier construction."""

    def test_canonical_default_and_minimum(self) -> None:
        self.assertEqual(config.NOTIFIER_TIMEOUT_DEFAULT, 20.0)
        self.assertEqual(config.NOTIFIER_TIMEOUT_MINIMUM, 1.0)

        self.assertEqual(parse_notifier_timeout({}), 20.0)
        self.assertEqual(parse_notifier_timeout({"NOTIFIER_TIMEOUT": ""}), 20.0)
        self.assertEqual(parse_notifier_timeout({"NOTIFIER_TIMEOUT": "not-a-number"}), 20.0)
        self.assertEqual(parse_notifier_timeout({"NOTIFIER_TIMEOUT": "45"}), 45.0)
        self.assertEqual(parse_notifier_timeout({"NOTIFIER_TIMEOUT": "0.1"}), 1.0)
        self.assertEqual(parse_notifier_timeout({"NOTIFIER_TIMEOUT": "-5"}), 1.0)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=s\n", encoding="utf-8")
            self.assertEqual(config.load_config(env_path, environ={}).notifier_timeout, 20.0)

            env_path.write_text("SIS_SESSION_ID=s\nNOTIFIER_TIMEOUT=0.2\n", encoding="utf-8")
            self.assertEqual(config.load_config(env_path, environ={}).notifier_timeout, 1.0)

    def test_notifier_instances_receive_the_unified_timeout(self) -> None:
        base = {"NTFY_URL": "https://ntfy.sh/topic"}
        default_notifiers = build_notifiers(dict(base))
        self.assertEqual(default_notifiers[1].timeout, 20.0)

        clamped = build_notifiers(dict(base, NOTIFIER_TIMEOUT="0.5"))
        self.assertEqual(clamped[1].timeout, 1.0)

        explicit = build_notifiers(dict(base, NOTIFIER_TIMEOUT="0.5"), timeout=7.5)
        self.assertEqual(explicit[1].timeout, 7.5)

        # No duplicate parser survives in the Notifiers module.
        import notifiers as notifiers_module

        self.assertFalse(hasattr(notifiers_module, "_parse_notifier_timeout"))


class WatcherTests(unittest.TestCase):
    """Single-read watching across in-place edits and atomic inode swaps."""

    def test_watcher_reuses_one_read_per_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=first\n", encoding="utf-8")

            real_read_bytes = Path.read_bytes
            reads: List[Path] = []

            def _counting_read_bytes(self) -> bytes:  # type: ignore[no-untyped-def]
                reads.append(self)
                return real_read_bytes(self)

            with mock.patch.object(
                Path, "read_bytes", autospec=True, side_effect=_counting_read_bytes
            ):
                watcher = EnvWatcher(env_path, environ={})
                reads.clear()

                reload = watcher.check(environ={})
                self.assertEqual(
                    len([p for p in reads if Path(p) == env_path]), 1,
                    msg=f"check() read the env file {len(reads)} times",
                )
                self.assertFalse(reload.changed)
                self.assertEqual(reload.description, "")
                self.assertEqual(reload.config.session_id, "first")

                # In-place edit: still exactly one read, change detected + parsed.
                env_path.write_text("SIS_SESSION_ID=second\n", encoding="utf-8")
                reads.clear()
                reload2 = watcher.check(environ={})
                self.assertEqual(len([p for p in reads if Path(p) == env_path]), 1)
                self.assertTrue(reload2.changed)
                self.assertIn("content changed", reload2.description)
                self.assertEqual(reload2.config.session_id, "second")

                # Atomic inode replacement with identical bytes.
                tmp_env = Path(tmp) / ".env.tmp"
                tmp_env.write_text("SIS_SESSION_ID=second\n", encoding="utf-8")
                os.replace(tmp_env, env_path)
                reads.clear()
                reload3 = watcher.check(environ={})
                self.assertEqual(len([p for p in reads if Path(p) == env_path]), 1)
                self.assertTrue(reload3.changed)
                self.assertIn("inode", reload3.description)
                self.assertIn("content unchanged", reload3.description)

    def test_check_changed_in_place_and_inode_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=cookie_same\n", encoding="utf-8")
            watcher = EnvWatcher(env_path, environ={})
            self.assertEqual(watcher.check_changed(), (False, ""))

            tmp_env = Path(tmp) / ".env.tmp"
            tmp_env.write_text("SIS_SESSION_ID=cookie_same\n", encoding="utf-8")
            os.replace(tmp_env, env_path)
            changed, desc = watcher.check_changed()
            self.assertTrue(changed)
            self.assertIn("inode", desc)
            self.assertIn("content unchanged", desc)

            env_path.write_text("SIS_SESSION_ID=cookie_new\n", encoding="utf-8")
            changed2, desc2 = watcher.check_changed()
            self.assertTrue(changed2)
            self.assertIn("content changed", desc2)
            self.assertEqual(
                config.load_config(env_path, environ={}).session_id, "cookie_new"
            )

    def test_watcher_reports_creation_and_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            watcher = EnvWatcher(env_path, environ={})
            self.assertFalse(watcher.check_changed()[0])

            env_path.write_text("SIS_SESSION_ID=created\n", encoding="utf-8")
            changed, desc = watcher.check_changed()
            self.assertTrue(changed)
            self.assertIn("created", desc)

            env_path.unlink()
            changed2, desc2 = watcher.check_changed()
            self.assertTrue(changed2)
            self.assertIn("deleted", desc2)
            reload = watcher.check(environ={})
            self.assertFalse(reload.config.session_id)

    def test_watcher_reload_uses_injected_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("SIS_SESSION_ID=\n", encoding="utf-8")
            watcher = EnvWatcher(env_path, environ={"SIS_SESSION_ID": "from_process_env"})
            reload = watcher.check()
            self.assertEqual(reload.config.session_id, "from_process_env")
            self.assertEqual(
                reload.config.sources["SIS_SESSION_ID"], "process_env"
            )


class PathResolutionTests(unittest.TestCase):
    """ENV_FILE / STATE_FILE / HEARTBEAT_FILE resolution against an injected mapping."""

    def test_path_resolution_prefers_injected_environ(self) -> None:
        root = Path("/srv/app")
        env = {
            "ENV_FILE": "/etc/comsats/custom.env",
            "STATE_FILE": "/var/lib/comsats/state.json",
            "HEARTBEAT_FILE": "/var/lib/comsats/hb.json",
        }
        self.assertEqual(resolve_env_file(root, env), Path("/etc/comsats/custom.env"))
        self.assertEqual(resolve_state_file(root, env), Path("/var/lib/comsats/state.json"))
        self.assertEqual(
            resolve_heartbeat_file(Path("/var/lib/comsats/state.json"), env),
            Path("/var/lib/comsats/hb.json"),
        )

    def test_path_resolution_falls_back_to_defaults(self) -> None:
        root = Path("/srv/app")
        self.assertEqual(resolve_env_file(root, {}), root / ".env")
        self.assertEqual(resolve_state_file(root, {}), root / "state.json")
        self.assertEqual(
            resolve_heartbeat_file(root / "data" / "state.json", {}),
            root / "data" / "heartbeat.json",
        )
        # Blank values count as unset.
        self.assertEqual(resolve_env_file(root, {"ENV_FILE": "   "}), root / ".env")


class NotifierConfigKeyTests(unittest.TestCase):
    """Notifier-relevant settings change detection."""

    def test_key_ignores_unrelated_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "SIS_SESSION_ID=a\nNTFY_URL=https://ntfy.sh/t\nNOTIFIER_TIMEOUT=25\n",
                encoding="utf-8",
            )
            cfg = config.load_config(env_path, environ={})
            original = cfg.notifier_key

            env_path.write_text(
                "SIS_SESSION_ID=b\nNTFY_URL=https://ntfy.sh/t\nNOTIFIER_TIMEOUT=25\n"
                "KEEPALIVE_INTERVAL=15\nFETCH_INTERVAL=30\n",
                encoding="utf-8",
            )
            cfg2 = config.load_config(env_path, environ={})
            self.assertEqual(cfg2.notifier_key, original)
            self.assertEqual(
                notifier_config_key(cfg2.raw), notifier_config_key(cfg.raw)
            )

            env_path.write_text(
                "SIS_SESSION_ID=b\nNTFY_URL=https://ntfy.sh/other\nNOTIFIER_TIMEOUT=25\n",
                encoding="utf-8",
            )
            cfg3 = config.load_config(env_path, environ={})
            self.assertNotEqual(cfg3.notifier_key, original)

            env_path.write_text(
                "SIS_SESSION_ID=b\nNTFY_URL=https://ntfy.sh/t\nNOTIFIER_TIMEOUT=30\n",
                encoding="utf-8",
            )
            cfg4 = config.load_config(env_path, environ={})
            self.assertNotEqual(cfg4.notifier_key, original)

    def test_key_normalizes_timeout_and_ignores_token_whitespace(self) -> None:
        a = notifier_config_key({"NTFY_URL": " https://ntfy.sh/t ", "NOTIFIER_TIMEOUT": "0.5"})
        b = notifier_config_key({"NTFY_URL": "https://ntfy.sh/t", "NOTIFIER_TIMEOUT": ""})
        self.assertEqual(a[1][:2], ("NTFY_URL", "https://ntfy.sh/t"))
        self.assertEqual(a[0], ("NOTIFIER_TIMEOUT", 1.0))
        self.assertEqual(b[0], ("NOTIFIER_TIMEOUT", 20.0))


class DaemonNotifierRebuildTests(unittest.TestCase):
    """run_daemon rebuilds notifier instances only when notifier settings change."""

    def test_rebuild_only_on_notifier_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_path.write_text("SIS_SESSION_ID=valid_cookie\n", encoding="utf-8")

            courses = [Course(course_id="100001", code="CSC101", name="Intro")]
            ticks = 0
            tick_lock = threading.Lock()
            rebuilds: List[Any] = []
            notifier_changed = threading.Event()

            class _FakeClient:
                """Interface-only daemon stub: this test is about *notifier rebuild timing*."""

                def __init__(self, *args, **kwargs) -> None:
                    self.cancel_event = None
                    self.session_id = kwargs.get("session_id", "")
                    self.base_url = kwargs.get("base_url", tracker.DEFAULT_BASE_URL)
                    self.user_agent = kwargs.get("user_agent", "")
                    self.request_delay = 0.0

                def fetch_roster(self):
                    nonlocal ticks
                    with tick_lock:
                        ticks += 1
                        current = ticks
                    if current >= 3:
                        notifier_changed.set()
                    return RosterResult(courses=list(courses))

                def fetch_course_snapshot(self, course, include_announcements=False):
                    return CourseSnapshotResult()

            real_load_config = config.load_config

            def _fast_load_config(env_file, require_session=True, **kwargs):
                cfg = real_load_config(
                    env_file, require_session=require_session, **kwargs
                )
                cfg.keepalive_interval = 1
                return cfg

            def _counting_build_notifiers(cfg_raw, timeout=None):
                rebuilds.append(timeout)
                return [_RecordingNotifier()]

            def _driver() -> None:
                # Wait for at least 3 idle ticks with no notifier-relevant change.
                self.assertTrue(notifier_changed.wait(timeout=8.0))
                env_path.write_text(
                    "SIS_SESSION_ID=valid_cookie\nNTFY_URL=https://ntfy.sh/new-topic\n",
                    encoding="utf-8",
                )
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and len(rebuilds) < 2:
                    time.sleep(0.05)
                time.sleep(0.2)
                signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_driver, daemon=True)
            t.start()
            try:
                with mock.patch.object(tracker, "SisClient", _FakeClient), mock.patch.object(
                    tracker, "load_config", side_effect=_fast_load_config
                ), mock.patch.object(
                    tracker, "build_notifiers", side_effect=_counting_build_notifiers
                ):
                    rc = tracker.run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=5.0)

            self.assertEqual(rc, 0)
            self.assertGreaterEqual(ticks, 3)
            # One build at startup, exactly one more after the notifier change.
            self.assertEqual(len(rebuilds), 2, msg=f"rebuild timeouts={rebuilds}")
            self.assertEqual(rebuilds[0], 20.0)


class ProbeConfigTests(unittest.TestCase):
    """The prober loads configuration through the leaf module."""

    def test_probe_load_config_uses_injected_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            env_path.write_bytes(
                b'\xef\xbb\xbfexport SIS_SESSION_ID="expired_cookie" # initial\n'
            )
            sid, user_agent = probe.load_config(env_path, environ={})
            self.assertEqual(sid, "expired_cookie")
            self.assertEqual(user_agent, probe.USER_AGENT)

            sid2, _ = probe.load_config(env_path, environ={"SIS_SESSION_ID": "env_sid"})
            self.assertEqual(sid2, "expired_cookie")  # file wins over process env

    def test_probe_main_reads_env_file_from_injected_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "custom.env"
            env_path.write_text(
                "SIS_SESSION_ID=Cookie: ASP.NET_SessionId=probe_cookie_from_env_file\n",
                encoding="utf-8",
            )
            with mock.patch.object(sys, "argv", ["probe.py"]), mock.patch.object(
                probe, "run_probe", return_value=0
            ) as mock_run:
                rc = probe.main(environ={"ENV_FILE": str(env_path), "SIS_SESSION_ID": ""})

            self.assertEqual(rc, 0)
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[0][0], "probe_cookie_from_env_file")

    def test_probe_module_does_not_import_tracker(self) -> None:
        source = (ROOT / "probe.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("from tracker") or stripped.startswith("import tracker"),
                msg=f"probe.py still imports tracker: {line!r}",
            )
        self.assertFalse(hasattr(probe, "load_env"))


if __name__ == "__main__":
    unittest.main()
