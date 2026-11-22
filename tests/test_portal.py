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
from runtime_store import RuntimeStore  # noqa: E402
from sis_client import (  # noqa: E402
    AnnouncementItem,
    Course,
    CourseMismatchError,
    CourseSnapshotResult,
    PortalHttpError,
    PortalParseError,
    PortalResponse,
    PortalTransport,
    RosterResult,
    SessionExpiredError,
    SisClient,
    TransportError,
    UrllibTransport,
    parse_active_course,
    verify_active_course,
)

from support_portal_pages import SimulatedPortal, announcements_page, courses_page  # noqa: E402
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
COURSES_PAGE = _auth_shell(
    '<table class="table sis-dash-table"><thead><tr><th>Course No</th><th>Course Name</th></tr></thead>'
    '<tbody><tr onclick="window.location=\'/Courses/SetCourse/100001\'">'
    "<td>CSC101</td><td>Intro</td></tr></tbody></table>"
)
LOGIN_HTML = (
    '<html><body class="sis-login-page"><form action="/Login"><button>Go</button></form></body></html>'
)


def _intro_course() -> Course:
    return Course(course_id="100001", code="CSC101", name="Intro")


def _course_shell(course_id: str, code: str, name: str) -> str:
    """SetCourse response whose active-course header matches ``code``/``name``."""
    return _auth_shell(f'<div class="sis-ann">course {course_id}</div>', code, name)


MARKS_ROWS = """
      <tr id="9001">
        <td>Quiz1</td>
        <td><span class="sis-marks-score">7.0</span></td>
        <td>10.0</td>
        <td>Saturday, 31 October 2026</td>
      </tr>
      <tr id="9002">
        <td>Assign1</td>
        <td><span class="sis-marks-score">8.5</span></td>
        <td>10.0</td>
        <td>Sunday, 1 November 2026</td>
      </tr>
"""


def _marks_shell(code: str, name: str, rows: str) -> str:
    if rows:
        body = (
            '<div class="sis-marks-summary table-responsive quiz_listing">'
            '<div class="sis-marks-section-head">Quizzes</div>'
            '<table class="table table-striped sis-marks-table"><thead><tr>'
            "<th>Title</th><th>Obtained Marks</th><th>Total Marks</th><th>Date Time</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
    else:
        body = (
            '<div class="sis-marks-summary"><p class="sis-marks-empty">'
            "No marks available.</p></div>"
        )
    return _auth_shell(body, code, name)


def _resources_shell(code: str, name: str) -> str:
    return _auth_shell(
        '<div class="sis-lr"><span class="sis-lr-count">1 resource</span>'
        '<div class="sis-lr-table-wrap"><table class="table sis-lr-table"><thead><tr>'
        "<th>#</th><th>Title</th><th>Type</th><th>Date</th><th>Size</th><th>Action</th>"
        '</tr></thead><tbody><tr class="sis-lr-row" id="res-55">'
        '<td class="sis-lr-num">1</td><td class="sis-lr-title">Week 1 Slides</td>'
        '<td><span class="sis-lr-material">Lecture Notes</span></td>'
        '<td class="sis-lr-date">02 Nov 2026</td><td class="sis-lr-size">1.2 MB</td>'
        '<td class="actions"><a class="sis-lr-btn" href="/LearningResources/Download/55">'
        "Download</a></td></tr></tbody></table></div></div>",
        code,
        name,
    )


ANNOUNCEMENTS = [
    AnnouncementItem(
        key="Quiz 2 Postponed|Date: 10 Nov 2026",
        title="Quiz 2 Postponed",
        meta="Date: 10 Nov 2026",
        body="Moved to Friday.",
    )
]


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

    def test_redirect_sequence_is_served_hop_by_hop(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse("", status=302, headers={"Location": "/Courses/Index"}), url="/Courses")
        stub.add(StubResponse(DASH), url="/Courses/Index")

        client = _client(stub)
        response = client.fetch_page("/Courses")
        self.assertEqual(response.body, DASH)
        self.assertEqual(response.final_url, f"{BASE}/Courses/Index")
        self.assertEqual(stub.urls(), [f"{BASE}/Courses", f"{BASE}/Courses/Index"])

    def test_multi_value_location_header_is_read_as_a_single_url(self) -> None:
        # Adapters may hand back repeated headers as sequences; Portal must read one value.
        stub = StubTransport()
        stub.add(
            StubResponse("", status=302, headers={"Location": ["/Courses/Index"]}),
            url="/Courses",
        )
        stub.add(StubResponse(DASH), url="/Courses/Index")
        self.assertEqual(_client(stub).get_html("/Courses"), DASH)

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
        shell = _auth_shell(
            "<div>Empty error shell</div>", course_code="CSC101", course_name="Intro"
        )
        stub.add(StubResponse(shell))
        stub.add(StubResponse(shell))
        stub.add(StubResponse(shell))
        client = _client(stub)
        course = Course(course_id="100001", code="CSC101", name="Intro")
        with self.assertRaises(PortalParseError) as ctx:
            client.fetch_course_snapshot(course)
        self.assertEqual(ctx.exception.set_cookie_names, ())
        self.assertIn("MarksSummary", str(ctx.exception))

    def test_parse_error_reports_cookie_names_observed_on_the_page(self) -> None:
        stub = StubTransport()
        shell = _auth_shell(
            "<div>Empty error shell</div>", course_code="CSC101", course_name="Intro"
        )
        stub.add(StubResponse(shell))
        stub.add(
            StubResponse(shell, headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]})
        )
        course = Course(course_id="100001", code="CSC101", name="Intro")
        with self.assertRaises(PortalParseError) as ctx:
            _client(stub).fetch_course_snapshot(course)
        self.assertEqual(ctx.exception.set_cookie_names, ("ARRAffinity",))


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

        # SetCourse answers with a *different* active course than the one requested.
        stub = StubTransport()
        stub.add(StubResponse(stuck_html))
        with self.assertRaises(CourseMismatchError):
            _client(stub).fetch_course_snapshot(expected_325)

    def test_matching_active_course_passes(self) -> None:
        expected = Course(course_id="182153", code="CSC325", name="COAL")
        html = _auth_shell(
            '<div class="sis-lr"><span class="sis-lr-count">1 resource</span></div>',
            course_code="CSC325",
            course_name="Computer Organization and Assembly Language",
        )
        verify_active_course(html, expected)
        marks = _auth_shell(
            '<div class="sis-marks-summary"><p class="sis-marks-empty">No marks available.</p></div>',
            course_code="CSC325",
            course_name="Computer Organization and Assembly Language",
        )
        stub = StubTransport()
        stub.add(StubResponse(html))
        stub.add(StubResponse(marks))
        stub.add(StubResponse(html))
        snapshot = _client(stub).fetch_course_snapshot(expected)
        self.assertEqual(snapshot.marks, [])
        self.assertIn("No marks available.", snapshot.diagnostic_preview)
        self.assertEqual(
            stub.urls(),
            [f"{BASE}/Courses/SetCourse/182153", f"{BASE}/MarksSummary/Index",
             f"{BASE}/LearningResources/Index"],
        )


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

    def test_urllib_adapter_commits_cookies_from_a_real_200_response(self) -> None:
        cookies_seen: list[str] = []

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                cookies_seen.append(self.headers.get("Cookie", ""))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if len(cookies_seen) == 1:
                    self.send_header("Set-Cookie", "ARRAffinity=node1; Path=/; HttpOnly")
                    self.send_header("Set-Cookie", "PortalAffinity=edge2; Path=/")
                body = DASH.encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
                """Keep test output quiet."""

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            client = SisClient("sid", base_url=f"http://127.0.0.1:{port}", request_delay=0.0)
            page = client.fetch_page("/Courses")
            response = page.body
            self.assertEqual(page.set_cookie_names, ("ARRAffinity", "PortalAffinity"))
            client.get_html("/Courses")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5.0)

        self.assertEqual(response, DASH)
        self.assertEqual(cookies_seen[0], "ASP.NET_SessionId=sid")
        self.assertEqual(
            cookies_seen[1], "ASP.NET_SessionId=sid; ARRAffinity=node1; PortalAffinity=edge2"
        )

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


class PortalTypedRosterTests(unittest.TestCase):
    """``fetch_roster()`` owns dashboard quirks and returns one typed result."""

    def test_returns_courses_semester_and_cookie_names(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                COURSES_PAGE,
                headers={"Set-Cookie": ["ARRAffinity=node1; Path=/", "PortalAffinity=e2; Path=/"]},
            ),
            url="/Courses",
        )
        roster = _client(stub).fetch_roster()
        self.assertEqual([c.course_id for c in roster.courses], ["100001"])
        self.assertEqual(roster.courses[0].code, "CSC101")
        self.assertEqual(roster.semester, "")
        self.assertEqual(roster.set_cookie_names, ("ARRAffinity", "PortalAffinity"))
        self.assertEqual(stub.urls(), [f"{BASE}/Courses"])

    def test_extracts_semester_label_from_the_dashboard(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(courses_page([_intro_course()], semester="Spring 2027")))
        roster = _client(stub).fetch_roster()
        self.assertEqual(roster.semester, "Spring 2027")
        self.assertEqual([c.code for c in roster.courses], ["CSC101"])

    def test_missing_dashboard_marker_raises_parse_error(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_auth_shell("<div>nothing here</div>")))
        with self.assertRaises(PortalParseError) as ctx:
            _client(stub).fetch_roster()
        self.assertIn("courses table", str(ctx.exception))

    def test_zero_courses_raises_parse_error(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_auth_shell('<table class="sis-dash-table"></table>')))
        with self.assertRaises(PortalParseError) as ctx:
            _client(stub).fetch_roster()
        self.assertIn("0 courses", str(ctx.exception))

    def test_login_page_raises_session_expired_not_parse_error(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(LOGIN_HTML))
        with self.assertRaises(SessionExpiredError):
            _client(stub).fetch_roster()


class PortalTypedSnapshotTests(unittest.TestCase):
    """``fetch_course_snapshot()`` owns course switching, verification and parsing."""

    def test_returns_marks_resources_and_cookie_names_in_course_order(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_course_shell("100001", "CSC101", "Intro")))
        stub.add(StubResponse(_marks_shell("CSC101", "Intro", MARKS_ROWS)))
        stub.add(
            StubResponse(
                _resources_shell("CSC101", "Intro"),
                headers={"Set-Cookie": ["ARRAffinity=node3; Path=/"]},
            )
        )
        snapshot = _client(stub).fetch_course_snapshot(_intro_course())

        self.assertEqual([m.key for m in snapshot.marks], ["Quizzes:9001", "Quizzes:9002"])
        self.assertEqual([r.key for r in snapshot.resources], ["res-55"])
        self.assertIsNone(snapshot.announcements)
        self.assertEqual(snapshot.diagnostic_html, "")
        self.assertEqual(snapshot.diagnostic_preview, "")
        self.assertEqual(snapshot.set_cookie_names, ("ARRAffinity",))
        self.assertEqual(
            stub.urls(),
            [
                f"{BASE}/Courses/SetCourse/100001",
                f"{BASE}/MarksSummary/Index",
                f"{BASE}/LearningResources/Index",
            ],
        )

    def test_announcements_are_parsed_only_when_requested(self) -> None:
        def _stub() -> StubTransport:
            stub = StubTransport()
            stub.add(StubResponse(announcements_page(_intro_course(), ANNOUNCEMENTS)))
            stub.add(StubResponse(_marks_shell("CSC101", "Intro", MARKS_ROWS)))
            stub.add(StubResponse(_resources_shell("CSC101", "Intro")))
            return stub

        without = _client(_stub()).fetch_course_snapshot(_intro_course())
        self.assertIsNone(without.announcements)

        with_ann = _client(_stub()).fetch_course_snapshot(
            _intro_course(), include_announcements=True
        )
        assert with_ann.announcements is not None
        self.assertEqual([a.title for a in with_ann.announcements], ["Quiz 2 Postponed"])
        self.assertIn("10 Nov 2026", with_ann.announcements[0].to_dict()["meta"])

    def test_empty_marks_carry_scrubbed_html_and_preview(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_course_shell("100001", "CSC101", "Intro")))
        stub.add(StubResponse(_marks_shell("CSC101", "Intro", "")))
        stub.add(StubResponse(_resources_shell("CSC101", "Intro")))
        snapshot = _client(stub).fetch_course_snapshot(_intro_course())

        self.assertEqual(snapshot.marks, [])
        self.assertIn("sis-marks-summary", snapshot.diagnostic_html)
        self.assertIn("No marks available.", snapshot.diagnostic_preview)
        self.assertNotIn("<html", snapshot.diagnostic_html)

    def test_course_mismatch_aborts_before_marks_are_attributed(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_course_shell("100001", "CSC336", "Web Technologies")))
        with self.assertRaises(CourseMismatchError):
            _client(stub).fetch_course_snapshot(_intro_course())
        # Only the SetCourse request was made: marks attribution never happened.
        self.assertEqual(stub.urls(), [f"{BASE}/Courses/SetCourse/100001"])

    def test_mismatch_after_a_cookie_rotation_still_reports_the_name(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                _course_shell("100001", "CSC101", "Intro"),
                headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]},
            )
        )
        # SetCourse still reports CSC101, but the marks page claims CSC336: the sticky
        # session glitch this verification exists for.
        stub.add(
            StubResponse(
                _marks_shell("CSC336", "Web Technologies", MARKS_ROWS),
                headers={"Set-Cookie": ["PortalAffinity=edge2; Path=/"]},
            )
        )
        with self.assertRaises(CourseMismatchError) as ctx:
            _client(stub).fetch_course_snapshot(_intro_course())
        self.assertEqual(ctx.exception.set_cookie_names, ("ARRAffinity", "PortalAffinity"))

    def test_late_failure_keeps_every_cookie_name_observed_so_far(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                _course_shell("100001", "CSC101", "Intro"),
                headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]},
            )
        )
        stub.add(
            StubResponse(
                _marks_shell("CSC101", "Intro", MARKS_ROWS),
                headers={"Set-Cookie": ["PortalAffinity=edge2; Path=/"]},
            )
        )
        stub.add(StubResponse("upstream boom", status=502))
        with self.assertRaises(PortalHttpError) as ctx:
            _client(stub).fetch_course_snapshot(_intro_course())
        self.assertEqual(ctx.exception.set_cookie_names, ("ARRAffinity", "PortalAffinity"))

    def test_session_expiry_mid_snapshot_carries_cookie_names(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                _course_shell("100001", "CSC101", "Intro"),
                headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]},
            )
        )
        stub.add(
            StubResponse("", status=302, headers={"Location": "/Login/Index"}), url="/MarksSummary/Index"
        )
        with self.assertRaises(SessionExpiredError) as ctx:
            _client(stub).fetch_course_snapshot(_intro_course())
        self.assertEqual(ctx.exception.set_cookie_names, ("ARRAffinity",))

    def test_missing_marks_marker_raises_parse_error(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_course_shell("100001", "CSC101", "Intro")))
        stub.add(StubResponse(_course_shell("100001", "CSC101", "Intro")))
        with self.assertRaises(PortalParseError) as ctx:
            _client(stub).fetch_course_snapshot(_intro_course())
        self.assertIn("MarksSummary", str(ctx.exception))

    def test_missing_resources_marker_raises_parse_error(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_course_shell("100001", "CSC101", "Intro")))
        stub.add(StubResponse(_marks_shell("CSC101", "Intro", MARKS_ROWS)))
        stub.add(StubResponse(_course_shell("100001", "CSC101", "Intro")))
        with self.assertRaises(PortalParseError) as ctx:
            _client(stub).fetch_course_snapshot(_intro_course())
        self.assertIn("LearningResources", str(ctx.exception))

    def test_result_dataclasses_are_constructible_with_defaults(self) -> None:
        self.assertEqual(RosterResult().courses, [])
        self.assertEqual(RosterResult().semester, "")
        self.assertEqual(RosterResult().set_cookie_names, ())
        snapshot = CourseSnapshotResult()
        self.assertEqual(snapshot.marks, [])
        self.assertEqual(snapshot.resources, [])
        self.assertIsNone(snapshot.announcements)
        self.assertEqual(snapshot.set_cookie_names, ())
        self.assertEqual(snapshot.diagnostic_html, "")
        self.assertEqual(snapshot.diagnostic_preview, "")


class PortalSecretScrubbingSeamTests(unittest.TestCase):
    """Raw form inputs and cookie values must never cross the Portal seam."""

    PASSWORD_VALUE = "SUPER_SECRET_PWD_5"
    SESSION_VALUE = "SESSION_ID_DO_NOT_LEAK"

    def _hostile_marks_page(self) -> str:
        secret_shell = (
            '<form id="synopsisForm" method="post" action="https://ww5.comsats.edu.pk/efs/StudentLogin.aspx">'
            f'<input name="uid" hidden type="text" value="CIIT/TEST-001/ISB">'
            f'<input name="pwd" hidden type="password" value="{self.PASSWORD_VALUE}">'
            "</form>"
            '<input type="password" name="inner" value="INNER_SECRET_5">'
            f'<span class="session-echo">{self.SESSION_VALUE}</span>'
        )
        # Empty marks so the diagnostic path (the only place HTML could cross) is used.
        return _auth_shell(
            secret_shell
            + '<div class="content_area"><div class="sis-marks-summary">'
            '<p class="sis-marks-empty">No marks available.</p></div></div>',
            _intro_course().code,
            _intro_course().name,
        )

    def test_diagnostic_html_is_already_scrubbed_and_still_has_page_content(self) -> None:
        stub = StubTransport()
        stub.add(StubResponse(_course_shell("100001", "CSC101", "Intro")))
        stub.add(
            StubResponse(
                self._hostile_marks_page(),
                headers={
                    "Set-Cookie": [f"ASP.NET_SessionId={self.SESSION_VALUE}; Path=/"]
                },
            )
        )
        stub.add(StubResponse(_resources_shell("CSC101", "Intro")))

        snapshot = _client(stub).fetch_course_snapshot(_intro_course())

        self.assertEqual(snapshot.marks, [])
        self.assertTrue(snapshot.diagnostic_html)
        # Secrets are gone...
        self.assertNotIn(self.PASSWORD_VALUE, snapshot.diagnostic_html)
        self.assertNotIn("synopsisForm", snapshot.diagnostic_html)
        self.assertNotIn("INNER_SECRET_5", snapshot.diagnostic_html)
        self.assertNotIn(self.SESSION_VALUE, snapshot.diagnostic_html)
        self.assertNotIn("type=\"password\"", snapshot.diagnostic_html.lower())
        # ...while the actual page content survives.
        self.assertIn("sis-marks-summary", snapshot.diagnostic_html)
        self.assertIn("No marks available.", snapshot.diagnostic_html)
        # The preview is text-only and equally clean.
        self.assertIn("No marks available.", snapshot.diagnostic_preview)
        self.assertNotIn(self.PASSWORD_VALUE, snapshot.diagnostic_preview)
        self.assertNotIn(self.SESSION_VALUE, snapshot.diagnostic_preview)

    def test_cookie_value_travels_as_a_header_name_only(self) -> None:
        stub = StubTransport()
        stub.add(
            StubResponse(
                _course_shell("100001", "CSC101", "Intro"),
                headers={"Set-Cookie": [f"ASP.NET_SessionId={self.SESSION_VALUE}; Path=/"]},
            )
        )
        stub.add(StubResponse(_marks_shell("CSC101", "Intro", MARKS_ROWS)))
        stub.add(StubResponse(_resources_shell("CSC101", "Intro")))

        snapshot = _client(stub).fetch_course_snapshot(_intro_course())

        # Only the *name* is reported; the value stays inside the cookie jar.
        self.assertEqual(snapshot.set_cookie_names, ("ASP.NET_SessionId",))
        self.assertNotIn(self.SESSION_VALUE, snippet(snapshot))

    def test_anomaly_artifact_written_end_to_end_contains_no_secrets(self) -> None:
        import tracker

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "data" / "state.json"
            portal = SimulatedPortal(
                courses=[_intro_course()],
                marks={_intro_course().course_id: []},
                marks_html={_intro_course().course_id: self._hostile_marks_page()},
            )
            portal.add(StubResponse(COURSES_PAGE), url="/Courses")
            client = SisClient(
                "sid", base_url=BASE, request_delay=0.0, transport=portal
            )

            RuntimeStore(state_path).save_state(
                {
                    "courses": {
                        "100001": {
                            "code": "CSC101",
                            "name": "Intro",
                            "label": "CSC101 — Intro",
                            "marks": {
                                "Quizzes:1": {
                                    "key": "Quizzes:1",
                                    "section": "Quizzes",
                                    "title": "Quiz1",
                                    "obtained": "8.0",
                                    "total": "10.0",
                                    "date_text": "01 Nov 2026",
                                }
                            },
                            "resources": {},
                        }
                    }
                }
            )
            tracker.run_cycle(
                client,
                [_intro_course()],
                state_path=state_path,
                base_url=BASE,
                track_announcements=False,
                notifiers=[],
            )
            diagnostics = (state_path.parent / "last_anomaly.json").read_text(encoding="utf-8")
            saved_html = (state_path.parent / "last_anomaly_marks.html").read_text(
                encoding="utf-8"
            )

        for blob in (diagnostics, saved_html):
            self.assertNotIn(self.PASSWORD_VALUE, blob)
            self.assertNotIn("INNER_SECRET_5", blob)
            self.assertNotIn("synopsisForm", blob)
            self.assertNotIn(self.SESSION_VALUE, blob)
        self.assertIn("No marks available.", diagnostics)
        self.assertIn("No marks available.", saved_html)


def snippet(result: CourseSnapshotResult) -> str:
    """Every string a snapshot result exposes, concatenated (leak assertions)."""
    return " ".join(
        [
            result.diagnostic_html,
            result.diagnostic_preview,
            " ".join(m.key + m.title + m.obtained for m in result.marks),
            " ".join(r.key + r.title + r.href for r in result.resources),
            " ".join(result.set_cookie_names),
        ]
    )


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
            course = Course(course_id="100001", code="CSC101", name="Intro")
            courses_page_html = _auth_shell(
                '<table class="table sis-dash-table"><thead><tr>'
                "<th>Course No</th><th>Course Name</th></tr></thead><tbody>"
                "<tr onclick=\"window.location='/Courses/SetCourse/100001'\">"
                "<td>CSC101</td><td>Intro</td></tr></tbody></table>"
            )

            # A real SisClient over a scripted transport: the portal answers /Courses with
            # the login page while the session is "expired", and with a real dashboard once
            # the .env update has been picked up.
            class _ExpiringPortal(StubTransport):
                dashboard_hits = 0

                def fetch(self, url, headers, timeout=None):
                    path = urllib.parse.urlparse(url).path
                    if path == "/Courses":
                        self.dashboard_hits += 1
                        if self.dashboard_hits >= 2:
                            retested.set()
                        if resumed.is_set():
                            self.add(StubResponse(courses_page_html), url=None)
                        else:
                            self.add(StubResponse(LOGIN_HTML), url=None)
                    elif path == "/Courses/SetCourse/100001":
                        self.add(StubResponse(_auth_shell("<div>ann</div>")), url=None)
                    elif path == "/MarksSummary/Index":
                        self.add(
                            StubResponse(
                                _auth_shell(
                                    '<div class="sis-marks-summary">'
                                    '<p class="sis-marks-empty">No marks</p></div>',
                                    course_code="CSC101",
                                    course_name="Intro",
                                )
                            ),
                            url=None,
                        )
                    elif path == "/LearningResources/Index":
                        self.add(
                            StubResponse(
                                _auth_shell(
                                    '<div class="sis-lr"><span class="sis-lr-count">0 resources</span></div>',
                                    course_code="CSC101",
                                    course_name="Intro",
                                )
                            ),
                            url=None,
                        )
                    else:  # pragma: no cover - unexpected route
                        raise AssertionError(f"unexpected path {path}")
                    return super().fetch(url, headers, timeout)

            portal = _ExpiringPortal()

            def _client_factory(**kwargs):
                client = SisClient(
                    kwargs["session_id"],
                    base_url=kwargs.get("base_url", BASE),
                    user_agent=kwargs.get("user_agent", ""),
                    request_delay=0.0,
                    cancel_event=kwargs.get("cancel_event"),
                    transport=portal,
                )
                real_reconfigure = client.reconfigure

                def _spy(*args, **kw):
                    reconfig_calls.append(
                        {
                            "session_id": kw.get("session_id", args[0] if args else None),
                            "reset_cookies": kw.get("reset_cookies", False),
                        }
                    )
                    return real_reconfigure(*args, **kw)

                client.reconfigure = _spy  # type: ignore[method-assign]
                return client

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
                resumed.set()
                tmp_env2 = Path(tmp) / ".env.tmp2"
                tmp_env2.write_text(env_content + "# touched twice\n", encoding="utf-8")
                os.replace(tmp_env2, env_path)
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and not state_path.exists():
                    time.sleep(0.05)
                time.sleep(0.3)
                signal.raise_signal(signal.SIGTERM)

            import signal

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_updater, daemon=True)
            t.start()
            try:
                with mock.patch.object(tracker, "SisClient", _client_factory), mock.patch.object(
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

    def test_real_client_drops_stale_cookies_after_expired_session_env_change(self) -> None:
        """End-to-end: the daemon's reset_cookies path is visible at the wire.

        A real ``SisClient`` over a scripted in-memory transport is driven by the real
        ``run_daemon`` loop. The session dies on the first ``SetCourse``; after ``.env`` is
        touched with the *same* ``SIS_SESSION_ID``, the retried ``/Courses`` request must
        carry only ``ASP.NET_SessionId`` — proving the stale auxiliary cookie set on the
        earlier authenticated response was explicitly dropped by ``reset_cookies=True``.
        """
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

            marks_html = _auth_shell(
                '<div class="sis-marks-summary"><p class="sis-marks-empty">No marks</p></div>',
                course_code="CSC101",
                course_name="Intro",
            )
            resources_html = _auth_shell(
                '<div class="sis-lr"><span class="sis-lr-count">0 resources</span></div>',
                course_code="CSC101",
                course_name="Intro",
            )

            expired = threading.Event()
            recovered = threading.Event()
            stale_jars: list[str] = []
            clean_jars: list[str] = []

            class _ScriptedTransport(PortalTransport):
                """First SetCourse redirects to Login; every later request is authenticated."""

                def __init__(self) -> None:
                    self.died = False

                def fetch(self, url, headers, timeout=None):
                    cookie = headers.get("Cookie", "")
                    path = urllib.parse.urlparse(url).path
                    if path == "/Courses":
                        if self.died:
                            if "ARRAffinity" in cookie:
                                stale_jars.append(cookie)
                            else:
                                clean_jars.append(cookie)
                                recovered.set()
                        return StubResponse(
                            COURSES_PAGE, headers={"Set-Cookie": ["ARRAffinity=node1; Path=/"]}
                        ).to_transport_response(url)
                    if path == "/Courses/SetCourse/100001":
                        if not self.died:
                            self.died = True
                            expired.set()
                            return StubResponse(
                                "", status=302, headers={"Location": "/Login/Index"}
                            ).to_transport_response(url)
                        return StubResponse(_auth_shell("<div>ann</div>")).to_transport_response(url)
                    if path == "/MarksSummary/Index":
                        return StubResponse(marks_html).to_transport_response(url)
                    if path == "/LearningResources/Index":
                        return StubResponse(resources_html).to_transport_response(url)
                    raise AssertionError(f"unexpected path {path}")

            client = SisClient(
                "reused_cookie", base_url=BASE, request_delay=0.0, transport=_ScriptedTransport()
            )

            def _client_factory(**kwargs):
                self.assertEqual(kwargs["session_id"], "reused_cookie")
                return client

            notifier = _RecordingNotifier()
            import os
            import signal

            def _updater() -> None:
                try:
                    self.assertTrue(expired.wait(timeout=10.0))
                    deadline = time.monotonic() + 10.0
                    while time.monotonic() < deadline:
                        if hb_path.exists() and "waiting_for_session" in hb_path.read_text(
                            encoding="utf-8"
                        ):
                            break
                        time.sleep(0.05)
                    tmp_env = Path(tmp) / ".env.tmp"
                    tmp_env.write_text(env_content + "# touched\n", encoding="utf-8")
                    os.replace(tmp_env, env_path)
                    self.assertTrue(recovered.wait(timeout=10.0))
                    time.sleep(0.3)
                finally:
                    signal.raise_signal(signal.SIGTERM)

            prev_int = signal.getsignal(signal.SIGINT)
            prev_term = signal.getsignal(signal.SIGTERM)
            t = threading.Thread(target=_updater, daemon=True)
            t.start()
            try:
                with mock.patch.object(tracker, "SisClient", _client_factory), mock.patch.object(
                    tracker, "build_notifiers", return_value=[notifier]
                ):
                    rc = tracker.run_daemon(env_path, state_path, heartbeat_path=hb_path)
            finally:
                signal.signal(signal.SIGINT, prev_int)
                signal.signal(signal.SIGTERM, prev_term)
                t.join(timeout=5.0)

            self.assertEqual(rc, 0)
            self.assertEqual(stale_jars, [], "stale ARRAffinity survived the explicit jar reset")
            self.assertTrue(clean_jars)
            self.assertEqual(clean_jars[0], "ASP.NET_SessionId=reused_cookie")
            self.assertIn("session_expired", [n.event for n in notifier.notes])


if __name__ == "__main__":
    unittest.main()
