#!/usr/bin/env python3
"""Portal HTTP transport seam tests.

Everything here drives the real ``SisClient`` protocol logic (cookie staging, redirect
safety, login detection, cancellation, error translation, explicit reconfiguration) through
one in-memory stub transport adapter — no private opener patching, no fake response classes.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import tracker  # noqa: E402
from sis_client import (  # noqa: E402
    Course,
    CourseMismatchError,
    PortalHttpError,
    PortalParseError,
    PortalResponse,
    SessionExpiredError,
    SisClient,
    TransportError,
    UrllibTransport,
    parse_active_course,
    verify_active_course,
)
from support_stub_transport import (  # noqa: E402
    StubResponse,
    StubTransport,
    StubTransportError,
    transport_error,
)

BASE = "https://sis.comsats.edu.pk"


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


DASH = _auth_shell('<table class="sis-dash-table"></table>')
LOGIN_HTML = (
    '<html><body class="sis-login-page"><form action="/Login"><button>Go</button></form></body></html>'
)


def _client(transport, **kwargs) -> SisClient:
    kwargs.setdefault("request_delay", 0.0)
    return SisClient("init_sid", base_url=BASE, transport=transport, **kwargs)


class StubTransportInterfaceTests(unittest.TestCase):
    """The stub adapter contract later tickets build on."""

    def test_serves_canned_response_and_records_request(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH), url="/Courses")

        client = _client(stub)
        self.assertEqual(client.get_html("/Courses"), DASH)

        self.assertEqual(stub.request_count, 1)
        request = stub.requests[0]
        self.assertEqual(request.url, f"{BASE}/Courses")
        self.assertEqual(request.cookie_header, "ASP.NET_SessionId=init_sid")
        self.assertEqual(request.timeout, 20)
        self.assertEqual(stub.remaining_patterns(), [])

    def test_multi_value_set_cookie_headers_are_preserved(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                DASH,
                headers={
                    "Set-Cookie": [
                        "ARRAffinity=node1; Path=/; HttpOnly",
                        "PortalAffinity=edge2; Path=/",
                    ]
                },
            ),
            url="/Courses",
        )
        client = _client(stub)
        response = client.fetch_page("/Courses")
        self.assertEqual(response.set_cookie_names, ("ARRAffinity", "PortalAffinity"))
        self.assertEqual(client.last_set_cookie_names, ["ARRAffinity", "PortalAffinity"])

    def test_redirect_sequence_is_served_hop_by_hop(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "/Courses/Index"}), url="/Courses")
        stub.add(StubResponse(DASH), url="/Courses/Index")

        client = _client(stub)
        response = client.fetch_page("/Courses")
        self.assertEqual(response.body, DASH)
        self.assertEqual(response.final_url, f"{BASE}/Courses/Index")
        self.assertEqual(stub.urls(), [f"{BASE}/Courses", f"{BASE}/Courses/Index"])

    def test_unexpected_request_is_reported_loudly(self) -> None:
        stub = StubTransport()
        with self.assertRaises(AssertionError):
            stub.fetch(f"{BASE}/Courses", {})


class PortalResponseAndCookieDiagnosticsTests(unittest.TestCase):
    def test_portal_response_carries_cookie_names_final_url_and_status(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(DASH, headers={"Set-Cookie": ["ASP.NET_SessionId=rotated; Path=/"]}),
            url="/Courses",
        )
        response = _client(stub).fetch_page("/Courses")
        self.assertIsInstance(response, PortalResponse)
        self.assertEqual(response.url, f"{BASE}/Courses")
        self.assertEqual(response.final_url, f"{BASE}/Courses")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.set_cookie_names, ("ASP.NET_SessionId",))

    def test_login_page_failure_exposes_cookie_names_on_error(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(LOGIN_HTML, headers={"Set-Cookie": ["ARRAffinity=node9; Path=/"]}),
            url="/Courses",
        )
        with self.assertRaises(SessionExpiredError) as ctx:
            _client(stub).fetch_page("/Courses")
        self.assertEqual(ctx.exception.set_cookie_names, ("ARRAffinity",))

    def test_login_redirect_failure_exposes_staged_and_response_cookie_names(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                "",
                status=302,
                headers={"Location": "/Courses/Index", "Set-Cookie": ["PortalAffinity=edge; Path=/"]},
            ),
            url="/Courses",
        )
        stub.add(
            StubResponse(
                "",
                status=302,
                headers={"Location": "/Login/Index", "Set-Cookie": ["ARRAffinity=node9; Path=/"]},
            ),
            url="/Courses/Index",
        )
        with self.assertRaises(SessionExpiredError) as ctx:
            _client(stub).fetch_page("/Courses")
        self.assertEqual(ctx.exception.set_cookie_names, ("PortalAffinity", "ARRAffinity"))
        self.assertEqual(stub.request_count, 2)

    def test_http_error_exposes_cookie_names(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse("upstream down", status=503, headers={"Set-Cookie": ["ARRAffinity=n1; Path=/"]}),
            url="/Courses",
        )
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).fetch_page("/Courses")
        self.assertEqual(ctx.exception.set_cookie_names, ("ARRAffinity",))

    def test_transport_error_exposes_cookie_names_and_wraps_cause(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse("", status=302, headers={"Location": "/Courses/Index", "Set-Cookie": ["A=1; Path=/"]}),
            url="/Courses",
        )
        stub.add(transport_error("connection reset"), url="/Courses/Index")
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).fetch_page("/Courses")
        self.assertEqual(ctx.exception.set_cookie_names, ("A",))
        self.assertIsInstance(ctx.exception.__cause__, StubTransportError)

    def test_exceptions_are_constructible_with_only_a_message(self) -> None:
        expired = SessionExpiredError("boom")
        self.assertEqual(str(expired), "boom")
        self.assertEqual(expired.set_cookie_names, ())
        http_err = PortalHttpError("boom")
        self.assertEqual(str(http_err), "boom")
        self.assertEqual(http_err.set_cookie_names, ())
        # Subclasses keep the single-message construction contract.
        self.assertEqual(PortalParseError("bad markup").set_cookie_names, ())
        self.assertEqual(CourseMismatchError("mismatch").set_cookie_names, ())


class PortalCookieLifecycleTests(unittest.TestCase):
    def test_auxiliary_cookie_persists_across_requests(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH, headers={"Set-Cookie": ["ARRAffinity=node1; Path=/; HttpOnly"]}))
        stub.add(StubResponse(DASH))
        client = _client(stub)

        client.get_html("/Courses")
        client.get_html("/Courses")

        self.assertEqual(stub.requests[0].cookie_header, "ASP.NET_SessionId=init_sid")
        self.assertIn("ASP.NET_SessionId=init_sid", stub.requests[1].cookie_header)
        self.assertIn("ARRAffinity=node1", stub.requests[1].cookie_header)
        self.assertEqual(client.session_id, "init_sid")

    def test_redirect_hop_cookie_is_staged_then_committed(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                "",
                status=302,
                headers={"Location": "/Courses/Index", "Set-Cookie": ["PortalAffinity=edge2; Path=/"]},
            ),
            url="/Courses",
        )
        stub.add(StubResponse(DASH), url="/Courses/Index")
        stub.add(StubResponse(DASH), url="/Courses")
        stub.add(StubResponse(DASH), url="/Courses")
        client = _client(stub)

        client.get_html("/Courses")
        client.get_html("/Courses")

        # Hop 2 already carries the cookie staged from the redirect response...
        self.assertIn("PortalAffinity=edge2", stub.requests[1].cookie_header)
        # ...and it was committed once the authenticated response passed login detection.
        self.assertIn("PortalAffinity=edge2", stub.requests[2].cookie_header)
        self.assertEqual(client.fetch_page("/Courses").set_cookie_names, ())

    def test_authenticated_rotation_updates_session_and_clears_expired_cookies(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                DASH,
                headers={
                    "Set-Cookie": [
                        "ASP.NET_SessionId=rotated_sid; Path=/; HttpOnly",
                        "PortalAffinity=; Expires=Thu, 01-Jan-1970 00:00:00 GMT",
                    ]
                },
            )
        )
        stub.add(StubResponse(DASH))
        client = _client(stub)

        client.get_html("/Courses")
        client.get_html("/Courses")

        self.assertEqual(client.session_id, "rotated_sid")
        self.assertIn("ASP.NET_SessionId=rotated_sid", stub.requests[1].cookie_header)
        self.assertNotIn("PortalAffinity", stub.requests[1].cookie_header)

    def test_expiring_session_cookie_on_authenticated_response_is_never_committed(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                DASH,
                headers={"Set-Cookie": ["ASP.NET_SessionId=; Expires=Thu, 01-Jan-1970 00:00:00 GMT"]},
            )
        )
        stub.add(StubResponse(DASH))
        client = _client(stub)

        client.get_html("/Courses")
        client.get_html("/Courses")

        self.assertEqual(client.session_id, "init_sid")
        self.assertIn("ASP.NET_SessionId=init_sid", stub.requests[1].cookie_header)

    def test_login_responses_never_mutate_the_jar(self) -> None:
        # 200 login page carrying a hostile cookie must not be committed.
        stub = StubTransport()
        stub.add(StubResponse(LOGIN_HTML, headers={"Set-Cookie": ["ASP.NET_SessionId=evil_200; Path=/"]}))
        client = _client(stub)
        with self.assertRaises(SessionExpiredError):
            client.get_html("/Courses")
        self.assertEqual(client.session_id, "init_sid")

        # Login redirect carrying a hostile cookie must not be committed either.
        stub2 = StubTransport()
        stub2.add(
            StubResponse(
                "",
                status=302,
                headers={"Location": "/Login/Index", "Set-Cookie": ["ASP.NET_SessionId=evil_redir; Path=/"]},
            )
        )
        client2 = _client(stub2)
        with self.assertRaises(SessionExpiredError):
            client2.get_html("/Courses")
        self.assertEqual(client2.session_id, "init_sid")


class PortalRedirectSafetyTests(unittest.TestCase):
    def test_cross_host_redirect_refused_without_second_request(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "https://evil.example.com/steal"}))
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).get_html("/Courses")
        self.assertIn("Refusing unsafe redirect", str(ctx.exception))
        self.assertEqual(stub.request_count, 1)

    def test_https_to_http_downgrade_refused_without_second_request(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "http://sis.comsats.edu.pk/Courses/Index"}))
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).get_html("/Courses")
        self.assertIn("Refusing unsafe redirect", str(ctx.exception))
        self.assertEqual(stub.request_count, 1)

    def test_same_host_https_redirect_allowed(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "/Courses/Index"}), url="/Courses")
        stub.add(StubResponse(DASH), url="/Courses/Index")
        self.assertEqual(_client(stub).get_html("/Courses"), DASH)
        self.assertEqual(stub.request_count, 2)

    def test_cross_host_login_redirect_raises_session_expired(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "https://sso.example.com/Login/Index"}))
        with self.assertRaises(SessionExpiredError):
            _client(stub).get_html("/Courses")

    def test_redirect_without_location_is_an_http_error(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302))
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).get_html("/Courses")
        self.assertIn("HTTP 302", str(ctx.exception))

    def test_redirect_loop_is_bounded(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "/Courses/Loop"}), url="/Courses")
        for _ in range(6):
            stub.add(StubResponse("", status=302, headers={"Location": "/Courses/Loop"}), url="/Courses/Loop")
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).get_html("/Courses")
        self.assertIn("Too many redirects", str(ctx.exception))
        self.assertEqual(stub.request_count, 4)


class PortalLoginDetectionTests(unittest.TestCase):
    def test_200_login_page_raises_session_expired(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(LOGIN_HTML))
        with self.assertRaises(SessionExpiredError):
            _client(stub).get_html("/Courses")

    def test_login_final_url_raises_session_expired(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH, final_url=f"{BASE}/Login/Index"))
        with self.assertRaises(SessionExpiredError) as ctx:
            _client(stub).get_html("/Courses")
        self.assertIn("login", str(ctx.exception).lower())

    def test_missing_page_markers_raise_parse_errors(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_auth_shell("<div>Empty error shell</div>")))
        stub.add(StubResponse(_auth_shell("<div>Empty error shell</div>")))
        client = _client(stub)
        with self.assertRaises(PortalParseError):
            client.get_marks_summary()
        with self.assertRaises(PortalParseError):
            client.get_learning_resources()


class PortalActiveCourseTests(unittest.TestCase):
    def test_active_course_verification_prevents_attribution_drift(self) -> None:
        expected_325 = Course(course_id="182153", code="CSC325", name="COAL")
        stuck_html = _auth_shell(
            '<div class="sis-lr"><span class="sis-lr-count">0 resources</span></div>',
            course_code="CSC336",
            course_name="Web Technologies",
        )
        info = parse_active_course(stuck_html)
        self.assertIsNotNone(info)
        self.assertEqual(info.code if info else "", "CSC336")

        stub = StubTransport()
        stub.add(StubResponse(stuck_html))
        client = _client(stub)
        with self.assertRaises(CourseMismatchError):
            client.select_course("182153", expected_course=expected_325)

    def test_matching_active_course_passes(self) -> None:
        expected = Course(course_id="182153", code="CSC325", name="COAL")
        html = _auth_shell(
            '<div class="sis-lr"><span class="sis-lr-count">1 resource</span></div>',
            course_code="CSC325",
            course_name="Computer Organization and Assembly Language",
        )
        verify_active_course(html, expected)
        stub = StubTransport()
        stub.add(StubResponse(html))
        self.assertEqual(_client(stub).get_learning_resources(expected_course=expected), [])


class PortalHeaderAndErrorTranslationTests(unittest.TestCase):
    def test_crlf_and_non_latin1_user_agent_is_sanitized_before_send(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH))
        client = _client(stub, user_agent="Mozilla/5.0\r\nX-Bad: 1 — test")
        client.get_html("/Courses")
        self.assertEqual(stub.requests[0].header("User-Agent"), "Mozilla/5.0X-Bad: 1  test")
        self.assertEqual(stub.requests[0].header("Referer"), f"{BASE}/")

    def test_invalid_base_url_translates_to_portal_http_error(self) -> None:
        # The production adapter rejects the scheme-less URL; Portal translates it.
        client = SisClient("sid", base_url="sis.comsats.edu.pk", request_delay=0.0)
        with self.assertRaises(PortalHttpError):
            client.get_html("/Courses")

    def test_transport_exception_types_translate_to_portal_http_error(self) -> None:
        for exc in (
            UnicodeEncodeError("latin-1", "bad", 0, 1, "ordinal not in range"),
            TimeoutError("timed out"),
            OSError("socket closed"),
            ValueError("bad url"),
            TransportError("tls handshake failed"),
        ):
            stub = StubTransport()
            stub.add(exc)
            with self.assertRaises(PortalHttpError):
                _client(stub).get_html("/Courses")

    def test_http_status_error_is_translated(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("nope", status=500))
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).get_html("/Courses")
        self.assertIn("HTTP 500", str(ctx.exception))


class PortalCancellationTests(unittest.TestCase):
    def test_cancelled_before_throttle_raises_without_a_request(self) -> None:
        cancel = threading.Event()
        cancel.set()
        stub = StubTransport()
        client = SisClient("sid", base_url=BASE, request_delay=5.0, cancel_event=cancel, transport=stub)
        with self.assertRaises(PortalHttpError):
            client.get_html("/Courses")
        self.assertEqual(stub.request_count, 0)

    def test_cancellation_interrupts_throttle_wait(self) -> None:
        cancel = threading.Event()
        stub = StubTransport()
        stub.add(StubResponse(DASH))
        stub.add(StubResponse(DASH))
        client = SisClient(
            "sid", base_url=BASE, request_delay=30.0, cancel_event=cancel, transport=stub
        )
        client.get_html("/Courses")

        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(PortalHttpError):
            client.get_html("/Courses")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(stub.request_count, 1)

    def test_cancellation_between_redirect_hops(self) -> None:
        cancel = threading.Event()
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "/Courses/Index"}), url="/Courses")
        stub.add(StubResponse(DASH), url="/Courses/Index")

        def _cancel_on_second(request) -> None:
            if len(stub.requests) == 1:
                cancel.set()

        stub.on_request = _cancel_on_second
        client = SisClient(
            "sid", base_url=BASE, request_delay=0.0, cancel_event=cancel, transport=stub
        )
        with self.assertRaises(PortalHttpError) as ctx:
            client.get_html("/Courses")
        self.assertIn("cancelled", str(ctx.exception).lower())
        self.assertEqual(stub.request_count, 1)


class PortalReconfigurationTests(unittest.TestCase):
    def test_session_id_is_read_only(self) -> None:
        client = _client(StubTransport())
        with self.assertRaises(AttributeError):
            client.session_id = "nope"  # type: ignore[misc]

    def test_reconfigure_updates_settings_without_touching_cookies(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH, headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]}))
        stub.add(StubResponse(DASH))
        client = _client(stub)
        client.get_html("/Courses")

        reset = client.reconfigure(
            base_url=BASE + "/", user_agent="ua-2", request_delay=0.25, timeout=11
        )
        self.assertFalse(reset)
        self.assertEqual(client.base_url, BASE)
        self.assertEqual(client.user_agent, "ua-2")
        self.assertEqual(client.request_delay, 0.25)
        self.assertEqual(client.timeout, 11)

        client.get_html("/Courses")
        # Unchanged session id -> auxiliary cookies survive the reconfiguration.
        self.assertIn("ARRAffinity=node1", stub.requests[1].cookie_header)
        self.assertEqual(stub.requests[1].header("User-Agent"), "ua-2")

    def test_reconfigure_with_new_session_id_resets_the_jar(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH, headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]}))
        stub.add(StubResponse(DASH))
        client = _client(stub)
        client.get_html("/Courses")

        reset = client.reconfigure(session_id="brand_new")
        self.assertTrue(reset)
        self.assertEqual(client.session_id, "brand_new")

        client.get_html("/Courses")
        self.assertEqual(stub.requests[1].cookie_header, "ASP.NET_SessionId=brand_new")

    def test_same_session_id_does_not_reset_the_jar(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH, headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]}))
        stub.add(StubResponse(DASH))
        client = _client(stub)
        client.get_html("/Courses")

        self.assertFalse(client.reconfigure(session_id="init_sid"))
        client.get_html("/Courses")
        self.assertIn("ARRAffinity=node1", stub.requests[1].cookie_header)

    def test_explicit_reset_cookies_clears_the_jar_even_without_session_change(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH, headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]}))
        stub.add(StubResponse(DASH))
        client = _client(stub)
        client.get_html("/Courses")

        self.assertTrue(client.reconfigure(reset_cookies=True))
        client.get_html("/Courses")
        self.assertEqual(stub.requests[1].cookie_header, "ASP.NET_SessionId=init_sid")

    def test_reconfigure_replaces_the_cancel_event(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH))
        client = _client(stub)
        new_cancel = threading.Event()
        client.reconfigure(cancel_event=new_cancel)
        self.assertIs(client.cancel_event, new_cancel)

    def test_rotated_session_from_server_survives_reconfigure_and_reports_change(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(DASH, headers={"Set-Cookie": ["ASP.NET_SessionId=server_sid; Path=/"]}))
        client = _client(stub)
        client.get_html("/Courses")
        self.assertEqual(client.session_id, "server_sid")
        # Reconfiguring with the original .env value is a genuine change -> jar reset.
        self.assertTrue(client.reconfigure(session_id="init_sid"))
        self.assertEqual(client.session_id, "init_sid")


class ProductionTransportTests(unittest.TestCase):
    def test_default_transport_is_the_stdlib_adapter(self) -> None:
        client = SisClient("sid", request_delay=0.0)
        self.assertIsInstance(client.transport, UrllibTransport)

    def test_urllib_adapter_returns_redirect_instead_of_following_it(self) -> None:
        hits: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                hits.append(self.path)
                if self.path == "/Courses":
                    self.send_response(302)
                    self.send_header("Location", "/Login/Index")
                    self.send_header("Set-Cookie", "ARRAffinity=node1; Path=/")
                    self.end_headers()
                else:  # pragma: no cover - the adapter must never follow the redirect
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"followed")

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
                """Keep test output quiet."""


        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            client = SisClient("sid", base_url=f"http://127.0.0.1:{port}", request_delay=0.0)
            with self.assertRaises(SessionExpiredError) as ctx:
                client.fetch_page("/Courses")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

        self.assertEqual(hits, ["/Courses"])
        self.assertEqual(ctx.exception.set_cookie_names, ("ARRAffinity",))


class ProbeTransportSafetyTests(unittest.TestCase):
    """`probe.fetch` keeps its own redirect guard; verify it against a real local server."""

    def test_probe_fetch_refuses_cross_host_redirect(self) -> None:
        import probe

        hits: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "https://evil.example.com/steal")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
                """Keep test output quiet."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            result = probe.fetch(f"http://127.0.0.1:{port}/Courses", "secret_sid")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

        self.assertEqual(result.status, 302)
        self.assertEqual(hits, ["/Courses"])

    def test_probe_fetch_refuses_port_change_redirect(self) -> None:
        import probe

        hits: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/Courses/Index")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
                """Keep test output quiet."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            result = probe.fetch(f"http://127.0.0.1:{port}/Courses", "secret_sid")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

        self.assertEqual(result.status, 302)
        self.assertEqual(hits, ["/Courses"])


class ScanCookieDiagnosticsTests(unittest.TestCase):
    """Mid-scan failures must still surface cookie diagnostics through ScanResult."""

    def test_mid_scan_session_expiry_carries_error_cookie_names(self) -> None:
        courses = [
            Course(course_id="100001", code="CSC101", name="Course 1"),
            Course(course_id="100002", code="CSC102", name="Course 2"),
        ]
        marks_html = _auth_shell(
            '<div class="sis-marks-summary"><p class="sis-marks-empty">No marks available.</p></div>',
            course_code="CSC101",
            course_name="Course 1",
        )
        resources_html = _auth_shell(
            '<div class="sis-lr"><span class="sis-lr-count">0 resources</span></div>',
            course_code="CSC101",
            course_name="Course 1",
        )

        stub = StubTransport()
        # Course 1: SetCourse -> marks -> resources all succeed.
        stub.add(StubResponse(DASH), url="/Courses/SetCourse/100001")
        stub.add(StubResponse(marks_html), url="/MarksSummary/Index")
        stub.add(StubResponse(resources_html), url="/LearningResources/Index")
        # Course 2: SetCourse rotates to the login page while setting a cookie.
        stub.add(
            StubResponse(
                "",
                status=302,
                headers={"Location": "/Login/Index", "Set-Cookie": ["ARRAffinity=node7; Path=/"]},
            ),
            url="/Courses/SetCourse/100002",
        )

        scan = tracker.scan_all_courses(_client(stub), courses)
        self.assertTrue(scan.session_expired)
        self.assertIn("ARRAffinity", scan.set_cookie_names)

    def test_mid_scan_portal_error_carries_error_cookie_names(self) -> None:
        courses = [Course(course_id="100001", code="CSC101", name="Course 1")]
        stub = StubTransport()
        stub.add(
            StubResponse("boom", status=502, headers={"Set-Cookie": ["PortalAffinity=x; Path=/"]}),
            url="/Courses/SetCourse/100001",
        )
        scan = tracker.scan_all_courses(_client(stub), courses)
        self.assertTrue(scan.partial)
        self.assertIn("PortalAffinity", scan.set_cookie_names)


class DaemonReconfigurationTests(unittest.TestCase):
    """The daemon hot-reload path must go through the explicit reconfiguration API."""

    def test_expired_session_env_change_force_clears_the_cookie_jar(self) -> None:
        from test_tracker import _RecordingNotifier

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            state_path = Path(tmp) / "data" / "state.json"
            hb_path = Path(tmp) / "data" / "heartbeat.json"
            env_content = (
                "SIS_SESSION_ID=reused_cookie\n"
                "KEEPALIVE_INTERVAL=15\n"
                "FETCH_INTERVAL=30\n"
                "EXPIRED_POLL_INTERVAL=1\n"
                "SESSION_EXPIRED_TIMEOUT=20\n"
            )
            env_path.write_text(env_content, encoding="utf-8")

            notifier = _RecordingNotifier()
            resumed = threading.Event()
            retested = threading.Event()
            reconfig_calls: list[dict] = []

            class _RecordingClient:
                def __init__(self, session_id, **kwargs) -> None:
                    self.session_id = session_id
                    self.base_url = kwargs.get("base_url", BASE)
                    self.user_agent = kwargs.get("user_agent", "")
                    self.request_delay = 0.0
                    self.cancel_event = None
                    self.attempts = 0

                def reconfigure(self, session_id=None, base_url=None, user_agent=None,
                                request_delay=None, reset_cookies=False):
                    reconfig_calls.append(
                        {"session_id": session_id, "reset_cookies": reset_cookies}
                    )
                    if session_id is not None:
                        self.session_id = session_id
                    return reset_cookies

                def get_courses(self):
                    self.attempts += 1
                    if self.attempts == 1:
                        raise SessionExpiredError("Expired #1")
                    if self.attempts == 2:
                        retested.set()
                        raise SessionExpiredError("Still expired #2")
                    resumed.set()
                    return [Course(course_id="100001", code="CSC101", name="Intro")]

                def select_course(self, cid, expected_course=None):
                    return ""

                def get_marks_summary(self, expected_course=None):
                    return []

                def get_learning_resources(self, expected_course=None):
                    return []

            def _wait_for_waiting() -> None:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if hb_path.exists() and "waiting_for_session" in hb_path.read_text(encoding="utf-8"):
                        return
                    time.sleep(0.05)
                self.fail("daemon never entered waiting_for_session")

            def _updater() -> None:
                import os
                import signal

                _wait_for_waiting()
                tmp_env = Path(tmp) / ".env.tmp1"
                tmp_env.write_text(env_content + "# touched once\n", encoding="utf-8")
                os.replace(tmp_env, env_path)
                self.assertTrue(retested.wait(timeout=5.0))

                _wait_for_waiting()
                tmp_env2 = Path(tmp) / ".env.tmp2"
                tmp_env2.write_text(env_content + "# touched twice\n", encoding="utf-8")
                os.replace(tmp_env2, env_path)
                self.assertTrue(resumed.wait(timeout=5.0))
                time.sleep(0.2)
                signal.raise_signal(signal.SIGTERM)

            import signal

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_updater, daemon=True)
            t.start()
            try:
                with mock.patch.object(tracker, "SisClient", _RecordingClient), mock.patch.object(
                    tracker, "build_notifiers", return_value=[notifier]
                ):
                    rc = tracker.run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=2.0)

            self.assertEqual(rc, 0)
            self.assertTrue(any(call["reset_cookies"] for call in reconfig_calls))
            self.assertIn("session_expired", [n.event for n in notifier.notes])


if __name__ == "__main__":
    unittest.main()
