#!/usr/bin/env python3
"""End-to-end HTTP tests for the session-renewal web page.

Every test drives the *real* `ThreadingHTTPServer` on `127.0.0.1:0` with real
temporary `.env` and `heartbeat.json` files, wired to the existing
`tests.support_portal_pages.SimulatedPortal` transport seam. Nothing here mocks
a private helper: assertions are made on HTTP statuses, response bodies, the
bytes on disk, and `config.EnvWatcher.check()`.
"""

from __future__ import annotations

import contextlib
import html
import http.client
import io
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import config  # noqa: E402
import session_ui  # noqa: E402
from sis_client import Course  # noqa: E402
from support_portal_pages import SimulatedPortal  # noqa: E402

SECRET_SESSION = "stored_session_sentinel_do_not_render"
SECRET_TOKEN = "ntfy_token_sentinel_do_not_render"

#: Marker telling the harness to fetch a real per-form token before submitting.
_TOKEN_AUTO = object()

#: The hidden per-form token a browser would echo back on submit.
CSRF_INPUT_RE = re.compile(
    r'<input[^>]*name="%s"[^>]*value="([^"]*)"'
    % re.escape(session_ui.CSRF_FIELD),
    re.IGNORECASE,
)

ENV_TEXT = (
    "# Tracker configuration — keep this comment\n"
    "\n"
    f"SIS_SESSION_ID={SECRET_SESSION}\n"
    "SIS_BASE_URL=https://sis.example.test\n"
    "\n"
    "# notifier settings\n"
    f"NTFY_TOKEN={SECRET_TOKEN}\n"
    "NTFY_URL=https://ntfy.sh/topic\n"
    "KEEPALIVE_INTERVAL=120\n"
)

COURSES = (
    Course(course_id="100001", code="CSC101", name="Intro to Computing"),
    Course(course_id="100002", code="CSC102", name="Programming"),
    Course(course_id="100003", code="CSC103", name="Data Structures"),
)


class _ServerTestCase(unittest.TestCase):
    """One real server + real temp files per test."""

    courses = COURSES
    env_text: Optional[str] = ENV_TEXT
    #: Extra entries merged into the app's injected environment (never os.environ).
    extra_environ: Optional[Dict[str, str]] = None

    def setUp(self) -> None:
        self.log_stream = io.StringIO()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.env_path = self.tmpdir / ".env"
        if self.env_text is not None:
            self.env_path.write_text(self.env_text, encoding="utf-8")
        self.heartbeat_path = self.tmpdir / "heartbeat.json"
        self.portal = self.make_portal()
        self.server, self.thread = self.start_server(self.portal)

    # -- wiring ---------------------------------------------------------------
    def make_portal(self) -> SimulatedPortal:
        return SimulatedPortal(courses=self.courses)

    def app_environ(self) -> Dict[str, str]:
        environ = {
            "ENV_FILE": str(self.env_path),
            "SIS_BASE_URL": "https://sis.example.test",
        }
        environ.update(self.extra_environ or {})
        return environ

    @property
    def no_env(self) -> Dict[str, str]:
        """An environment whose `.env` path does not exist, so defaults are real.

        `load_settings` now consults `.env`, so an empty mapping would silently
        read whatever file happens to sit at the repo root.
        """
        return {"ENV_FILE": str(self.tmpdir / "absent.env")}

    def build_app(self, portal) -> "session_ui.SessionUiApp":
        return session_ui.build_app(
            self.env_path,
            self.heartbeat_path,
            transport=portal,
            environ=self.app_environ(),
        )

    def start_server(self, portal) -> Tuple["session_ui.ThreadingHTTPServer", threading.Thread]:
        app = self.build_app(portal)
        self.app = app
        server = session_ui.create_server(app, port=0, log_stream=self.log_stream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop, server, thread)
        return server, thread

    @staticmethod
    def _stop(server, thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    # -- HTTP -----------------------------------------------------------------
    def http(
        self,
        method: str,
        path: str = "/",
        data: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str]:
        payload = None
        sent = {"Accept": "text/html"}
        if data is not None:
            payload = urllib.parse.urlencode(data).encode("utf-8")
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        sent.update(headers or {})
        request = urllib.request.Request(
            self.base_url + path, data=payload, headers=sent, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10.0) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read().decode("utf-8", errors="replace")
            finally:
                error.close()

    def get(
        self, path: str = "/", headers: Optional[Dict[str, str]] = None
    ) -> Tuple[int, str]:
        return self.http("GET", path, headers=headers)

    @property
    def host_header(self) -> str:
        """The authority a browser would send as ``Host`` for this server."""
        host, port = self.server.server_address[:2]
        return f"{host}:{port}"

    def same_origin_headers(self) -> Dict[str, str]:
        """The headers a real browser sends when submitting this page's own form."""
        return {
            "Origin": f"http://{self.host_header}",
            "Sec-Fetch-Site": "same-origin",
            "Host": self.host_header,
        }

    def csrf_token(self, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        """Fetch the form like a browser does and read its hidden token."""
        _, body = self.get(headers=headers)
        match = CSRF_INPUT_RE.search(body)
        return match.group(1) if match else None

    def wait_for_log(self, fragment: str, timeout: float = 5.0) -> str:
        """Wait until ``fragment`` appears in the request log, then return it.

        The server writes its one log line after flushing the response body, so a
        client can return slightly before the line lands; this removes that race
        without patching anything.
        """
        deadline = time.monotonic() + timeout
        logged = self.log_stream.getvalue()
        while fragment not in logged and time.monotonic() < deadline:
            time.sleep(0.01)
            logged = self.log_stream.getvalue()
        return logged

    def post(
        self,
        value: str,
        headers: Optional[Dict[str, str]] = None,
        token: object = _TOKEN_AUTO,
    ) -> Tuple[int, str]:
        """Submit the form the way the rendered page would, token included."""
        data = {session_ui.SESSION_FIELD: value}
        if headers is None:
            headers = self.same_origin_headers()
        if token is _TOKEN_AUTO:
            token = self.csrf_token(headers=headers)
        if isinstance(token, str):
            data[session_ui.CSRF_FIELD] = token
        return self.http("POST", data=data, headers=headers)

    # -- heartbeat ------------------------------------------------------------
    def write_heartbeat(self, **overrides) -> None:
        payload = {
            "timestamp": time.time(),
            "iso": "2026-11-23T10:00:00+00:00",
            "pid": 4321,
            "status": "ok",
            "detail": "3 courses tracked",
            "last_ok_timestamp": time.time() - 30,
            "keepalive_interval": 120,
            "fetch_interval": 480,
            "healthcheck_max_age": 0,
        }
        payload.update(overrides)
        self.heartbeat_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def env_bytes(self) -> bytes:
        return self.env_path.read_bytes()


class MalformedPostTests(_ServerTestCase):
    """A hostile `POST` body must not become an unhandled exception.

    Both cases here are reachable with a single forged `Sec-Fetch-Site:
    same-origin` header -- no browser needed -- and spec #17 requires a missing
    or invalid token to be a plain `403` with no portal call and no `.env`
    write. A dropped connection or a traceback is neither.
    """

    def raw_post(
        self, body: bytes, headers: Optional[Dict[str, str]] = None
    ) -> Tuple[int, str]:
        """Send an exact byte body, so the form can be as malformed as we like."""
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(conn.close)
        sent = {"Content-Type": "application/x-www-form-urlencoded"}
        sent.update(self.same_origin_headers())
        sent.update(headers or {})
        conn.request("POST", "/", body=body, headers=sent)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")

    def test_a_body_with_more_fields_than_the_parser_allows_is_a_plain_403(self) -> None:
        before = self.env_bytes()
        body = urllib.parse.urlencode(
            {f"field{i}": "value" for i in range(33)}
        ).encode("utf-8")

        status, text = self.raw_post(body)

        self.assertEqual(status, 403)
        self.assertIn("forbidden", text.lower())
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_a_non_ascii_token_is_a_plain_403(self) -> None:
        """`hmac.compare_digest` raises TypeError on non-ASCII `str` input."""
        before = self.env_bytes()
        body = urllib.parse.urlencode(
            {session_ui.SESSION_FIELD: "non_ascii_cookie", session_ui.CSRF_FIELD: "tökén"}
        ).encode("utf-8")

        status, text = self.raw_post(body)

        self.assertEqual(status, 403)
        self.assertIn("forbidden", text.lower())
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_a_malformed_paste_is_rejected_cleanly_by_the_route_too(self) -> None:
        """The same defensiveness must hold on the path that parses a real form."""
        token = self.csrf_token() or ""
        body = urllib.parse.urlencode(
            {session_ui.SESSION_FIELD: "x", session_ui.CSRF_FIELD: token, **{
                "pad%d" % i: "y" for i in range(31)
            }}
        ).encode("utf-8")

        status, text = self.raw_post(body)

        # Either a 403 from the gate or a 400 from the route is fine; a dropped
        # connection and a traceback are not.
        self.assertIn(status, (400, 403), text[:200])
        self.assertNotIn("Traceback", text)

    def test_the_connection_survives_a_refused_post(self) -> None:
        body = urllib.parse.urlencode({f"field{i}": "value" for i in range(33)}).encode()
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(conn.close)
        headers = self.same_origin_headers()

        conn.request("POST", "/", body=body, headers=headers)
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.status, 403)

        conn.request("GET", "/", headers=headers)
        follow_up = conn.getresponse()
        try:
            self.assertEqual(follow_up.status, 200)
        finally:
            follow_up.close()


class OversizedBodyTests(_ServerTestCase):
    """A body over `MAX_REQUEST_BODY` must not desync an HTTP/1.1 connection.

    HTTP/1.1 is keep-alive by default and this server always sets
    `protocol_version = "HTTP/1.1"`. Reading only the first 8 KB of a 9 KB body
    leaves 1 KB in the socket, and the next request on that connection is parsed
    from the leftover bytes (originally reproduced as a 501).
    """

    def connect(self) -> "http.client.HTTPConnection":
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(conn.close)
        return conn

    def test_an_oversized_body_is_refused_and_the_next_request_parses(self) -> None:
        conn = self.connect()
        body = b"field=" + b"a" * (session_ui.MAX_REQUEST_BODY + 1024)
        headers = dict(self.same_origin_headers())
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        conn.request("POST", "/", body=body, headers=headers)
        refused = conn.getresponse()
        refused_body = refused.read()

        self.assertEqual(refused.status, 413)
        self.assertTrue(refused_body)

        conn.request("GET", "/", headers=self.same_origin_headers())
        follow_up = conn.getresponse()
        try:
            self.assertEqual(follow_up.status, 200)
            self.assertIn(b"Tracker status", follow_up.read())
        finally:
            follow_up.close()

    def test_an_absurd_declared_length_closes_instead_of_draining(self) -> None:
        """Draining is bounded: past the cap the server hangs up, never reads on."""
        conn = self.connect()
        headers = dict(self.same_origin_headers())
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(session_ui.MAX_REQUEST_BODY * 1000)

        conn.putrequest("POST", "/")
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        conn.send(b"x" * 1024)

        response = conn.getresponse()
        self.assertEqual(response.status, 413)
        self.assertIn("close", (response.getheader("Connection") or "").lower())


class StatusPageTests(_ServerTestCase):
    """`GET /` renders read-only tracker status and no secrets."""

    def test_renders_status_detail_and_last_healthy_time(self) -> None:
        self.write_heartbeat()

        status, body = self.get()

        self.assertEqual(status, 200)
        lowered = body.lower()
        self.assertIn("status", lowered)
        self.assertIn("ok", lowered)
        self.assertIn("3 courses tracked", body)
        self.assertIn("2026-11-23", body)  # last healthy time, from last_ok_timestamp

    def test_missing_heartbeat_is_reported_cleanly(self) -> None:
        self.assertFalse(self.heartbeat_path.exists())

        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertIn("no heartbeat", body.lower())

    def test_corrupt_heartbeat_is_reported_cleanly(self) -> None:
        self.heartbeat_path.write_text("{not json", encoding="utf-8")

        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertIn("unreadable", body.lower())

    def test_heartbeat_that_is_not_an_object_is_reported_cleanly(self) -> None:
        self.heartbeat_path.write_text("[1, 2, 3]", encoding="utf-8")

        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertIn("unreadable", body.lower())

    def test_stale_heartbeat_is_flagged(self) -> None:
        self.write_heartbeat(timestamp=time.time() - 3600, last_ok_timestamp=None)

        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertIn("stale", body.lower())
        self.assertIn("never", body.lower())  # no healthy timestamp recorded

    def test_waiting_for_session_status_is_flagged_as_needing_a_cookie(self) -> None:
        self.write_heartbeat(status="waiting_for_session", detail="session expired")

        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertIn("waiting_for_session", body)
        self.assertIn("session expired", body)
        self.assertIn("cookie", body.lower())

    def test_never_renders_stored_session_or_notifier_token(self) -> None:
        self.write_heartbeat()

        status, body = self.get()

        self.assertEqual(status, 200)
        self.assertNotIn(SECRET_SESSION, body)
        self.assertNotIn(SECRET_TOKEN, body)

    def test_unknown_path_is_404(self) -> None:
        status, _ = self.get("/nope")

        self.assertEqual(status, 404)

    def test_get_is_read_only_and_never_writes_env(self) -> None:
        before = self.env_bytes()

        self.get()

        self.assertEqual(self.env_bytes(), before)


class SaveSessionTests(_ServerTestCase):
    """`POST /` verifies through the portal seam, then rewrites only one line."""

    def test_raw_value_is_saved_and_only_the_session_line_changes(self) -> None:
        watcher = config.EnvWatcher(self.env_path, environ={})
        self.assertFalse(watcher.check(environ={}).changed)

        status, body = self.post("fresh_session_value")

        self.assertEqual(status, 200)
        self.assertIn("saved", body.lower())
        self.assertIn("3", body)  # course count
        expected = ENV_TEXT.replace(
            f"SIS_SESSION_ID={SECRET_SESSION}", "SIS_SESSION_ID=fresh_session_value"
        )
        self.assertEqual(self.env_path.read_text(encoding="utf-8"), expected)

        reload = watcher.check(environ={})
        self.assertTrue(reload.changed)
        self.assertEqual(reload.config.session_id, "fresh_session_value")

    def test_asp_net_session_id_pair_is_accepted(self) -> None:
        status, body = self.post("ASP.NET_SessionId=pair_value")

        self.assertEqual(status, 200)
        self.assertIn("saved", body.lower())
        self.assertIn("SIS_SESSION_ID=pair_value\n", self.env_path.read_text(encoding="utf-8"))

    def test_full_cookie_header_is_accepted(self) -> None:
        status, body = self.post(
            "Cookie: ASP.NET_SessionId=header_value; ARRAffinity=node1; other=1"
        )

        self.assertEqual(status, 200)
        self.assertIn("saved", body.lower())
        self.assertIn("SIS_SESSION_ID=header_value\n", self.env_path.read_text(encoding="utf-8"))

    def test_saved_response_never_echoes_the_submitted_cookie(self) -> None:
        status, body = self.post("never_echoed_cookie_value")

        self.assertEqual(status, 200)
        self.assertNotIn("never_echoed_cookie_value", body)
        self.assertNotIn(SECRET_SESSION, body)

    def test_missing_env_file_is_created_owner_only(self) -> None:
        self.env_path.unlink()

        status, body = self.post("created_fresh")

        self.assertEqual(status, 200)
        self.assertIn("saved", body.lower())
        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"), "SIS_SESSION_ID=created_fresh\n"
        )
        self.assertEqual(self.env_path.stat().st_mode & 0o777, 0o600)


class RejectedSessionTests(_ServerTestCase):
    """Every rejection leaves `.env` byte-for-byte unchanged."""

    def assert_env_unchanged(self, before: bytes) -> None:
        self.assertEqual(self.env_bytes(), before)

    def test_blank_input_is_rejected_without_touching_the_file(self) -> None:
        before = self.env_bytes()

        status, body = self.post("   ")

        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertIn("cookie", body.lower())
        self.assert_env_unchanged(before)
        self.assertEqual(self.portal.request_count, 0)

    def test_placeholder_input_is_rejected_without_touching_the_file(self) -> None:
        before = self.env_bytes()

        status, body = self.post(config.SESSION_PLACEHOLDER)

        self.assertEqual(status, 400)
        self.assertIn("placeholder", body.lower())
        self.assert_env_unchanged(before)
        self.assertEqual(self.portal.request_count, 0)

    def test_expired_session_from_portal_is_rejected(self) -> None:
        before = self.env_bytes()
        self.portal.expire_sessions = {"rejected_cookie"}

        status, body = self.post("rejected_cookie")

        self.assertEqual(status, 400)
        self.assertIn("expired", body.lower())
        self.assertIn("unchanged", body.lower())
        self.assert_env_unchanged(before)
        self.assertNotIn("rejected_cookie", body)

    def test_portal_http_error_is_rejected_with_502(self) -> None:
        from sis_client import PortalHttpError

        before = self.env_bytes()
        self.portal.failures = {"/Courses": PortalHttpError("HTTP 503 fetching /Courses")}

        status, body = self.post("some_cookie")

        self.assertEqual(status, 502)
        self.assertIn("unchanged", body.lower())
        self.assert_env_unchanged(before)
        self.assertNotIn("some_cookie", body)

    def test_transport_timeout_is_rejected_without_touching_the_file(self) -> None:
        before = self.env_bytes()
        self.portal.failures = {"/Courses": TimeoutError("timed out")}

        status, body = self.post("timeout_cookie")

        self.assertEqual(status, 502)
        self.assertIn("unchanged", body.lower())
        self.assert_env_unchanged(before)
        self.assertNotIn("timeout_cookie", body)


class LogHygieneTests(_ServerTestCase):
    """Server output never contains a submitted cookie value."""

    def test_no_cookie_value_in_the_request_log(self) -> None:
        self.write_heartbeat()

        self.get()
        self.post("log_leak_candidate_value")
        self.post("")

        logged = self.log_stream.getvalue()
        self.assertNotIn("log_leak_candidate_value", logged)
        self.assertNotIn(SECRET_SESSION, logged)
        self.assertNotIn(SECRET_TOKEN, logged)
        # The request still has to be visible for operations, without secrets.
        self.assertIn("POST / -> 200", logged)
        self.assertIn("GET / -> 200", logged)

    def test_cookie_in_a_hand_crafted_query_string_is_not_logged(self) -> None:
        self.get("/?session_id=query_leak_candidate")

        logged = self.log_stream.getvalue()
        self.assertNotIn("query_leak_candidate", logged)
        self.assertIn("GET / -> 200", logged)

    def test_no_cookie_value_on_stdout_or_stderr(self) -> None:
        captured_out = io.StringIO()
        captured_err = io.StringIO()

        with contextlib.redirect_stdout(captured_out), contextlib.redirect_stderr(captured_err):
            status, _ = self.post("stderr_leak_candidate")

        self.assertEqual(status, 200)
        combined = captured_out.getvalue() + captured_err.getvalue()
        self.assertNotIn("stderr_leak_candidate", combined)
        self.assertNotIn(SECRET_SESSION, combined)


class RequestGateSeamTests(_ServerTestCase):
    """Every request passes one gate, so a future check can be added in one place.

    Ticket #17 adds the Tailscale-user and CSRF checks; this test proves the seam
    it will use is real: an override of ``guard_request`` blocks `GET` and `POST`
    alike before any status render or `.env` write happens.
    """

    def build_app(self, portal) -> "session_ui.SessionUiApp":
        base = super().build_app(portal)

        class _GatedApp(session_ui.SessionUiApp):
            def guard_request(self, request):
                return session_ui.Response(status=403, body="<p>blocked by gate</p>")

        gated = _GatedApp(
            base.env_path,
            base.heartbeat_path,
            transport=base.transport,
            environ=base.environ,
            portal_timeout=base.portal_timeout,
        )
        return gated

    def test_gate_blocks_get_and_post_before_any_side_effect(self) -> None:
        self.write_heartbeat()
        before = self.env_bytes()

        get_status, get_body = self.get()
        post_status, post_body = self.post("gated_cookie")

        self.assertEqual(get_status, 403)
        self.assertEqual(post_status, 403)
        self.assertIn("blocked by gate", get_body)
        self.assertNotIn("3 courses tracked", get_body)  # status never rendered
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)  # portal never contacted


class TailscaleUserGateTests(_ServerTestCase):
    """`SESSION_UI_ALLOWED_USER` gates every request on `Tailscale-User-Login`."""

    extra_environ = {"SESSION_UI_ALLOWED_USER": "ahmed@example.com"}

    def test_allowed_header_gets_the_status_page(self) -> None:
        self.write_heartbeat()

        status, body = self.get(headers={"Tailscale-User-Login": "ahmed@example.com"})

        self.assertEqual(status, 200)
        self.assertIn("3 courses tracked", body)

    def test_missing_header_is_forbidden_without_side_effects(self) -> None:
        self.write_heartbeat()
        before = self.env_bytes()

        get_status, get_body = self.get()
        post_status, post_body = self.post("allowed_cookie")

        self.assertEqual(get_status, 403)
        self.assertEqual(post_status, 403)
        self.assertIn("forbidden", get_body.lower())
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_mismatched_header_is_forbidden_without_side_effects(self) -> None:
        before = self.env_bytes()

        get_status, _ = self.get(headers={"Tailscale-User-Login": "someone@example.com"})
        post_status, body = self.post(
            "allowed_cookie", headers={"Tailscale-User-Login": "someone@example.com"}
        )

        self.assertEqual(get_status, 403)
        self.assertEqual(post_status, 403)
        self.assertIn("forbidden", body.lower())
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_case_insensitive_exact_match_succeeds(self) -> None:
        status, _ = self.get(headers={"Tailscale-User-Login": "Ahmed@Example.COM"})

        self.assertEqual(status, 200)

    def test_allowlist_entry_case_does_not_matter(self) -> None:
        app = session_ui.build_app(
            self.env_path,
            self.heartbeat_path,
            transport=self.portal,
            environ={"SESSION_UI_ALLOWED_USER": "AHMED@example.com"},
        )

        self.assertIsNone(
            app.guard_request(
                session_ui.Request(
                    method="GET", path="/", headers={"Tailscale-User-Login": "ahmed@EXAMPLE.com"}
                )
            )
        )

    def test_prefix_and_suffix_logins_do_not_match(self) -> None:
        """Lowercasing must not turn the comparison into a substring match."""
        for impostor in (
            "evil-ahmed@example.com",
            "ahmed@example.com.evil.test",
            "ahmed@example.co",
            "ahmed@example.com,ahmed2@example.com",
            "ahmed",
        ):
            with self.subTest(login=impostor):
                status, _ = self.get(headers={"Tailscale-User-Login": impostor})
                self.assertEqual(status, 403)

    def test_padded_login_is_not_trimmed_into_a_match(self) -> None:
        """An HTTP client strips OWS, so assert the comparison itself is strict."""
        app = session_ui.build_app(
            self.env_path,
            self.heartbeat_path,
            transport=self.portal,
            environ={"SESSION_UI_ALLOWED_USER": "ahmed@example.com"},
        )

        for padded in (" ahmed@example.com", "ahmed@example.com ", "\tahmed@example.com"):
            with self.subTest(login=padded):
                blocked = app.guard_request(
                    session_ui.Request(
                        method="GET", path="/", headers={"Tailscale-User-Login": padded}
                    )
                )
                self.assertIsNotNone(blocked)
                self.assertEqual(blocked.status, 403)

    def test_header_name_is_case_insensitive(self) -> None:
        status, _ = self.get(headers={"tailscale-user-login": "ahmed@example.com"})

        self.assertEqual(status, 200)

    def test_unset_setting_leaves_the_page_open(self) -> None:
        app = session_ui.build_app(
            self.env_path, self.heartbeat_path, transport=self.portal, environ={}
        )

        self.assertIsNone(
            app.guard_request(session_ui.Request(method="GET", path="/", headers={}))
        )

    def test_blank_setting_is_treated_as_unset(self) -> None:
        app = session_ui.build_app(
            self.env_path,
            self.heartbeat_path,
            transport=self.portal,
            environ={"SESSION_UI_ALLOWED_USER": "   "},
        )

        self.assertIsNone(
            app.guard_request(session_ui.Request(method="GET", path="/", headers={}))
        )

    def test_denied_response_never_leaks_the_expected_login(self) -> None:
        status, body = self.get(headers={"Tailscale-User-Login": "someone@example.com"})

        self.assertEqual(status, 403)
        self.assertNotIn("ahmed@example.com", body)
        self.assertNotIn("someone@example.com", body)
        logged = self.wait_for_log("GET / -> 403")
        self.assertNotIn("ahmed@example.com", logged)
        self.assertNotIn("someone@example.com", logged)


class CrossSitePostTests(_ServerTestCase):
    """`POST /` rejects cross-site submissions and needs a per-form token."""

    def test_same_origin_browser_submission_succeeds(self) -> None:
        status, body = self.post("same_origin_cookie", headers=self.same_origin_headers())

        self.assertEqual(status, 200)
        self.assertIn("saved", body.lower())
        self.assertIn("SIS_SESSION_ID=same_origin_cookie\n", self.env_path.read_text("utf-8"))

    def test_foreign_origin_is_forbidden_without_side_effects(self) -> None:
        before = self.env_bytes()

        status, body = self.post(
            "cross_site_cookie",
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Host": self.host_header,
            },
        )

        self.assertEqual(status, 403)
        self.assertIn("forbidden", body.lower())
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_cross_site_fetch_site_is_forbidden(self) -> None:
        for value in ("cross-site", "same-site"):
            with self.subTest(sec_fetch_site=value):
                before = self.env_bytes()
                status, body = self.post(
                    "cross_site_cookie",
                    headers={
                        "Origin": f"http://{self.host_header}",
                        "Sec-Fetch-Site": value,
                        "Host": self.host_header,
                    },
                )
                self.assertEqual(status, 403)
                self.assertIn("forbidden", body.lower())
                self.assertEqual(self.env_bytes(), before)
                self.assertEqual(self.portal.request_count, 0)

    def test_missing_token_is_forbidden(self) -> None:
        before = self.env_bytes()

        status, body = self.post(
            "tokenless_cookie", headers=self.same_origin_headers(), token=None
        )

        self.assertEqual(status, 403)
        self.assertIn("forbidden", body.lower())
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_invalid_token_is_forbidden(self) -> None:
        before = self.env_bytes()

        status, _ = self.post(
            "forged_cookie", headers=self.same_origin_headers(), token="forged-token"
        )

        self.assertEqual(status, 403)
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_token_from_a_previous_visit_still_works(self) -> None:
        """The token is the app's, not one-shot: a stale tab must not 403 unfairly."""
        first = self.csrf_token()

        status, _ = self.post(
            "second_visit_cookie", headers=self.same_origin_headers(), token=first
        )

        self.assertEqual(status, 200)

    def test_bare_post_with_no_origin_and_no_fetch_site_is_forbidden(self) -> None:
        """Absent headers are untrusted, not implicitly same-origin."""
        before = self.env_bytes()

        status, body = self.post("bare_cookie", headers={"Host": self.host_header})

        self.assertEqual(status, 403)
        self.assertIn("forbidden", body.lower())
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_direct_navigation_origin_absent_with_token_succeeds(self) -> None:
        """`Sec-Fetch-Site: none` + a real token is a genuine browser form post."""
        status, _ = self.post(
            "direct_nav_cookie",
            headers={"Sec-Fetch-Site": "none", "Host": self.host_header},
        )

        self.assertEqual(status, 200)

    def test_form_renders_the_hidden_token_the_post_expects(self) -> None:
        _, body = self.get()

        token = self.csrf_token()
        self.assertIsNotNone(token)
        self.assertGreaterEqual(len(token or ""), 16)
        self.assertIn(f'name="{session_ui.CSRF_FIELD}"', body)
        self.assertIn('type="hidden"', body)

    def test_token_is_html_escaped_when_rendered(self) -> None:
        """A token is escaped on the way into the attribute, not just "safe today".

        `secrets.token_urlsafe` can only emit `[A-Za-z0-9_-]`, so asserting the
        rendered token holds no `"` passes whether or not `html.escape` is
        called -- a tautology. This drives an adversarial token through the real
        render path instead, so removing the escape breaks it: the raw token
        contains a `"`, the attribute value would end early, and the unescape
        round-trip below would no longer reproduce the token.
        """
        token = 'quote"and<angle>&amp'
        self.app.csrf_token = token

        _, body = self.get()

        match = CSRF_INPUT_RE.search(body)
        self.assertIsNotNone(match, "the hidden token input vanished")
        self.assertNotIn(token, body, "the raw token reached the markup")
        self.assertEqual(html.unescape(match.group(1)), token)

    def test_cross_site_rejection_is_logged_without_the_token(self) -> None:
        token = self.csrf_token() or ""

        self.post(
            "logged_cookie",
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Host": self.host_header,
            },
        )

        logged = self.wait_for_log("POST / -> 403")
        self.assertIn("POST / -> 403", logged)
        self.assertNotIn("logged_cookie", logged)
        if token:
            self.assertNotIn(token, logged)


    def test_cross_site_path_is_gated_before_routing(self) -> None:
        """The gate precedes routing, so even an unknown path needs the token."""
        for path in ("/nope", "/index.html"):
            with self.subTest(path=path):
                status, _ = self.http(
                    "POST", path, data={session_ui.SESSION_FIELD: "x"}, headers={
                        "Origin": "https://evil.example",
                        "Sec-Fetch-Site": "cross-site",
                        "Host": self.host_header,
                    }
                )
                self.assertEqual(status, 403)

    def test_get_needs_no_token_even_from_a_cross_site_context(self) -> None:
        """`GET /` is read-only, so the CSRF rule is POST-only by design."""
        status, body = self.get(
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Host": self.host_header,
            }
        )

        self.assertEqual(status, 200)
        self.assertNotIn(SECRET_SESSION, body)

    def test_each_process_gets_its_own_token(self) -> None:
        other = session_ui.build_app(
            self.env_path, self.heartbeat_path, transport=self.portal, environ={}
        )

        self.assertNotEqual(other.csrf_token, self.app.csrf_token)

    def test_forbidden_body_carries_no_token_and_no_login(self) -> None:
        token = self.csrf_token() or ""

        status, body = self.post(
            "quiet_cookie",
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Host": self.host_header,
            },
        )

        self.assertEqual(status, 403)
        if token:
            self.assertNotIn(token, body)
        self.assertNotIn("quiet_cookie", body)

    def test_proxied_https_origin_matching_host_is_accepted(self) -> None:
        """Behind `tailscale serve` the browser sends https:// while we are http://.

        The Origin/Host comparison must therefore ignore the scheme, or every real
        submission through the proxy would be refused.
        """
        status, _ = self.post(
            "proxied_cookie",
            headers={
                "Origin": f"https://{self.host_header}",
                "Sec-Fetch-Site": "same-origin",
                "Host": self.host_header,
            },
        )

        self.assertEqual(status, 200)

    def test_null_origin_is_refused(self) -> None:
        """`Origin: null` is an opaque origin (sandboxed frame, file://) — never us."""
        status, _ = self.post(
            "null_origin_cookie",
            headers={
                "Origin": "null",
                "Sec-Fetch-Site": "same-origin",
                "Host": self.host_header,
            },
        )

        self.assertEqual(status, 403)

    def test_origin_with_userinfo_does_not_match(self) -> None:
        status, _ = self.post(
            "userinfo_cookie",
            headers={
                "Origin": f"https://evil.example@{self.host_header}",
                "Sec-Fetch-Site": "same-origin",
                "Host": self.host_header,
            },
        )

        self.assertEqual(status, 403)

    def test_origin_scheme_only_difference_is_allowed_but_host_must_match(self) -> None:
        """A different host with the same scheme is still cross-site."""
        status, _ = self.post(
            "other_host_cookie",
            headers={
                "Origin": "https://elsewhere.example",
                "Sec-Fetch-Site": "same-origin",
                "Host": self.host_header,
            },
        )

        self.assertEqual(status, 403)


class GateOrderTests(_ServerTestCase):
    """The user check runs first, so a wrong login never reaches the CSRF path."""

    extra_environ = {"SESSION_UI_ALLOWED_USER": "ahmed@example.com"}

    def test_wrong_user_with_a_valid_token_is_still_forbidden(self) -> None:
        before = self.env_bytes()
        _, body = self.get(headers={"Tailscale-User-Login": "ahmed@example.com"})
        token = CSRF_INPUT_RE.search(body)
        self.assertIsNotNone(token)

        status, _ = self.post(
            "gated_cookie",
            headers={
                "Tailscale-User-Login": "someone@example.com",
                "Origin": f"http://{self.host_header}",
                "Sec-Fetch-Site": "same-origin",
                "Host": self.host_header,
            },
            token=token.group(1) if token else None,
        )

        self.assertEqual(status, 403)
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)

    def test_allowed_user_with_valid_token_and_origin_succeeds(self) -> None:
        status, body = self.post(
            "fully_valid_cookie",
            headers={
                "Tailscale-User-Login": "ahmed@example.com",
                "Origin": f"http://{self.host_header}",
                "Sec-Fetch-Site": "same-origin",
                "Host": self.host_header,
            },
        )

        self.assertEqual(status, 200)
        self.assertIn("saved", body.lower())


class BindAddressTests(_ServerTestCase):
    """Startup binds loopback only unless explicitly opted in."""

    def test_defaults_to_loopback(self) -> None:
        settings = session_ui.load_settings(environ=self.no_env)

        self.assertEqual(settings.host, "127.0.0.1")

    def test_refuses_non_loopback_bind_without_opt_in(self) -> None:
        app = self.build_app(self.portal)

        with self.assertRaises(session_ui.BindRefusedError):
            session_ui.create_server(app, host="0.0.0.0", port=0, environ=self.no_env)

        with self.assertRaises(session_ui.BindRefusedError):
            session_ui.create_server(app, host="192.168.1.10", port=0, environ=self.no_env)

    def test_allows_non_loopback_bind_with_explicit_opt_in(self) -> None:
        self.assertEqual(
            session_ui.resolve_bind_host("0.0.0.0", environ=self.no_env, allow_non_loopback=True),
            "0.0.0.0",
        )
        # The opt-in may also come from the file, which is why this goes through
        # settings rather than a raw environment key.
        opt_in_path = self.tmpdir / "opt-in.env"
        opt_in_path.write_text("SESSION_UI_ALLOW_NON_LOOPBACK=1\n", encoding="utf-8")
        environ = {"ENV_FILE": str(opt_in_path)}
        self.assertEqual(session_ui.resolve_bind_host("0.0.0.0", environ=environ), "0.0.0.0")
        self.assertEqual(
            session_ui.resolve_bind_host(
                "0.0.0.0", environ={"SESSION_UI_ALLOW_NON_LOOPBACK": "yes"}
            ),
            "0.0.0.0",
        )
        # Opt-in must be explicit; "0"/blank are not.
        for falsy in ("", "0", "false", "no"):
            with self.assertRaises(session_ui.BindRefusedError):
                session_ui.resolve_bind_host(
                    "0.0.0.0",
                    environ={"SESSION_UI_ALLOW_NON_LOOPBACK": falsy},
                    allow_non_loopback=False,
                )

    def test_loopback_variants_are_accepted(self) -> None:
        for host in ("127.0.0.1", "127.0.0.53", "::1", "localhost"):
            self.assertEqual(session_ui.resolve_bind_host(host, environ=self.no_env), host)

    def test_main_refuses_non_loopback_and_exits_nonzero(self) -> None:
        env = {
            "ENV_FILE": str(self.env_path),
            "HEARTBEAT_FILE": str(self.heartbeat_path),
            "SESSION_UI_HOST": "0.0.0.0",
            "SESSION_UI_PORT": "0",
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            rc = session_ui.main([], environ=env)

        self.assertNotEqual(rc, 0)
        self.assertIn("loopback", stderr.getvalue().lower())

    def test_port_from_settings(self) -> None:
        settings = session_ui.load_settings(
            environ={"SESSION_UI_PORT": "9123", "ENV_FILE": str(self.tmpdir / "absent.env")}
        )

        self.assertEqual(settings.port, 9123)
        self.assertEqual(session_ui.load_settings(environ=self.no_env).port, 8765)


class MethodRoutingTests(_ServerTestCase):
    """The routing policy in `handle()` and the HTTP method surface.

    `handle()` is the documented single request surface, so its 405 branch is
    exercised directly: over HTTP an unhandled method never reaches it (the
    base class answers 501 for a `do_*` that does not exist), but the app-level
    policy still has to be right and tested.

    `HEAD` is served: a status page is exactly what an uptime probe polls, and
    a 501 from the base class is a poor answer. It must return the GET headers
    with no body.
    """

    def test_unsupported_methods_get_a_405_from_the_app(self) -> None:
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                response = self.app.handle(
                    session_ui.Request(method=method, path="/", headers={})
                )

                self.assertEqual(response.status, 405)
                self.assertIn("Method not allowed", response.body)

    def test_unsupported_methods_are_501_over_http(self) -> None:
        """The base class answers first; documenting 501 keeps the two in step."""
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(conn.close)

        for method in ("PUT", "DELETE"):
            with self.subTest(method=method):
                conn.request(method, "/")
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 501)
                self.assertNotIn(
                    "Traceback", self.log_stream.getvalue()
                )

    def test_head_returns_the_get_headers_without_a_body(self) -> None:
        self.write_heartbeat()
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(conn.close)

        conn.request("HEAD", "/")
        response = conn.getresponse()
        body = response.read()

        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(response.getheader("Content-Length") or 0), 0)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_head_of_an_unknown_path_is_a_bodyless_404(self) -> None:
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        self.addCleanup(conn.close)

        conn.request("HEAD", "/nope")
        response = conn.getresponse()

        self.assertEqual(response.status, 404)
        self.assertEqual(response.read(), b"")


class SettingsFromEnvFileTests(unittest.TestCase):
    """The `.env` file is a real settings source, not just the process environment.

    `.env.example`, the README and `docker-compose.yml` all tell an operator to
    put `SESSION_UI_ALLOWED_USER` in `.env`, and a bare `python3 session_ui.py`
    run has no process environment to read it from -- so the file has to be
    consulted, with the process environment still winning where both are set.
    These tests inject `ENV_FILE` and never touch `os.environ`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.env_path = self.tmpdir / ".env"

    def _write_env(self, text: str, encoding: str = "utf-8") -> None:
        self.env_path.write_bytes(text.encode(encoding))

    def _settings(self, **extra: str) -> "session_ui.SessionUiSettings":
        environ = {"ENV_FILE": str(self.env_path)}
        environ.update(extra)
        return session_ui.load_settings(environ=environ)

    def test_allowed_user_is_read_from_the_env_file_alone(self) -> None:
        self._write_env(
            "SIS_SESSION_ID=stored_cookie\n"
            "SESSION_UI_ALLOWED_USER=ahmed@example.com\n"
        )

        self.assertEqual(self._settings().allowed_user, "ahmed@example.com")

    def test_all_five_session_ui_settings_come_from_the_env_file(self) -> None:
        self._write_env(
            "SESSION_UI_HOST=127.0.0.1\n"
            "SESSION_UI_PORT=9123\n"
            "SESSION_UI_PORTAL_TIMEOUT=42\n"
            "SESSION_UI_ALLOW_NON_LOOPBACK=yes\n"
            "SESSION_UI_ALLOWED_USER=ahmed@example.com\n"
        )

        settings = self._settings()

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 9123)
        self.assertEqual(settings.portal_timeout, 42.0)
        self.assertTrue(settings.allow_non_loopback)
        self.assertEqual(settings.allowed_user, "ahmed@example.com")

    def test_process_env_overrides_the_env_file_value(self) -> None:
        self._write_env("SESSION_UI_ALLOWED_USER=from_file@example.com\n")

        settings = self._settings(SESSION_UI_ALLOWED_USER="from_process@example.com")

        self.assertEqual(settings.allowed_user, "from_process@example.com")

    def test_a_missing_env_file_still_yields_defaults(self) -> None:
        settings = self._settings()

        self.assertEqual(settings.host, session_ui.DEFAULT_HOST)
        self.assertEqual(settings.port, session_ui.DEFAULT_PORT)
        self.assertEqual(settings.allowed_user, "")

    def test_env_file_settings_are_trimmed_and_quotes_are_stripped(self) -> None:
        self._write_env('export SESSION_UI_ALLOWED_USER = "ahmed@example.com"  # mine\n')

        self.assertEqual(self._settings().allowed_user, "ahmed@example.com")

    def test_main_uses_the_resolved_settings_paths(self) -> None:
        """`main()` has no path fallbacks of its own; the settings supply them."""
        heartbeat = self.tmpdir / "heartbeat.json"
        settings = session_ui.load_settings(
            environ={
                "ENV_FILE": str(self.env_path),
                "HEARTBEAT_FILE": str(heartbeat),
            }
        )

        env_path, heartbeat_path = session_ui._paths_for(None, None, settings)

        self.assertEqual(env_path, self.env_path)
        self.assertEqual(heartbeat_path, heartbeat)

    def test_explicit_flags_override_the_resolved_paths(self) -> None:
        heartbeat = self.tmpdir / "heartbeat.json"
        other_env = self.tmpdir / "other.env"
        other_hb = self.tmpdir / "other.json"
        settings = session_ui.load_settings(
            environ={
                "ENV_FILE": str(self.env_path),
                "HEARTBEAT_FILE": str(heartbeat),
            }
        )

        env_path, heartbeat_path = session_ui._paths_for(
            str(other_env), str(other_hb), settings
        )

        self.assertEqual(env_path, other_env)
        self.assertEqual(heartbeat_path, other_hb)

    def test_settings_always_carry_both_paths(self) -> None:
        """The dataclass requires them, so `main()` needs no `or` fallback."""
        settings = session_ui.load_settings(environ={"ENV_FILE": str(self.env_path)})

        self.assertIsInstance(settings.env_path, Path)
        self.assertIsInstance(settings.heartbeat_path, Path)
        self.assertTrue(settings.heartbeat_path.name.endswith(".json"))


class EnvFileAllowlistGateTests(_ServerTestCase):
    """End to end: an allowlist that exists only in `.env` still blocks `GET /`."""

    env_text = (
        "# only the file carries the allowlist\n"
        f"SIS_SESSION_ID={SECRET_SESSION}\n"
        "SIS_BASE_URL=https://sis.example.test\n"
        "SESSION_UI_ALLOWED_USER=ahmed@example.com\n"
    )

    def app_environ(self) -> Dict[str, str]:
        # Deliberately no SESSION_UI_ALLOWED_USER: the file is the only source.
        return {"ENV_FILE": str(self.env_path)}

    def test_unauthenticated_get_is_forbidden_when_only_the_file_sets_it(self) -> None:
        self.write_heartbeat()

        status, body = self.get()

        self.assertEqual(status, 403)
        self.assertIn("forbidden", body.lower())

    def test_the_named_user_still_gets_the_page(self) -> None:
        self.write_heartbeat()

        status, body = self.get(headers={"Tailscale-User-Login": "ahmed@example.com"})

        self.assertEqual(status, 200)
        self.assertIn("3 courses tracked", body)

    def test_unauthenticated_post_cannot_reach_the_portal(self) -> None:
        before = self.env_bytes()

        status, _ = self.post("env_file_cookie")

        self.assertEqual(status, 403)
        self.assertEqual(self.env_bytes(), before)
        self.assertEqual(self.portal.request_count, 0)


class SettingsTests(unittest.TestCase):
    """Environment-driven settings, injected as a mapping (never os.environ)."""

    #: No `.env` anywhere in play, so "default" means the built-in default and
    #: not whatever file a developer happens to keep at the repo root.
    NO_ENV = {"ENV_FILE": str(Path(tempfile.gettempdir()) / "definitely-absent-session-ui.env")}

    def test_portal_timeout_is_bounded(self) -> None:
        default = session_ui.load_settings(environ=self.NO_ENV)
        self.assertGreater(default.portal_timeout, 0)
        self.assertLessEqual(default.portal_timeout, 60)

        huge = session_ui.load_settings(
            environ=dict(self.NO_ENV, SESSION_UI_PORTAL_TIMEOUT="9999")
        )
        self.assertLessEqual(huge.portal_timeout, 60)

        tiny = session_ui.load_settings(
            environ=dict(self.NO_ENV, SESSION_UI_PORTAL_TIMEOUT="0.01")
        )
        self.assertGreaterEqual(tiny.portal_timeout, 1.0)

    def test_bad_port_falls_back_to_default(self) -> None:
        self.assertEqual(
            session_ui.load_settings(
                environ=dict(self.NO_ENV, SESSION_UI_PORT="not-a-port")
            ).port,
            8765,
        )

    def test_allowed_user_defaults_to_empty_and_is_trimmed(self) -> None:
        self.assertEqual(session_ui.load_settings(environ=self.NO_ENV).allowed_user, "")
        self.assertEqual(
            session_ui.load_settings(
                environ=dict(self.NO_ENV, SESSION_UI_ALLOWED_USER="  ahmed@example.com  ")
            ).allowed_user,
            "ahmed@example.com",
        )


if __name__ == "__main__":
    unittest.main()
