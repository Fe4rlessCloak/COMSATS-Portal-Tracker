#!/usr/bin/env python3
"""
Local prober for COMSATS CUOnline Student Portal (https://sis.comsats.edu.pk).

Zero external dependencies (Python 3 stdlib only).
1. Verifies an ASP.NET_SessionId cookie from .env / environment.
2. Inspects /Courses, /MarksSummary/Index, /LearningResources/Index, and /Announcements
   to discover course-switching endpoints and HTML table/link structures.
3. Optional --keepalive mode to test whether the 4-5 min session timeout is sliding or hard.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_URL = "https://sis.comsats.edu.pk"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent urllib from silently following 302 redirects to /Login/Index."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class HttpResult:
    url: str
    status: int
    location: str
    set_cookies: List[str]
    body: str


class StructureExtractor(HTMLParser):
    """Extracts links, forms, onclick handlers, scripts, and table summaries."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[str, str, str]] = []  # (href, onclick, text)
        self.forms: List[Dict[str, object]] = []
        self.onclicks: List[Tuple[str, str, str]] = []  # (tag, onclick, text)
        self.scripts: List[str] = []
        self.script_srcs: List[str] = []
        self.tables: List[Dict[str, object]] = []

        self._cur_link: Optional[Tuple[str, str]] = None
        self._link_text: List[str] = []
        self._cur_onclick: Optional[Tuple[str, str]] = None
        self._onclick_text: List[str] = []
        self._cur_form: Optional[Dict[str, object]] = None
        self._in_script = False
        self._script_buf: List[str] = []
        self._cur_table: Optional[Dict[str, object]] = None
        self._in_tr = False
        self._in_cell = False
        self._cell_tag = ""
        self._cell_buf: List[str] = []
        self._cur_row: List[str] = []

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        t = tag.lower()

        if t == "a":
            self._cur_link = (attrs.get("href", ""), attrs.get("onclick", ""))
            self._link_text = []

        if attrs.get("onclick") and t != "a":
            self._cur_onclick = (t, attrs["onclick"])
            self._onclick_text = []

        if t == "form":
            self._cur_form = {
                "action": attrs.get("action", ""),
                "method": (attrs.get("method") or "GET").upper(),
                "id": attrs.get("id", ""),
                "inputs": [],
            }
        elif t in ("input", "select", "button") and self._cur_form is not None:
            inputs = self._cur_form["inputs"]
            assert isinstance(inputs, list)
            inputs.append(
                {
                    "tag": t,
                    "type": attrs.get("type", ""),
                    "name": attrs.get("name", ""),
                    "id": attrs.get("id", ""),
                }
            )

        if t == "script":
            src = attrs.get("src", "")
            if src:
                self.script_srcs.append(src)
            else:
                self._in_script = True
                self._script_buf = []

        if t == "table":
            self._cur_table = {
                "id": attrs.get("id", ""),
                "class": attrs.get("class", ""),
                "headers": [],
                "row_count": 0,
                "sample_rows": [],
            }
        elif t == "tr" and self._cur_table is not None:
            self._in_tr = True
            self._cur_row = []
        elif t in ("th", "td") and self._in_tr:
            self._in_cell = True
            self._cell_tag = t
            self._cell_buf = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()

        if t == "a" and self._cur_link is not None:
            href, onclick = self._cur_link
            text = " ".join("".join(self._link_text).split())
            self.links.append((href, onclick, text))
            self._cur_link = None

        if self._cur_onclick is not None and t == self._cur_onclick[0]:
            text = " ".join("".join(self._onclick_text).split())
            self.onclicks.append((t, self._cur_onclick[1], text))
            self._cur_onclick = None

        if t == "form" and self._cur_form is not None:
            self.forms.append(self._cur_form)
            self._cur_form = None

        if t == "script" and self._in_script:
            code = "".join(self._script_buf).strip()
            if code:
                self.scripts.append(code)
            self._in_script = False

        if t in ("th", "td") and self._in_cell:
            cell_text = " ".join("".join(self._cell_buf).split())
            self._cur_row.append(cell_text)
            if self._cell_tag == "th" and self._cur_table is not None:
                headers = self._cur_table["headers"]
                assert isinstance(headers, list)
                headers.append(cell_text)
            self._in_cell = False

        if t == "tr" and self._in_tr and self._cur_table is not None:
            if self._cur_row:
                self._cur_table["row_count"] = int(self._cur_table["row_count"]) + 1
                samples = self._cur_table["sample_rows"]
                assert isinstance(samples, list)
                if len(samples) < 3:
                    samples.append(self._cur_row)
            self._in_tr = False

        if t == "table" and self._cur_table is not None:
            self.tables.append(self._cur_table)
            self._cur_table = None

    def handle_data(self, data: str) -> None:
        if self._cur_link is not None:
            self._link_text.append(data)
        if self._cur_onclick is not None:
            self._onclick_text.append(data)
        if self._in_script:
            self._script_buf.append(data)
        if self._in_cell:
            self._cell_buf.append(data)


def load_env(env_path: Path) -> Dict[str, str]:
    """Load key=value pairs from .env if present."""
    values: Dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def load_config(env_path: Path) -> Tuple[str, str]:
    """Load SIS_SESSION_ID and optional SIS_USER_AGENT from env or .env."""
    env_file = load_env(env_path)
    session_id = (os.environ.get("SIS_SESSION_ID") or env_file.get("SIS_SESSION_ID") or "").strip()
    if session_id.startswith("ASP.NET_SessionId="):
        session_id = session_id.split("=", 1)[1].strip()

    if not session_id or session_id == "paste_your_ASP_NET_SessionId_here":
        raise SystemExit(
            "Missing SIS_SESSION_ID.\n"
            f"1. Copy {env_path.parent / '.env.example'} to {env_path}\n"
            "2. Set SIS_SESSION_ID=<your ASP.NET_SessionId cookie value>\n"
            "3. Re-run this script."
        )

    user_agent = (
        os.environ.get("SIS_USER_AGENT") or env_file.get("SIS_USER_AGENT") or ""
    ).strip() or USER_AGENT
    return session_id, user_agent


def fetch(path: str, session_id: str, user_agent: str = USER_AGENT) -> HttpResult:
    """Perform a GET request, following non-login redirects (up to 3 hops)."""
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(3):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{BASE_URL}/",
                "Cookie": f"ASP.NET_SessionId={session_id}",
            },
            method="GET",
        )
        try:
            with opener.open(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return HttpResult(
                    url=url,
                    status=resp.status,
                    location=resp.headers.get("Location", ""),
                    set_cookies=resp.headers.get_all("Set-Cookie") or [],
                    body=raw,
                )
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "") if e.headers else ""
            if e.code in (301, 302, 303, 307, 308) and loc and "login" not in loc.lower():
                url = urllib.parse.urljoin(url, loc)
                continue
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            return HttpResult(
                url=url,
                status=e.code,
                location=loc,
                set_cookies=(e.headers.get_all("Set-Cookie") or []) if e.headers else [],
                body=raw,
            )
    return HttpResult(url=url, status=310, location=url, set_cookies=[], body="")


def is_authenticated(res: HttpResult) -> bool:
    """Return True if response appears to be an authenticated portal page."""
    if res.status in (301, 302, 303, 307, 308) and "login" in res.location.lower():
        return False
    if res.status != 200:
        return False
    if 'id="LoginSubmit"' in res.body or 'id="CaptchaMount"' in res.body:
        return False
    return True


def summarize_page(name: str, path: str, res: HttpResult, save_dir: Path) -> None:
    """Save raw HTML to gitignored snapshots/ and print structural findings."""
    print(f"\n=== {name} ({path}) ===")
    print(f"HTTP {res.status}" + (f" -> Location: {res.location}" if res.location else ""))
    if res.set_cookies:
        names = [c.split("=", 1)[0].strip() for c in res.set_cookies]
        print(f"Set-Cookie names: {names}")

    if not is_authenticated(res):
        title_m = re.search(r"<title>(.*?)</title>", res.body, re.IGNORECASE | re.DOTALL)
        title = " ".join(title_m.group(1).split()) if title_m else ""
        print(f"STATUS: Not authenticated / session expired. (final_url={res.url}, title={title!r})")
        return

    save_dir.mkdir(parents=True, exist_ok=True)
    out_file = save_dir / f"{name}.html"
    out_file.write_text(res.body, encoding="utf-8")
    print(f"Saved HTML ({len(res.body):,} bytes) -> {out_file}")

    parser = StructureExtractor()
    parser.feed(res.body)

    # Filter interesting links (skip repetitive global header nav so page-specific links stand out)
    global_nav = {
        "/Courses/Index",
        "/RegistrationCard/Index",
        "/BusRouteReg/BusCard",
        "/StudentCard/Index",
        "/Fees/Index",
        "/ResultCard/Index",
        "/Profile/Index",
        "/SurveyLink/index",
        "/Clearance/Index",
        "/StudentSoS/Index",
        "/APS/Index",
        "/Login/Sign_out",
        "http://itscomsats.com/",
    }
    interesting_links = [
        (href, onclick, text)
        for href, onclick, text in parser.links
        if (
            (href and not href.startswith("#") and not href.startswith("javascript:void"))
            or onclick
        )
        and href not in global_nav
        and "document.getElementById('synopsisForm')" not in onclick
    ]
    print(f"Page-specific links ({len(interesting_links)}):")
    for href, onclick, text in interesting_links[:50]:
        oc = f" | onclick={onclick}" if onclick else ""
        print(f"  - [{text[:50]}] href={href}{oc}")
    if len(interesting_links) > 50:
        print(f"  ... ({len(interesting_links) - 50} more in saved HTML)")

    if parser.onclicks:
        print(f"Non-anchor onclick handlers ({len(parser.onclicks)}):")
        for tag, onclick, text in parser.onclicks[:20]:
            print(f"  - <{tag}> [{text[:40]}] onclick={onclick}")

    if parser.forms:
        print(f"Forms ({len(parser.forms)}):")
        for f in parser.forms:
            print(f"  - {f['method']} action={f['action']!r} id={f['id']!r} inputs={f['inputs']}")

    # Inspect inline scripts for $.ajax / $.post / $.get / window.location / Course
    ajax_hits: List[str] = []
    pattern = re.compile(
        r"(?:\$\.(?:ajax|post|get)|fetch\(|window\.location|Course|MarksSummary|LearningResources)",
        re.IGNORECASE,
    )
    for idx, code in enumerate(parser.scripts):
        for line in code.splitlines():
            if pattern.search(line):
                ajax_hits.append(f"[script#{idx}] {line.strip()[:140]}")
    if ajax_hits:
        print(f"Inline script clues ({len(ajax_hits)}):")
        for hit in ajax_hits[:25]:
            print(f"  - {hit}")

    if parser.tables:
        print(f"Tables ({len(parser.tables)}):")
        for i, tbl in enumerate(parser.tables):
            print(
                f"  - table#{i} id={tbl['id']!r} class={tbl['class']!r} "
                f"rows={tbl['row_count']} headers={tbl['headers']}"
            )
            for r in tbl["sample_rows"]:
                # Truncate cell contents so output stays compact
                compact = [c[:35] + ("…" if len(c) > 35 else "") for c in r]
                print(f"      row: {compact}")


def run_probe(
    session_id: str, user_agent: str, snapshots_dir: Path, course_id: Optional[str] = None
) -> int:
    """Probe /Courses, select a course via SetCourse, and inspect course-scoped pages."""
    courses_res = fetch("/Courses", session_id, user_agent)
    summarize_page("courses", "/Courses", courses_res, snapshots_dir)
    if not is_authenticated(courses_res):
        return 1

    found_ids = re.findall(r"/Courses/SetCourse/(\d+)", courses_res.body)
    chosen_id = course_id or (found_ids[0] if found_ids else None)
    if not chosen_id:
        print("\nNo /Courses/SetCourse/<id> links found on /Courses.")
        return 1

    print(f"\n>>> Selecting course {chosen_id} via /Courses/SetCourse/{chosen_id} (found {len(found_ids)} courses)")
    set_res = fetch(f"/Courses/SetCourse/{chosen_id}", session_id, user_agent)
    summarize_page(f"set_course_{chosen_id}", f"/Courses/SetCourse/{chosen_id}", set_res, snapshots_dir)

    targets = [
        ("marks_summary", "/MarksSummary/Index"),
        ("learning_resources", "/LearningResources/Index"),
        ("assignments", "/Assignments/Index"),
        ("quizzes", "/Quizzes/Index"),
        ("lecture_contents", "/LectureContents/Index"),
    ]
    for name, path in targets:
        res = fetch(path, session_id, user_agent)
        summarize_page(name, path, res, snapshots_dir)
    return 0


def run_keepalive(
    session_id: str, user_agent: str, interval: int, duration: int, path: str
) -> int:
    """Periodically ping a portal page to determine if session timeout is sliding or hard."""
    stop = False

    def _handle_sig(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_sig)

    start = time.monotonic()
    print(
        f"Starting keep-alive test on {path} every {interval}s for {duration}s "
        f"(Ctrl+C to stop early)..."
    )

    checks = 0
    while not stop:
        elapsed = int(time.monotonic() - start)
        now_str = datetime.now().strftime("%H:%M:%S")
        res = fetch(path, session_id, user_agent)
        checks += 1
        ok = is_authenticated(res)
        rotated = [c.split("=", 1)[0].strip() for c in res.set_cookies]
        rot_note = f" | Set-Cookie={rotated}" if rotated else ""

        if ok:
            print(f"[{now_str} | +{elapsed:>4}s] #{checks} HTTP {res.status} ALIVE{rot_note}")
        else:
            print(
                f"[{now_str} | +{elapsed:>4}s] #{checks} HTTP {res.status} EXPIRED "
                f"(Location={res.location!r}){rot_note}"
            )
            print(f"\nVerdict: Session expired after ~{elapsed}s despite {interval}s keep-alive pings.")
            return 1

        if elapsed >= duration:
            print(
                f"\nVerdict: Session stayed ALIVE for {elapsed}s (>= {duration}s)! "
                "Sliding expiration works."
            )
            return 0

        sleep_for = min(interval, max(1, duration - elapsed))
        for _ in range(sleep_for):
            if stop:
                break
            time.sleep(1)

    print("\nStopped by user.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="COMSATS SIS Portal Prober & Keep-Alive Tester")
    ap.add_argument(
        "--keepalive",
        action="store_true",
        help="Run keep-alive test instead of single-shot page probe",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=120,
        help="Seconds between keep-alive pings (default: 120)",
    )
    ap.add_argument(
        "--duration",
        type=int,
        default=600,
        help="Total seconds for keep-alive test (default: 600 = 10 min)",
    )
    ap.add_argument(
        "--path",
        default="/Courses",
        help="Path to ping during keep-alive (default: /Courses)",
    )
    ap.add_argument(
        "--course",
        default=None,
        help="Specific course ID for /Courses/SetCourse/<id> during probe (defaults to first found)",
    )
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    session_id, user_agent = load_config(root / ".env")
    if args.keepalive:
        return run_keepalive(session_id, user_agent, args.interval, args.duration, args.path)
    return run_probe(session_id, user_agent, root / "snapshots", args.course)


if __name__ == "__main__":
    sys.exit(main())
