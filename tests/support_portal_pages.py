"""In-memory COMSATS portal page simulator over the stub HTTP transport.

Tests that need *portal behaviour* (a course returning zero marks, a course whose
``SetCourse`` fails, a session that dies on the third dashboard fetch) build a
:class:`SimulatedPortal` and hand it to the real :class:`sis_client.SisClient`:

    portal = SimulatedPortal(courses=[c1], marks={c1.course_id: []})
    client = SisClient("sid", base_url=BASE, request_delay=0.0, transport=portal)
    scan = tracker.scan_all_courses(client, portal.courses)

The simulator is a :class:`support_stub_transport.StubTransport` subclass: it still
records every outbound request through the stub contract, but generates the response for
each request on the fly (``/Courses`` dashboard, ``/Courses/SetCourse/<id>`` course
switch, ``/MarksSummary/Index``, ``/LearningResources/Index``) so multi-tick daemon tests
do not need a pre-queued response sequence.

Escape hatches, all optional:

* ``portal.marks[cid]`` / ``portal.resources[cid]`` / ``portal.announcements[cid]`` —
  parsed items returned for that course; mutate them between cycles.
* ``portal.failures["/Courses/SetCourse/100002"] = PortalHttpError("503")`` — a queued
  exception instance is raised, a ``StubResponse`` (e.g. a login redirect) is returned.
* ``portal.expire_sessions = {"stale_cookie"}`` — any request whose ``Cookie`` header
  carries one of these session values is answered with a login redirect.
* ``portal.on_path["/Courses"] = callable`` — full override; the callable receives
  ``(path, headers, portal)`` and returns a ``StubResponse`` or exception instance.

Python 3.9+ standard library only.
"""

from __future__ import annotations

import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from sis_client import AnnouncementItem, Course, MarkItem, ResourceItem  # noqa: E402
from support_stub_transport import StubResponse, StubTransport  # noqa: E402

__all__ = [
    "auth_shell",
    "courses_page",
    "marks_page",
    "resources_page",
    "announcements_page",
    "SimulatedPortal",
    "LOGIN_PAGE",
]

LOGIN_PAGE = (
    '<html><body class="sis-login-page"><form action="/Login">'
    '<button id="LoginSubmit">Sign in</button></form></body></html>'
)


def auth_shell(inner: str, course: Optional[Course] = None) -> str:
    """Wrap page content in the authenticated portal chrome the detectors require."""
    course_hdr = ""
    if course is not None:
        course_hdr = (
            f'<div class="page_title_container"><h3>{course.name}</h3>'
            f"<h5>Course Code : {course.code} &nbsp;&nbsp; Credit Hours : 3</h5></div>"
        )
    return (
        '<html><body class="sis-portal-body">'
        '<header class="sis-portal-header">'
        '<a id="Dash_Board" href="/Courses/Index">Dashboard</a>'
        '<a id="Sign_Out" href="/Login/Sign_out">Logout</a>'
        f"{course_hdr}</header>{inner}</body></html>"
    )


def courses_page(courses: Sequence[Course], semester: str = "") -> str:
    """Dashboard HTML whose ``SetCourse`` rows parse via ``parse_courses``."""
    rows = "".join(
        f'<tr onclick="window.location=\'/Courses/SetCourse/{c.course_id}\'">'
        f"<td>{c.code}</td><td>{c.name}</td><td>{c.credits or '3'}</td>"
        f"<td>{c.teacher}</td><td>{c.section}</td><td>90%</td></tr>"
        for c in courses
    )
    semester_html = (
        f'<p class="sis-semester">Current session: {semester} . Open a course.</p>'
        if semester
        else ""
    )
    return auth_shell(
        f"{semester_html}"
        '<table class="table sis-dash-table"><thead><tr>'
        "<th>Course No</th><th>Course Name</th><th>Credits</th>"
        "<th>Teacher</th><th>Class</th><th>Attendance</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def marks_page(course: Course, marks: Iterable[MarkItem]) -> str:
    """MarksSummary HTML for ``course`` with one table row per mark.

    Row ids round-trip through ``parse_marks_summary`` so ``MarkItem.key`` survives the
    simulated fetch (the parser prefixes the section head onto the row id).
    """
    sections: Dict[str, List[MarkItem]] = {}
    for mark in marks:
        sections.setdefault(mark.section, []).append(mark)

    if not sections:
        return auth_shell(
            '<div class="sis-marks-summary"><p class="sis-marks-empty">'
            "No marks available.</p></div>",
            course=course,
        )

    blocks = []
    for section, items in sections.items():
        prefix = f"{section}:"
        rows = "".join(
            f'<tr id="{m.key[len(prefix):] if m.key.startswith(prefix) else m.key}">'
            f"<td>{m.title}</td>"
            f'<td><span class="sis-marks-score">{m.obtained}</span></td>'
            f"<td>{m.total}</td><td>{m.date_text}</td></tr>"
            for m in items
        )
        blocks.append(
            f'<div class="sis-marks-section-head">{section}</div>'
            '<table class="table table-striped sis-marks-table"><thead><tr>'
            "<th>Title</th><th>Obtained Marks</th><th>Total Marks</th><th>Date Time</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    return auth_shell(
        '<div class="sis-marks-summary table-responsive quiz_listing">'
        + "".join(blocks)
        + "</div>",
        course=course,
    )


def resources_page(course: Course, resources: Iterable[ResourceItem]) -> str:
    """LearningResources HTML for ``course`` with one row per resource."""
    items = list(resources)
    rows = "".join(
        f'<tr class="sis-lr-row" id="{r.key}">'
        '<td class="sis-lr-num">1</td>'
        f'<td class="sis-lr-title">{r.title}</td>'
        f'<td><span class="sis-lr-material">{r.material_type}</span></td>'
        f'<td class="sis-lr-date">{r.date_text}</td>'
        f'<td class="sis-lr-size">{r.size}</td>'
        f'<td class="actions"><a class="sis-lr-btn" href="{r.href}">Download</a></td>'
        "</tr>"
        for r in items
    )
    return auth_shell(
        '<div class="sis-lr">'
        f'<span class="sis-lr-count">{len(items)} resources</span>'
        '<div class="sis-lr-table-wrap"><table class="table sis-lr-table"><thead><tr>'
        "<th>#</th><th>Title</th><th>Type</th><th>Date</th><th>Size</th><th>Action</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div></div>",
        course=course,
    )


def announcements_page(course: Course, announcements: Iterable[AnnouncementItem]) -> str:
    """SetCourse response HTML carrying ``.sis-ann-item`` announcement blocks."""
    items = "".join(
        '<div class="sis-ann-item"><div class="sis-ann-item-head">'
        f"<h2>{a.title}</h2>"
        f'<ul class="sis-ann-meta"><li>{a.meta}</li></ul></div>'
        f'<div class="sis-ann-body"><p>{a.body}</p></div></div>'
        for a in announcements
    )
    return auth_shell(
        f'<div class="sis-ann announcements_box"><div class="sis-ann-list">{items}</div></div>',
        course=course,
    )


class SimulatedPortal(StubTransport):
    """Scripted COMSATS portal served through the stub HTTP transport contract."""

    def __init__(
        self,
        courses: Sequence[Course] = (),
        semester: str = "Fall 2026",
        marks: Optional[Mapping[str, Sequence[MarkItem]]] = None,
        resources: Optional[Mapping[str, Sequence[ResourceItem]]] = None,
        announcements: Optional[Mapping[str, Sequence[AnnouncementItem]]] = None,
        courses_html: Optional[str] = None,
        marks_html: Optional[Mapping[str, str]] = None,
        expire_sessions: Iterable[str] = (),
        failures: Optional[Mapping[str, Any]] = None,
        login_redirect_sets_cookie: Optional[str] = None,
        set_cookie_on_switch: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.courses: List[Course] = list(courses)
        self.semester = semester
        self.marks: Dict[str, List[MarkItem]] = {
            key: list(value) for key, value in (marks or {}).items()
        }
        self.resources: Dict[str, List[ResourceItem]] = {
            key: list(value) for key, value in (resources or {}).items()
        }
        self.announcements: Dict[str, List[AnnouncementItem]] = {
            key: list(value) for key, value in (announcements or {}).items()
        }
        self.courses_html = courses_html
        self.marks_html: Dict[str, str] = dict(marks_html or {})
        self.expire_sessions = set(expire_sessions)
        self.failures: Dict[str, Any] = dict(failures or {})
        self.login_redirect_sets_cookie = login_redirect_sets_cookie
        self.set_cookie_on_switch = set_cookie_on_switch
        self.on_path: Dict[str, Any] = {}
        self.active_course_id = ""
        self.path_counts: Counter = Counter()

    # -- helpers --------------------------------------------------------------
    def course_by_id(self, course_id: str) -> Optional[Course]:
        for course in self.courses:
            if course.course_id == course_id:
                return course
        return None

    def requests_to(self, path: str) -> int:
        return self.path_counts[path]

    @staticmethod
    def _session_cookie(headers: Mapping[str, str]) -> str:
        raw = ""
        for name, value in headers.items():
            if name.lower() == "cookie":
                raw = value
        for part in raw.split(";"):
            key, _, val = part.strip().partition("=")
            if key == "ASP.NET_SessionId":
                return val
        return ""

    def _login_redirect(self) -> StubResponse:
        headers: Dict[str, Any] = {"Location": "/Login/Index"}
        if self.login_redirect_sets_cookie:
            headers["Set-Cookie"] = [f"{self.login_redirect_sets_cookie}=node7; Path=/"]
        return StubResponse("", status=302, headers=headers)

    # -- transport interface --------------------------------------------------
    def fetch(self, url: str, headers: Mapping[str, str], timeout: Optional[float] = None):
        path = urllib.parse.urlparse(url).path
        self.path_counts[path] += 1

        override = self.on_path.get(path)
        if override is not None:
            outcome = override(path, headers, self)
        else:
            outcome = self._respond(path, headers)

        self.add(outcome, url=None)
        return super().fetch(url, headers, timeout)

    def _respond(self, path: str, headers: Mapping[str, str]):
        if self._session_cookie(headers) in self.expire_sessions:
            return self._login_redirect()

        for prefix, exc in self.failures.items():
            if path == prefix or path.startswith(prefix):
                return exc

        if path == "/Courses":
            if self.courses_html is not None:
                return StubResponse(self.courses_html)
            return StubResponse(courses_page(self.courses, self.semester))

        if path.startswith("/Courses/SetCourse/"):
            course_id = path.rsplit("/", 1)[-1]
            course = self.course_by_id(course_id)
            if course is None:
                return StubResponse("", status=404)
            self.active_course_id = course_id
            response = StubResponse(
                announcements_page(course, self.announcements.get(course_id, []))
            )
            if self.set_cookie_on_switch:
                return StubResponse(
                    response.body,
                    status=response.status,
                    headers={
                        "Set-Cookie": [f"{self.set_cookie_on_switch}=node1; Path=/"]
                    },
                    final_url=response.final_url,
                )
            return response

        if path == "/MarksSummary/Index":
            course = self.course_by_id(self.active_course_id)
            if course is None:
                return StubResponse("", status=404)
            if course.course_id in self.marks_html:
                return StubResponse(self.marks_html[course.course_id])
            return StubResponse(marks_page(course, self.marks.get(course.course_id, [])))

        if path == "/LearningResources/Index":
            course = self.course_by_id(self.active_course_id)
            if course is None:
                return StubResponse("", status=404)
            return StubResponse(
                resources_page(course, self.resources.get(course.course_id, []))
            )

        return StubResponse("", status=404)
