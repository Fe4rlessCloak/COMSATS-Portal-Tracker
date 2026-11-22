#!/usr/bin/env python3
"""
COMSATS CUOnline Student Portal HTTP client and HTML parsers.

Uses Python standard library only (urllib + html.parser) and standard
non-hardcoded MVC routes discovered from /Courses.
"""

from __future__ import annotations

import html as html_lib
import http.client
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

DEFAULT_BASE_URL = "https://sis.comsats.edu.pk"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _dedupe_names(*values: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Union of cookie names across ``values``, first-seen order, blanks dropped.

    The one ordered-dedupe helper: exceptions normalize their ``set_cookie_names``
    through it, and ``fetch_page`` widens an in-flight observation list with it.
    """
    names: List[str] = []
    for value in values:
        for name in value or ():
            if name and name not in names:
                names.append(name)
    return tuple(names)


class SessionExpiredError(RuntimeError):
    """Raised when the portal redirects to /Login or renders the login form.

    ``set_cookie_names`` carries the ``Set-Cookie`` header names observed on the response
    that proved the session dead, so mid-scan failures keep cookie diagnostics without
    callers having to reach into client state.
    """

    def __init__(self, message: str, set_cookie_names: Optional[Iterable[str]] = None) -> None:
        super().__init__(message)
        self.set_cookie_names: Tuple[str, ...] = _dedupe_names(set_cookie_names)


class PortalHttpError(RuntimeError):
    """Raised on unexpected HTTP/network errors."""

    def __init__(self, message: str, set_cookie_names: Optional[Iterable[str]] = None) -> None:
        super().__init__(message)
        self.set_cookie_names: Tuple[str, ...] = _dedupe_names(set_cookie_names)


class PortalParseError(PortalHttpError):
    """Raised when an authenticated portal page is missing expected page markup."""


class CourseMismatchError(PortalHttpError):
    """Raised when the active course header does not match the requested course."""


@dataclass(frozen=True)
class ActiveCourseInfo:
    code: str
    name: str


@dataclass(frozen=True)
class Course:
    course_id: str
    code: str
    name: str
    credits: str = ""
    teacher: str = ""
    section: str = ""

    @property
    def label(self) -> str:
        return f"{self.code} — {self.name}" if self.code and self.name else (self.name or self.course_id)


@dataclass(frozen=True)
class MarkItem:
    key: str
    section: str
    title: str
    obtained: str
    total: str
    date_text: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceItem:
    key: str
    title: str
    material_type: str
    date_text: str
    size: str
    href: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AnnouncementItem:
    key: str
    title: str
    meta: str
    body: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


@dataclass
class RosterResult:
    """Everything ``fetch_roster()`` observed on ``/Courses``.

    ``courses`` are the parsed enrolled courses, ``semester`` is the extracted semester
    label (empty when the dashboard omits it), and ``set_cookie_names`` are the
    ``Set-Cookie`` header names seen while fetching the page.
    """

    courses: List[Course] = field(default_factory=list)
    semester: str = ""
    set_cookie_names: Tuple[str, ...] = ()


@dataclass
class CourseSnapshotResult:
    """Everything ``fetch_course_snapshot()`` observed for one course.

    ``announcements`` is ``None`` when the caller did not ask for them. ``diagnostic_html``
    is already scrubbed by :func:`scrub_diagnostic_html` and ``diagnostic_preview`` by
    :func:`extract_diagnostic_text`; both are only populated when ``marks`` is empty, so
    raw portal HTML never crosses this seam.
    """

    marks: List[MarkItem] = field(default_factory=list)
    resources: List[ResourceItem] = field(default_factory=list)
    announcements: Optional[List[AnnouncementItem]] = None
    set_cookie_names: Tuple[str, ...] = ()
    diagnostic_html: str = ""
    diagnostic_preview: str = ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TransportError(RuntimeError):
    """Raised by an HTTP transport adapter when a request could not be completed at all.

    Adapters must *return* a :class:`TransportResponse` for every HTTP status they receive
    (including 3xx redirects and 4xx/5xx errors) and raise this (or an ``OSError`` /
    ``ValueError``) only for genuine transport failures such as DNS, TLS, or socket errors.
    """


@dataclass(frozen=True)
class TransportResponse:
    """One raw HTTP exchange as seen by the Portal module.

    ``headers`` is any object the Portal module can read headers from: an
    ``email.message.Message`` (production), or a plain mapping whose values may be a
    string or a sequence of strings (multi-value ``Set-Cookie``).
    """

    status: int
    headers: Any
    body: bytes = b""
    final_url: str = ""

    def header(self, name: str, default: str = "") -> str:
        headers = self.headers
        if headers is None:
            return default
        getter = getattr(headers, "get", None)
        if getter is not None:
            try:
                value = getter(name)
            except TypeError:
                value = None
            if value is None:
                value = getter(name.lower())
            if isinstance(value, (list, tuple)):
                value = value[-1] if value else None
            if value is not None:
                return str(value)
        for key, value in getattr(headers, "items", lambda: [])():
            if str(key).lower() == name.lower():
                if isinstance(value, (list, tuple)):
                    value = value[-1] if value else ""
                return str(value)
        return default

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")

    @property
    def set_cookie_headers(self) -> Tuple[str, ...]:
        return tuple(_get_set_cookie_headers(self.headers))

    @property
    def set_cookie_names(self) -> Tuple[str, ...]:
        """Names of every ``Set-Cookie`` header observed on this response, in order."""
        names: List[str] = []
        for raw_header in self.set_cookie_headers:
            parsed = _parse_set_cookie(raw_header)
            if parsed is None:
                continue
            name = _canonical_cookie_name(parsed[0])
            if name not in names:
                names.append(name)
        return tuple(names)


class PortalTransport:
    """Minimal HTTP transport interface the Portal module depends on.

    Implementations must be stateless with respect to cookies: they send exactly the headers
    they are given and return the raw response. Cookie staging, redirect following, login
    detection, and error translation all stay inside :class:`SisClient`.
    """

    def fetch(
        self, url: str, headers: Mapping[str, str], timeout: Optional[float] = None
    ) -> TransportResponse:  # pragma: no cover - interface declaration
        raise NotImplementedError


class UrllibTransport(PortalTransport):
    """Production adapter: stdlib ``urllib`` with redirect following disabled."""

    def __init__(self, opener: Optional[Any] = None) -> None:
        self._opener = opener if opener is not None else urllib.request.build_opener(_NoRedirect())

    def fetch(
        self, url: str, headers: Mapping[str, str], timeout: Optional[float] = None
    ) -> TransportResponse:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return TransportResponse(
                    status=int(getattr(response, "status", 200) or 200),
                    headers=getattr(response, "headers", None),
                    body=response.read() or b"",
                    final_url=response.geturl() or url,
                )
        except urllib.error.HTTPError as error:
            body = b""
            try:
                body = error.read() or b""
            except Exception:  # pragma: no cover - best-effort error body
                body = b""
            finally:
                error.close()
            return TransportResponse(
                status=int(error.code),
                headers=error.headers,
                body=body,
                final_url=error.geturl() or url,
            )


@dataclass(frozen=True)
class PortalResponse:
    """A portal page as returned by :meth:`SisClient.fetch_page`.

    ``set_cookie_names`` is what :mod:`tracker` records for anomaly diagnostics; failures
    carry the same names on :class:`SessionExpiredError` / :class:`PortalHttpError`.
    """

    url: str
    final_url: str
    status: int
    body: str
    set_cookie_names: Tuple[str, ...] = ()


def _clean_text(parts: List[str]) -> str:
    return " ".join("".join(parts).split())


def _attr_dict(attrs: List[Tuple[str, Optional[str]]]) -> Dict[str, str]:
    return {k.lower(): (v or "") for k, v in attrs}


def _has_class(attrs: Dict[str, str], cls_name: str) -> bool:
    return cls_name in attrs.get("class", "").split()


class _CoursesParser(HTMLParser):
    """Parses table.sis-dash-table rows with /Courses/SetCourse/<id> onclick handlers."""

    def __init__(self) -> None:
        super().__init__()
        self.courses: List[Course] = []
        self._in_dash_table = False
        self._table_depth = 0
        self._skip_depth = 0
        self._cur_course_id: Optional[str] = None
        self._in_td = False
        self._td_buf: List[str] = []
        self._row_cells: List[str] = []

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs = _attr_dict(attrs_list)

        if t in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if t == "table":
            if self._in_dash_table:
                self._table_depth += 1
            elif _has_class(attrs, "sis-dash-table"):
                self._in_dash_table = True
                self._table_depth = 1
            return

        if not self._in_dash_table:
            return

        if t == "tr":
            onclick = attrs.get("onclick", "")
            m = re.search(r"/Courses/SetCourse/(\d+)", onclick)
            if m:
                self._cur_course_id = m.group(1)
                self._row_cells = []
        elif t == "td" and self._cur_course_id is not None:
            self._in_td = True
            self._td_buf = []
        elif t == "a" and self._cur_course_id is None:
            href = attrs.get("href", "")
            m = re.search(r"/Courses/SetCourse/(\d+)", href)
            if m:
                self._cur_course_id = m.group(1)
                self._row_cells = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if t == "table" and self._in_dash_table:
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._in_dash_table = False
            return

        if not self._in_dash_table:
            return

        if t == "td" and self._in_td:
            self._row_cells.append(_clean_text(self._td_buf))
            self._in_td = False
        elif t == "tr" and self._cur_course_id is not None:
            cells = self._row_cells
            if cells:
                self.courses.append(
                    Course(
                        course_id=self._cur_course_id,
                        code=cells[0] if len(cells) > 0 else "",
                        name=cells[1] if len(cells) > 1 else "",
                        credits=cells[2] if len(cells) > 2 else "",
                        teacher=cells[3] if len(cells) > 3 else "",
                        section=cells[4] if len(cells) > 4 else "",
                    )
                )
            self._cur_course_id = None
            self._row_cells = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_td:
            self._td_buf.append(data)


class _MarksParser(HTMLParser):
    """Parses .sis-marks-section-head + table.sis-marks-table on /MarksSummary/Index."""

    def __init__(self) -> None:
        super().__init__()
        self.items: List[MarkItem] = []
        self._skip_depth = 0
        self._current_section = "Marks"
        self._in_section_head = False
        self._head_buf: List[str] = []

        self._in_table = False
        self._table_depth = 0
        self._in_tbody = False
        self._in_thead = False
        self._headers: List[str] = []

        self._cur_row_id = ""
        self._in_tr = False
        self._in_cell = False
        self._cell_tag = ""
        self._cell_buf: List[str] = []
        self._cells: List[str] = []

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs = _attr_dict(attrs_list)

        if t in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if _has_class(attrs, "sis-marks-section-head"):
            self._in_section_head = True
            self._head_buf = []
            return

        if t == "table":
            if self._in_table:
                self._table_depth += 1
            elif _has_class(attrs, "sis-marks-table"):
                self._in_table = True
                self._table_depth = 1
                self._headers = []
            return

        if not self._in_table:
            return

        if t == "thead":
            self._in_thead = True
        elif t == "tbody":
            self._in_tbody = True
        elif t == "tr":
            self._in_tr = True
            self._cur_row_id = attrs.get("id", "").strip()
            self._cells = []
        elif t in ("th", "td") and self._in_tr:
            self._in_cell = True
            self._cell_tag = t
            self._cell_buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if t == "div" and self._in_section_head:
            sec = _clean_text(self._head_buf)
            if sec:
                self._current_section = sec
            self._in_section_head = False
            return

        if t == "table" and self._in_table:
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._in_table = False
                self._in_thead = False
                self._in_tbody = False
            return

        if not self._in_table:
            return

        if t == "thead":
            self._in_thead = False
        elif t == "tbody":
            self._in_tbody = False
        elif t in ("th", "td") and self._in_cell:
            text = _clean_text(self._cell_buf)
            if self._in_thead or self._cell_tag == "th":
                self._headers.append(text.lower())
            else:
                self._cells.append(text)
            self._in_cell = False
        elif t == "tr" and self._in_tr:
            if not self._in_thead and self._cells:
                title = self._cells[0] if len(self._cells) > 0 else ""
                obtained = self._cells[1] if len(self._cells) > 1 else ""
                total = self._cells[2] if len(self._cells) > 2 else ""
                date_text = self._cells[3] if len(self._cells) > 3 else ""

                # Map by header name if column order differs
                for idx, h in enumerate(self._headers):
                    if idx >= len(self._cells):
                        continue
                    if "title" in h:
                        title = self._cells[idx]
                    elif "obtained" in h:
                        obtained = self._cells[idx]
                    elif "total" in h:
                        total = self._cells[idx]
                    elif "date" in h:
                        date_text = self._cells[idx]

                if title:
                    row_key = self._cur_row_id or f"{self._current_section}:{title}"
                    self.items.append(
                        MarkItem(
                            key=f"{self._current_section}:{row_key}",
                            section=self._current_section,
                            title=title,
                            obtained=obtained,
                            total=total,
                            date_text=date_text,
                        )
                    )
            self._in_tr = False
            self._cur_row_id = ""

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_section_head:
            self._head_buf.append(data)
        if self._in_cell:
            self._cell_buf.append(data)


class _LearningResourcesParser(HTMLParser):
    """Parses table.sis-lr-table (or fallback tables inside .sis-lr) on /LearningResources/Index."""

    def __init__(self) -> None:
        super().__init__()
        self.items: List[ResourceItem] = []
        self._skip_depth = 0
        self._in_lr_root = False
        self._lr_depth = 0

        self._in_table = False
        self._table_depth = 0
        self._in_thead = False
        self._headers: List[str] = []

        self._in_tr = False
        self._cur_row_id = ""
        self._cur_row_onclick = ""
        self._in_cell = False
        self._cell_tag = ""
        self._cell_classes = ""
        self._cell_buf: List[str] = []
        self._cells: List[Tuple[str, str]] = []  # (classes, text)
        self._row_hrefs: List[str] = []

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs = _attr_dict(attrs_list)

        if t in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if t == "div":
            if self._in_lr_root:
                self._lr_depth += 1
            elif _has_class(attrs, "sis-lr"):
                self._in_lr_root = True
                self._lr_depth = 1

        if t == "table":
            if self._in_table:
                self._table_depth += 1
            elif _has_class(attrs, "sis-lr-table") or self._in_lr_root:
                self._in_table = True
                self._table_depth = 1
                self._headers = []
            return

        if not self._in_table:
            return

        if t == "thead":
            self._in_thead = True
        elif t == "tr":
            self._in_tr = True
            self._cur_row_id = attrs.get("id", "").strip()
            self._cur_row_onclick = attrs.get("onclick", "")
            self._cells = []
            self._row_hrefs = []
        elif t in ("th", "td") and self._in_tr:
            self._in_cell = True
            self._cell_tag = t
            self._cell_classes = attrs.get("class", "")
            self._cell_buf = []
        elif t == "a" and self._in_tr:
            href = attrs.get("href", "").strip()
            if href and not href.startswith("#") and not href.startswith("javascript:"):
                self._row_hrefs.append(href)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if t == "div" and self._in_lr_root:
            self._lr_depth -= 1
            if self._lr_depth <= 0:
                self._in_lr_root = False

        if t == "table" and self._in_table:
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._in_table = False
                self._in_thead = False
            return

        if not self._in_table:
            return

        if t == "thead":
            self._in_thead = False
        elif t in ("th", "td") and self._in_cell:
            text = _clean_text(self._cell_buf)
            if self._in_thead or self._cell_tag == "th":
                self._headers.append(text.lower())
            else:
                self._cells.append((self._cell_classes, text))
            self._in_cell = False
        elif t == "tr" and self._in_tr:
            if not self._in_thead and self._cells:
                self.items.append(self._build_item())
            self._in_tr = False

    def _build_item(self) -> ResourceItem:
        title = ""
        material_type = ""
        date_text = ""
        size = ""

        for cls, text in self._cells:
            tokens = cls.split()
            if "sis-lr-title" in tokens:
                title = text
            elif "sis-lr-material" in tokens:
                material_type = text
            elif "sis-lr-date" in tokens:
                date_text = text
            elif "sis-lr-size" in tokens:
                size = text

        # Header-based fallback if cell classes differ
        for idx, h in enumerate(self._headers):
            if idx >= len(self._cells):
                continue
            val = self._cells[idx][1]
            if not title and ("title" in h or "topic" in h or "name" in h):
                title = val
            elif not material_type and ("material" in h or "type" in h or "category" in h):
                material_type = val
            elif not date_text and "date" in h:
                date_text = val
            elif not size and "size" in h:
                size = val

        # Positional fallback
        if not title:
            texts = [t for _, t in self._cells if t and not t.isdigit()]
            title = texts[0] if texts else (self._cells[0][1] if self._cells else "Untitled resource")

        href = self._row_hrefs[0] if self._row_hrefs else ""
        if not href and self._cur_row_onclick:
            m = re.search(r"""['"]([^'"]+)['"]""", self._cur_row_onclick)
            if m:
                href = m.group(1)

        key = self._cur_row_id or href or f"{title}|{material_type}|{date_text}"
        return ResourceItem(
            key=key,
            title=title,
            material_type=material_type,
            date_text=date_text,
            size=size,
            href=href,
        )

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_cell:
            self._cell_buf.append(data)


class _AnnouncementsParser(HTMLParser):
    """Parses .sis-ann-item blocks on /Announcements / SetCourse response."""

    def __init__(self) -> None:
        super().__init__()
        self.items: List[AnnouncementItem] = []
        self._skip_depth = 0
        self._in_item = False
        self._item_depth = 0
        self._in_h2 = False
        self._in_meta = False
        self._in_body = False
        self._h2_buf: List[str] = []
        self._meta_buf: List[str] = []
        self._body_buf: List[str] = []

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs = _attr_dict(attrs_list)

        if t in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if t == "div" or t == "article":
            if self._in_item:
                self._item_depth += 1
                if _has_class(attrs, "sis-ann-body"):
                    self._in_body = True
            elif _has_class(attrs, "sis-ann-item"):
                self._in_item = True
                self._item_depth = 1
                self._h2_buf = []
                self._meta_buf = []
                self._body_buf = []
        elif self._in_item:
            if t == "h2":
                self._in_h2 = True
            elif t == "ul" and _has_class(attrs, "sis-ann-meta"):
                self._in_meta = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth or not self._in_item:
            return

        if t == "h2":
            self._in_h2 = False
        elif t == "ul" and self._in_meta:
            self._in_meta = False
        elif t in ("div", "article"):
            if self._in_body:
                self._in_body = False
            self._item_depth -= 1
            if self._item_depth <= 0:
                title = _clean_text(self._h2_buf)
                meta = _clean_text(self._meta_buf)
                body = _clean_text(self._body_buf)
                if title or body:
                    key = f"{title}|{meta}"
                    self.items.append(AnnouncementItem(key=key, title=title, meta=meta, body=body))
                self._in_item = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._in_item:
            return
        if self._in_h2:
            self._h2_buf.append(data)
        elif self._in_meta:
            self._meta_buf.append(data)
        elif self._in_body:
            self._body_buf.append(data)


class _ActiveCourseParser(HTMLParser):
    """Parses active course code and name from .page_title_container."""

    def __init__(self) -> None:
        super().__init__()
        self.found_container = False
        self.name = ""
        self.code = ""
        self._skip_depth = 0
        self._in_container = False
        self._container_depth = 0
        self._in_h3 = False
        self._in_h5 = False
        self._buf: List[str] = []

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attrs = _attr_dict(attrs_list)

        if t in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if t == "div":
            if self._in_container:
                self._container_depth += 1
            elif _has_class(attrs, "page_title_container"):
                self._in_container = True
                self._container_depth = 1
                self.found_container = True
            return

        if not self._in_container:
            return

        if t == "h3":
            self._in_h3 = True
            self._buf = []
        elif t == "h5":
            self._in_h5 = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if t == "div" and self._in_container:
            self._container_depth -= 1
            if self._container_depth <= 0:
                self._in_container = False
            return

        if not self._in_container:
            return

        if t == "h3" and self._in_h3:
            text = _clean_text(self._buf)
            if text and not self.name:
                self.name = text
            self._in_h3 = False
        elif t == "h5" and self._in_h5:
            text = _clean_text(self._buf)
            m = re.search(r"Course\s*Code\s*:\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
            if m and not self.code:
                self.code = m.group(1).strip()
            self._in_h5 = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._in_container:
            return
        if self._in_h3 or self._in_h5:
            self._buf.append(data)


def parse_courses(html: str) -> List[Course]:
    p = _CoursesParser()
    p.feed(html)
    return p.courses


def parse_marks_summary(html: str) -> List[MarkItem]:
    p = _MarksParser()
    p.feed(html)
    return p.items


def parse_learning_resources(html: str) -> List[ResourceItem]:
    p = _LearningResourcesParser()
    p.feed(html)
    return p.items


def parse_announcements(html: str) -> List[AnnouncementItem]:
    p = _AnnouncementsParser()
    p.feed(html)
    return p.items


def parse_active_course(html: str) -> Optional[ActiveCourseInfo]:
    p = _ActiveCourseParser()
    p.feed(html)
    if p.found_container and (p.code or p.name):
        return ActiveCourseInfo(code=p.code, name=p.name)
    return None


_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|h[1-6]|li|tr|td|th|br|header|section|article)\b[^>]*>",
    re.IGNORECASE,
)
_CURRENT_SESSION_RE = re.compile(
    r"current\s+session\s*:\s*([^\n\r.<]{2,60})",
    re.IGNORECASE,
)
_TERM_YEAR_RE = re.compile(
    r"\b((?:Fall|Spring|Summer)\s+20\d{2})\b",
    re.IGNORECASE,
)


def parse_semester(html: str) -> str:
    """Extract the active academic semester label (e.g. 'Fall 2026') from /Courses HTML."""
    if not html:
        return ""
    text = _strip_script_and_style(html)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)

    m = _CURRENT_SESSION_RE.search(text)
    if m:
        candidate = m.group(1).strip(" .,:;-|")
        if candidate:
            return candidate

    m_term = _TERM_YEAR_RE.search(text)
    if m_term:
        parts = m_term.group(1).split()
        if len(parts) == 2:
            return f"{parts[0].capitalize()} {parts[1]}"
    return ""


def _norm_course_code(val: str) -> str:
    return re.sub(r"\s+", "", val).upper()


def _norm_course_name(val: str) -> str:
    return " ".join(val.split()).lower()


def verify_active_course(html: str, course: Course) -> None:
    """Verify the active course header in HTML matches the requested Course."""
    active = parse_active_course(html)
    if active is None:
        raise CourseMismatchError(
            f"Missing active course header while expecting {course.code or course.course_id} ({course.course_id})."
        )
    expected_code = course.code.strip()
    if expected_code:
        if not active.code or _norm_course_code(active.code) != _norm_course_code(expected_code):
            raise CourseMismatchError(
                f"Active course mismatch: expected {expected_code} ({course.course_id}), "
                f"got {active.code or active.name!r}."
            )
    elif course.name.strip():
        if not active.name or _norm_course_name(active.name) != _norm_course_name(course.name):
            raise CourseMismatchError(
                f"Active course mismatch: expected {course.name!r} ({course.course_id}), "
                f"got {active.name or active.code!r}."
            )


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"""([A-Za-z_:][A-Za-z0-9_.:-]*)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
)


def _strip_script_and_style(html: str) -> str:
    return _SCRIPT_STYLE_RE.sub("", html)


def _parse_tag_attrs(attr_str: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(attr_str):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        attrs[key] = val
    return attrs


def is_login_url(url: str) -> bool:
    """Return True if a URL points to a portal login route."""
    parsed = urllib.parse.urlparse(url.strip())
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if path == "/login" or path.startswith("/login/") or "/account/login" in path:
        return True
    if path in ("", "/") and "returnurl=" in query:
        return True
    return False


def has_authenticated_shell(html: str) -> bool:
    """Check for authenticated portal shell markers outside script/style blocks."""
    stripped = _strip_script_and_style(html)
    lowered = stripped.lower()
    if re.search(r"<header\b[^>]*\bsis-portal-header\b", stripped, re.IGNORECASE):
        return True
    if "/login/sign_out" in lowered:
        return True
    if 'id="sign_out"' in lowered or "id='sign_out'" in lowered:
        return True
    if 'id="dash_board"' in lowered or "id='dash_board'" in lowered:
        return True
    if re.search(r"""class\s*=\s*["'][^"']*\bsis-welcome\b""", stripped, re.IGNORECASE):
        return True
    return False


def detect_login_page(html: str, final_url: str = "") -> Optional[str]:
    """Return a reason string if the response looks like a login/unauthenticated page, else None."""
    if final_url and is_login_url(final_url):
        return f"Final URL is login page ({final_url})."

    if re.search(r"""id\s*=\s*["'](?:LoginSubmit|CaptchaMount)["']""", html, re.IGNORECASE):
        return "Portal returned the login page."

    if re.search(r"""<body\b[^>]*\bclass\s*=\s*["'][^"']*\bsis-login-page\b""", html, re.IGNORECASE):
        return "Portal returned a login page body."

    for m in re.finditer(r"<meta\b([^>]*)>", html, re.IGNORECASE):
        attrs = _parse_tag_attrs(m.group(1))
        if attrs.get("http-equiv", "").strip().lower() == "refresh":
            content = attrs.get("content", "")
            url_match = re.search(r"""url\s*=\s*['"]?([^'";\s>]+)""", content, re.IGNORECASE)
            if url_match:
                target = urllib.parse.urljoin("https://portal/", url_match.group(1).strip())
                if is_login_url(target):
                    return f"Meta refresh redirects to login ({url_match.group(1).strip()})."

    if re.search(
        r"""(?:window\.)?location(?:\.href)?\s*=\s*['"][^'"]*(?:/login(?:/index)?|/account/login)(?:['"?#])|location\.(?:replace|assign)\(\s*['"][^'"]*(?:/login(?:/index)?|/account/login)(?:['"?#])""",
        html,
        re.IGNORECASE,
    ):
        return "JavaScript redirects to login page."

    stripped = _strip_script_and_style(html)
    for m in re.finditer(r"<form\b([^>]*)>", stripped, re.IGNORECASE):
        attrs = _parse_tag_attrs(m.group(1))
        if attrs.get("id", "").strip().lower() == "synopsisform":
            continue
        action = attrs.get("action", "").strip()
        if action:
            parsed = urllib.parse.urlparse(action)
            norm_path = "/" + parsed.path.lstrip("/") if parsed.path else ""
            if re.match(r"^/(?:login(?:/index)?|account/login)$", norm_path, re.IGNORECASE):
                return f"Portal rendered a login form (action={action})."

    if not has_authenticated_shell(html):
        return "Portal response is missing authenticated shell markers (possible login or error page)."

    return None


def has_courses_page_marker(html: str) -> bool:
    stripped = _strip_script_and_style(html)
    lowered = stripped.lower()
    return (
        "sis-dash-table" in lowered
        or 'id="registeredcourses"' in lowered
        or "id='registeredcourses'" in lowered
        or bool(re.search(r"""class\s*=\s*["'][^"']*\bsis-dash\b""", stripped, re.IGNORECASE))
    )


def has_marks_page_marker(html: str) -> bool:
    stripped = _strip_script_and_style(html)
    return bool(re.search(r"\b(?:sis-marks-summary|sis-marks-table|sis-marks-empty)\b", stripped, re.IGNORECASE))


def has_resources_page_marker(html: str) -> bool:
    stripped = _strip_script_and_style(html)
    return bool(re.search(r"\b(?:sis-lr|sis-lr-table|sis-lr-count)\b", stripped, re.IGNORECASE))


_SYNOPSIS_FORM_RE = re.compile(
    r"""<form\b[^>]*\bid\s*=\s*(?:"synopsisform"|'synopsisform'|synopsisform\b)[^>]*>.*?</form>""",
    re.IGNORECASE | re.DOTALL,
)
_PASSWORD_INPUT_RE = re.compile(
    r"""<input\b[^>]*\btype\s*=\s*(?:"password"|'password'|password\b)[^>]*>""",
    re.IGNORECASE,
)
_CONTENT_AREA_RE = re.compile(
    r"""<div\b[^>]*\bclass\s*=\s*["'][^"']*\bcontent_area\b[^"']*["'][^>]*>""",
    re.IGNORECASE,
)


def scrub_diagnostic_html(html: str, max_chars: int = 65536) -> str:
    """Strip portal header/credential forms and return bounded diagnostic content HTML."""
    if not html:
        return ""
    cleaned = _SYNOPSIS_FORM_RE.sub("", html)
    cleaned = _PASSWORD_INPUT_RE.sub("<!-- password input redacted -->", cleaned)

    content_match = _CONTENT_AREA_RE.search(cleaned)
    if content_match:
        cleaned = cleaned[content_match.start() :]
    else:
        header_end = re.search(r"</header>", cleaned, re.IGNORECASE)
        if header_end:
            cleaned = cleaned[header_end.end() :]

    footer_start = re.search(r"<footer\b", cleaned, re.IGNORECASE)
    if footer_start:
        cleaned = cleaned[: footer_start.start()]

    cleaned = cleaned.strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]
    return cleaned


def extract_diagnostic_text(html: str, max_chars: int = 500) -> str:
    """Return a compact, credential-free plain-text snippet of portal content HTML."""
    scrubbed = scrub_diagnostic_html(html)
    if not scrubbed:
        return ""
    text_only = _strip_script_and_style(scrubbed)
    text_only = re.sub(r"<!--.*?-->", " ", text_only, flags=re.DOTALL)
    text_only = re.sub(r"<[^>]+>", " ", text_only)
    collapsed = " ".join(html_lib.unescape(text_only).split())
    if max_chars > 0 and len(collapsed) > max_chars:
        if max_chars <= 3:
            return collapsed[:max_chars]
        return collapsed[: max_chars - 3].rstrip() + "..."
    return collapsed


def _get_set_cookie_headers(headers: Any) -> List[str]:
    """Safely extract all Set-Cookie header strings from HTTPMessage, dict, or None."""
    if headers is None:
        return []
    if hasattr(headers, "get_all"):
        vals = headers.get_all("Set-Cookie")
        return [str(v) for v in vals if v] if vals else []
    results: List[str] = []
    if hasattr(headers, "items"):
        for k, v in headers.items():
            if str(k).lower() == "set-cookie" and v:
                if isinstance(v, (list, tuple)):
                    results.extend(str(item) for item in v if item)
                else:
                    results.append(str(v))
        return results
    if hasattr(headers, "get"):
        val = headers.get("Set-Cookie") or headers.get("set-cookie")
        if isinstance(val, (list, tuple)):
            return [str(item) for item in val if item]
        if val:
            return [str(val)]
    return []


def _parse_set_cookie(header_val: str) -> Optional[Tuple[str, str, bool]]:
    """Parse a Set-Cookie header into (name, value, expired)."""
    parts = [p.strip() for p in header_val.split(";")]
    if not parts or "=" not in parts[0]:
        return None
    name, val = parts[0].split("=", 1)
    name = name.strip()
    val = val.strip()
    if not name:
        return None

    expired = val == ""
    for attr in parts[1:]:
        if "=" not in attr:
            continue
        ak, av = attr.split("=", 1)
        ak = ak.strip().lower()
        av = av.strip()
        if ak == "max-age":
            try:
                if int(av) <= 0:
                    expired = True
            except ValueError:
                pass
        elif ak == "expires":
            if any(int(y) < 2000 for y in re.findall(r"\b(\d{4})\b", av)):
                expired = True
    return name, val, expired


def _canonical_cookie_name(name: str) -> str:
    return "ASP.NET_SessionId" if name.lower() == "asp.net_sessionid" else name


def _stage_set_cookies(
    headers: Any, pending: Dict[str, Tuple[str, bool]], seen_names: List[str]
) -> None:
    for raw_hdr in _get_set_cookie_headers(headers):
        parsed = _parse_set_cookie(raw_hdr)
        if parsed is None:
            continue
        raw_name, val, expired = parsed
        name = _canonical_cookie_name(raw_name)
        pending[name] = (val, expired)
        if name not in seen_names:
            seen_names.append(name)


def _is_safe_redirect_target(origin_url: str, target_url: str) -> bool:
    """Return True if target_url keeps the same host/port and does not downgrade HTTPS to HTTP."""
    try:
        orig = urllib.parse.urlparse(origin_url.strip())
        dest = urllib.parse.urlparse(target_url.strip())
        orig_scheme = orig.scheme.lower()
        dest_scheme = dest.scheme.lower()
        if dest_scheme not in ("http", "https"):
            return False
        if orig_scheme == "https" and dest_scheme != "https":
            return False
        if not orig.hostname or not dest.hostname:
            return False
        if orig.hostname.lower() != dest.hostname.lower():
            return False
        orig_port = orig.port or (443 if orig_scheme == "https" else 80)
        dest_port = dest.port or (443 if dest_scheme == "https" else 80)
        return orig_port == dest_port
    except ValueError:
        return False


def _sanitize_header_value(val: str) -> str:
    """Strip CR/LF and non-Latin-1 characters so outbound headers cannot raise ValueError."""
    cleaned = val.replace("\r", "").replace("\n", "")
    return cleaned.encode("latin-1", errors="ignore").decode("latin-1").strip()


class SisClient:
    """HTTP client for authenticated COMSATS Student Portal requests.

    The network edge is a single injectable :class:`PortalTransport` adapter. Everything
    protocol-shaped stays in here: header sanitization, cookie-header construction, cookie
    staging across redirect hops, committing cookies only on authenticated non-login
    responses (never clearing ``ASP.NET_SessionId``), same-origin/no-downgrade redirect
    safety, login-page detection, cooperative cancellation, and error translation.

    Connection settings are immutable from the outside: use :meth:`reconfigure` (which the
    daemon calls on ``.env`` hot-reload) instead of assigning ``session_id``/``base_url``/
    ``user_agent``/``request_delay``.
    """

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
    _MAX_HOPS = 4

    def __init__(
        self,
        session_id: str,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        request_delay: float = 0.6,
        timeout: int = 20,
        cancel_event: Optional[threading.Event] = None,
        transport: Optional[PortalTransport] = None,
    ) -> None:
        self._session_id = ""
        self._base_url = ""
        self._user_agent = DEFAULT_USER_AGENT
        self._request_delay = 0.0
        self.timeout: int = 20
        self.cancel_event: Optional[threading.Event] = None
        self._cookies: Dict[str, str] = {}
        self.reconfigure(
            session_id=session_id,
            base_url=base_url,
            user_agent=user_agent,
            request_delay=request_delay,
            timeout=timeout,
            cancel_event=cancel_event,
        )
        self.transport: PortalTransport = transport if transport is not None else UrllibTransport()
        self._last_request_ts = 0.0

    # -- connection settings --------------------------------------------------
    @property
    def session_id(self) -> str:
        """The session cookie currently in use (read-only; see :meth:`reconfigure`)."""
        return self._session_id

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @property
    def request_delay(self) -> float:
        return self._request_delay

    def reconfigure(
        self,
        session_id: Optional[str] = None,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_delay: Optional[float] = None,
        timeout: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
        reset_cookies: bool = False,
    ) -> bool:
        """Update connection settings in one explicit operation.

        The cookie jar is reset only when the session identifier actually changes or
        ``reset_cookies=True`` is passed (the daemon's expired-session ``.env`` change path).
        Returns True when the jar was reset, so callers can log the effect.
        """
        session_changed = session_id is not None and session_id != self._session_id
        if session_id is not None:
            self._session_id = session_id
        if base_url is not None:
            self._base_url = base_url.rstrip("/")
        if user_agent is not None:
            self._user_agent = user_agent or DEFAULT_USER_AGENT
        if request_delay is not None:
            self._request_delay = max(0.0, request_delay)
        if timeout is not None:
            self.timeout = timeout
        if cancel_event is not None:
            self.cancel_event = cancel_event

        if session_changed or reset_cookies:
            self._cookies = {"ASP.NET_SessionId": self._session_id} if self._session_id else {}
            return True
        return False

    # -- cookies -------------------------------------------------------------
    def _build_cookie_header(self, pending: Optional[Dict[str, Tuple[str, bool]]] = None) -> str:
        merged = dict(self._cookies)
        if self._session_id and "ASP.NET_SessionId" not in merged:
            merged["ASP.NET_SessionId"] = self._session_id
        if pending:
            for name, (val, expired) in pending.items():
                if expired:
                    if name != "ASP.NET_SessionId":
                        merged.pop(name, None)
                else:
                    merged[name] = val
        return "; ".join(f"{k}={v}" for k, v in merged.items() if v != "")

    def _commit_cookies(self, pending: Dict[str, Tuple[str, bool]]) -> None:
        if not pending:
            return
        changes: List[str] = []
        for name, (val, expired) in pending.items():
            if name == "ASP.NET_SessionId":
                if expired or not val:
                    print(
                        "[WARN] Portal attempted to clear ASP.NET_SessionId on an authenticated "
                        "response; preserving current session ID."
                    )
                    continue
                if self._cookies.get(name) != val:
                    self._cookies[name] = val
                    self._session_id = val
                    changes.append(f"{name} (rotated)")
            else:
                if expired:
                    if name in self._cookies:
                        del self._cookies[name]
                        changes.append(f"{name} (cleared)")
                elif name not in self._cookies:
                    self._cookies[name] = val
                    changes.append(f"{name} (added)")
                elif self._cookies[name] != val:
                    self._cookies[name] = val
                    changes.append(f"{name} (updated)")
        if changes:
            print(f"[INFO] Portal Set-Cookie update: {', '.join(changes)}")

    # -- transport loop ------------------------------------------------------
    def _throttle(self) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            raise PortalHttpError("Request cancelled.")
        if self._request_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self._request_delay:
            remaining = self._request_delay - elapsed
            if self.cancel_event is not None:
                self.cancel_event.wait(remaining)
                if self.cancel_event.is_set():
                    raise PortalHttpError("Request cancelled.")
            else:
                time.sleep(remaining)

    def _request_headers(self, pending: Dict[str, Tuple[str, bool]]) -> Dict[str, str]:
        headers = {
            "User-Agent": _sanitize_header_value(self._user_agent) or DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": _sanitize_header_value(f"{self._base_url}/") or f"{DEFAULT_BASE_URL}/",
        }
        cookie_header = self._build_cookie_header(pending)
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def fetch_page(self, path: str) -> PortalResponse:
        """Fetch an authenticated portal page, following safe redirects.

        Raises :class:`SessionExpiredError` when the portal redirects to Login or renders
        the login form, and :class:`PortalHttpError` for unsafe redirects, non-2xx statuses,
        and transport failures. Both carry ``set_cookie_names`` observed on the way.
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        pending: Dict[str, Tuple[str, bool]] = {}
        seen_names: List[str] = []

        for _ in range(self._MAX_HOPS):
            self._throttle()
            try:
                response = self.transport.fetch(
                    url, self._request_headers(pending), self.timeout
                )
            except (
                TransportError,
                urllib.error.URLError,
                TimeoutError,
                http.client.HTTPException,
                OSError,
                ValueError,
            ) as exc:
                raise PortalHttpError(
                    f"HTTP/network error fetching {url}: {exc}", set_cookie_names=seen_names
                ) from exc

            self._last_request_ts = time.monotonic()
            final_url = response.final_url or url
            location = response.header("Location")

            if response.status in self._REDIRECT_STATUSES:
                next_url = urllib.parse.urljoin(url, location) if location else ""
                if location and (is_login_url(next_url) or "login" in location.lower()):
                    names = _dedupe_names(seen_names, response.set_cookie_names)
                    raise SessionExpiredError(
                        f"Redirected to login ({location}).", set_cookie_names=names
                    )
                if location:
                    if not _is_safe_redirect_target(self._base_url, next_url):
                        raise PortalHttpError(
                            f"Refusing unsafe redirect from {url} to {next_url}",
                            set_cookie_names=_dedupe_names(seen_names, response.set_cookie_names),
                        )
                    _stage_set_cookies(response.headers, pending, seen_names)
                    url = next_url
                    continue
                raise PortalHttpError(
                    f"HTTP {response.status} fetching {url}",
                    set_cookie_names=_dedupe_names(seen_names, response.set_cookie_names),
                )

            if not 200 <= response.status < 300:
                raise PortalHttpError(
                    f"HTTP {response.status} fetching {url}",
                    set_cookie_names=_dedupe_names(seen_names, response.set_cookie_names),
                )

            body = response.text()
            login_reason = detect_login_page(body, final_url=final_url)
            if login_reason:
                # A login response must never mutate the jar or the session identifier; the
                # cookie names are still reported for diagnostics.
                names = _dedupe_names(seen_names, response.set_cookie_names)
                raise SessionExpiredError(login_reason, set_cookie_names=names)

            _stage_set_cookies(response.headers, pending, seen_names)
            self._commit_cookies(pending)
            names = tuple(seen_names)
            return PortalResponse(
                url=url,
                final_url=final_url,
                status=response.status,
                body=body,
                set_cookie_names=names,
            )

        raise PortalHttpError(
            f"Too many redirects fetching {path}", set_cookie_names=seen_names
        )

    def get_html(self, path: str) -> str:
        """Fetch an authenticated portal page body, raising SessionExpiredError at Login."""
        return self.fetch_page(path).body

    # -- typed portal operations ---------------------------------------------
    def fetch_roster(self) -> RosterResult:
        """Fetch and parse the enrolled-course roster from ``/Courses``.

        Owns the dashboard page-marker check, semester extraction, and the "0 courses is a
        parse error" rule, and reports the ``Set-Cookie`` header names observed on the page.
        """
        page = self.fetch_page("/Courses")
        html = page.body
        if not has_courses_page_marker(html):
            raise PortalParseError(
                "Dashboard response missing expected courses table markup.",
                set_cookie_names=page.set_cookie_names,
            )
        courses = parse_courses(html)
        if not courses:
            raise PortalParseError(
                "Dashboard parsed 0 courses.", set_cookie_names=page.set_cookie_names
            )
        return RosterResult(
            courses=courses,
            semester=parse_semester(html),
            set_cookie_names=page.set_cookie_names,
        )

    def _fetch_collecting(self, path: str, observed: List[str]) -> PortalResponse:
        """``fetch_page`` that accumulates observed cookie names and merges them on error."""
        try:
            page = self.fetch_page(path)
        except (SessionExpiredError, PortalHttpError) as exc:
            raise _with_observed_cookies(exc, observed)
        observed[:] = _dedupe_names(observed, page.set_cookie_names)
        return page

    def _fetch_verified(
        self,
        course: Course,
        path: str,
        marker_fn: Any,
        missing_marker_message: str,
        observed: List[str],
        parse_fn: Any,
    ) -> Tuple[str, Any]:
        """Fetch one course page, verify it, and parse it; return ``(body, parsed)``.

        One home for the four-step triple the marks and resources pages share:
        ``_fetch_collecting`` -> page-marker check -> ``verify_active_course`` ->
        ``parse_*``. Error messages and ``set_cookie_names`` widening are unchanged.
        The page body is returned for callers that need it (marks diagnostics).
        """
        page = self._fetch_collecting(path, observed)
        if not marker_fn(page.body):
            raise PortalParseError(
                missing_marker_message,
                set_cookie_names=tuple(observed),
            )
        try:
            verify_active_course(page.body, course)
        except CourseMismatchError as exc:
            raise _with_observed_cookies(exc, observed)
        return page.body, parse_fn(page.body)

    def fetch_course_snapshot(
        self, course: Course, include_announcements: bool = False
    ) -> CourseSnapshotResult:
        """Fetch one course's marks, resources and optional announcements.

        Performs the whole ASP.NET dance in order — ``SetCourse`` -> active-course
        verification -> ``/MarksSummary/Index`` fetch + parse -> ``/LearningResources/Index``
        fetch + parse -> optional announcement parsing — and returns parsed items only.

        Raw HTML never leaves this method: when no marks are parsed, ``diagnostic_html``
        carries the already-scrubbed marks page (see :func:`scrub_diagnostic_html`) and
        ``diagnostic_preview`` its credential-free plain-text snippet. Errors carry every
        ``Set-Cookie`` name observed so far in addition to their own.
        """
        observed: List[str] = []

        announcements_html = self._fetch_collecting(
            f"/Courses/SetCourse/{course.course_id}", observed
        ).body
        try:
            verify_active_course(announcements_html, course)
        except CourseMismatchError as exc:
            raise _with_observed_cookies(exc, observed)

        marks_html, marks = self._fetch_verified(
            course,
            "/MarksSummary/Index",
            has_marks_page_marker,
            "MarksSummary response missing expected marks markup.",
            observed,
            parse_marks_summary,
        )
        _resources_html, resources = self._fetch_verified(
            course,
            "/LearningResources/Index",
            has_resources_page_marker,
            "LearningResources response missing expected resources markup.",
            observed,
            parse_learning_resources,
        )

        announcements: Optional[List[AnnouncementItem]] = None
        if include_announcements:
            announcements = parse_announcements(announcements_html)

        diagnostic_html = ""
        diagnostic_preview = ""
        if not marks:
            diagnostic_html = scrub_diagnostic_html(marks_html)
            diagnostic_preview = extract_diagnostic_text(marks_html)

        return CourseSnapshotResult(
            marks=marks,
            resources=resources,
            announcements=announcements,
            set_cookie_names=tuple(observed),
            diagnostic_html=diagnostic_html,
            diagnostic_preview=diagnostic_preview,
        )


# -- module-level helpers ----------------------------------------------------


def _with_observed_cookies(
    exc: Exception, observed: Iterable[str]
) -> Exception:
    """Re-raise ``exc`` carrying every cookie name observed so far in the snapshot.

    Returns the exception to raise (never raises itself) so call sites read
    ``raise _with_observed_cookies(...)``. When nothing new was observed the original
    exception instance is returned unchanged, keeping its traceback chain intact.
    """
    current: Tuple[str, ...] = getattr(exc, "set_cookie_names", ())
    merged = _dedupe_names(observed, current)
    if merged == current:
        return exc
    return type(exc)(str(exc), set_cookie_names=merged)  # type: ignore[call-arg]
