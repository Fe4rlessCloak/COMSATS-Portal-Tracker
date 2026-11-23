#!/usr/bin/env python3
"""Opt-in session-renewal web page for the COMSATS Portal Tracker (3.9+ stdlib only).

A tiny ``http.server`` application that renders the tracker's heartbeat as
read-only status and accepts a pasted ``ASP.NET_SessionId`` cookie. A submitted
cookie is normalized by ``config``, verified live through the *real*
:class:`sis_client.SisClient` over its injectable ``PortalTransport`` seam, and —
only if the portal accepts it — written into ``.env`` by
``config.write_session_id()``, which touches that one line and nothing else.

Security posture, by construction:

* the bind address is loopback unless ``SESSION_UI_ALLOW_NON_LOOPBACK`` opts in;
* no response, error message or log line ever contains a cookie value, the
  stored ``SIS_SESSION_ID``, a notifier token, or raw ``.env`` content;
* every request goes through :meth:`SessionUiApp.handle`, whose first step is
  :meth:`SessionUiApp.guard_request` — the single place an operator-check
  (``Tailscale-User-Login``) and a CSRF check are added later, with no other
  edit required;
* there is no Docker access, no restart endpoint, and no other ``.env`` writer.

Settings come from the process environment first and the ``.env`` file second
(so ``SESSION_UI_ALLOWED_USER`` in ``.env`` is not a silent no-op for a bare
``python3 session_ui.py`` run), and an ``environ`` mapping is injected for tests:

===============================  =========  ==========================================
``SESSION_UI_HOST``              ``127.0.0.1``  Bind address (must be loopback).
``SESSION_UI_PORT``              ``8765``   Bind port (``0`` = ephemeral).
``SESSION_UI_PORTAL_TIMEOUT``    ``15``     Seconds for the live portal check (1–60).
``SESSION_UI_ALLOW_NON_LOOPBACK`` unset     Opt-in to a non-loopback bind (``1/true/yes/on``).
``SESSION_UI_ALLOWED_USER``      unset      Require this exact, case-insensitive
                                            ``Tailscale-User-Login`` on every request.
===============================  =========  ==========================================

``ENV_FILE``/``STATE_FILE``/``HEARTBEAT_FILE`` are resolved by ``config``.
"""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import os
import secrets
import socket
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from config import (
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    SESSION_PLACEHOLDER,
    load_env_dict,
    normalize_session_id,
    resolve_env_file,
    resolve_heartbeat_file,
    resolve_state_file,
    write_session_id,
)
from sis_client import PortalHttpError, PortalTransport, SessionExpiredError, SisClient

__all__ = [
    "APP_NAME",
    "CSRF_FIELD",
    "SESSION_FIELD",
    "BindRefusedError",
    "HeartbeatView",
    "Request",
    "Response",
    "SessionUiApp",
    "SessionUiSettings",
    "build_app",
    "create_server",
    "load_settings",
    "main",
    "read_heartbeat",
    "resolve_bind_host",
    "serve_forever",
]

APP_NAME = "session-ui"

#: The one form field a browser posts the pasted cookie in.
SESSION_FIELD = "session_id"

#: Hidden per-form field carrying the anti-CSRF token.
CSRF_FIELD = "csrf_token"

#: The identity header `tailscale serve` injects after authenticating the peer.
TAILSCALE_LOGIN_HEADER = "Tailscale-User-Login"

#: `Sec-Fetch-Site` values a legitimate same-origin form post can carry.
#: ``none`` is a direct navigation (a top-level load of the page itself), which
#: is still our own page; ``same-origin``/``same-site`` are our own fetches.
#: ``cross-site`` is a different site and is always refused.
TRUSTED_FETCH_SITES = frozenset({"same-origin", "none"})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PORTAL_TIMEOUT = 15.0
PORTAL_TIMEOUT_MINIMUM = 1.0
PORTAL_TIMEOUT_MAXIMUM = 60.0
MAX_REQUEST_BODY = 8 * 1024
#: Hard cap on form fields, handed to ``parse_qs`` as ``max_num_fields``. The
#: page posts exactly two; anything past this is not this form.
MAX_FORM_FIELDS = 32
CSRF_TOKEN_BYTES = 32

# Statuses the daemon writes when it cannot scan without a fresh cookie.
NEEDS_COOKIE_STATUSES = frozenset(
    {"waiting_for_session", "exiting_session_expired", "wedged_exiting"}
)
DEFAULT_HEALTHCHECK_MAX_AGE = 60

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class BindRefusedError(RuntimeError):
    """Raised when asked to bind a non-loopback address without the explicit opt-in."""


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class SessionUiSettings:
    """Resolved process settings for the session UI server."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    portal_timeout: float = DEFAULT_PORTAL_TIMEOUT
    allow_non_loopback: bool = False
    allowed_user: str = ""
    env_path: Optional[Path] = None
    heartbeat_path: Optional[Path] = None


def _env(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environ is None else environ


#: Every setting this page reads, in one place. ``load_settings`` consults each
#: from the process environment first and the ``.env`` file second, so the
#: documented "set it in .env" story is true for a bare ``python3 session_ui.py``
#: run as well as inside a container (where Compose puts them in the process
#: environment instead).
SESSION_UI_KEYS = (
    "SESSION_UI_HOST",
    "SESSION_UI_PORT",
    "SESSION_UI_PORTAL_TIMEOUT",
    "SESSION_UI_ALLOW_NON_LOOPBACK",
    "SESSION_UI_ALLOWED_USER",
)


def _flag(raw: str) -> bool:
    return raw.strip().lower() in _TRUTHY


def _parse_port(raw: str) -> int:
    try:
        port = int(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 0 <= port <= 65535 else DEFAULT_PORT


def _parse_portal_timeout(raw: str) -> float:
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_PORTAL_TIMEOUT
    return max(PORTAL_TIMEOUT_MINIMUM, min(PORTAL_TIMEOUT_MAXIMUM, value))


def _setting_values(
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, str], Path]:
    """Merge ``.env`` values under the process environment for the five settings.

    The file is read with ``config.load_env_dict`` so it gets the same BOM,
    ``export``, quoting and inline-comment handling as every other setting. A
    non-empty process-environment value is overlaid on top, so Compose (which
    passes them as environment entries) still wins over the mounted file, and a
    bare ``python3 session_ui.py`` run still reads ``.env`` as documented.
    """
    env = _env(environ)
    env_path = resolve_env_file(Path(__file__).resolve().parent, env)
    values = {
        key: value
        for key, value in load_env_dict(env_path, environ=env).items()
        if key in SESSION_UI_KEYS
    }
    for key in SESSION_UI_KEYS:
        raw = (env.get(key) or "").strip()
        if raw:
            values[key] = raw
    return values, env_path


def load_settings(environ: Optional[Mapping[str, str]] = None) -> SessionUiSettings:
    """Build settings from the process environment and ``.env`` (never writes)."""
    env = _env(environ)
    root = Path(__file__).resolve().parent
    state_path = resolve_state_file(root, env)
    values, env_path = _setting_values(environ)
    host = (values.get("SESSION_UI_HOST") or "").strip() or DEFAULT_HOST
    return SessionUiSettings(
        host=host,
        port=_parse_port(values.get("SESSION_UI_PORT", "")),
        portal_timeout=_parse_portal_timeout(values.get("SESSION_UI_PORTAL_TIMEOUT", "")),
        allow_non_loopback=_flag(values.get("SESSION_UI_ALLOW_NON_LOOPBACK", "")),
        allowed_user=(values.get("SESSION_UI_ALLOWED_USER") or "").strip(),
        env_path=env_path,
        heartbeat_path=resolve_heartbeat_file(state_path, env),
    )


def _origin_authority(origin: str) -> str:
    """Reduce an ``Origin`` header to lowercase ``host[:port]``, or ``""``.

    Handles the ``null`` opaque origin (a sandboxed frame, a ``file://`` page) by
    returning ``""``, which never matches a real ``Host``. Comparison is on the
    authority only, deliberately ignoring scheme: the page is reached over
    ``tailscale serve`` HTTPS while the loopback socket behind it is HTTP, so
    requiring an exact scheme match would reject every real browser submit.
    """
    candidate = (origin or "").strip()
    if not candidate or candidate.lower() == "null":
        return ""
    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    # A browser never puts userinfo in Origin, and stripping it would let
    # ``https://evil.example@our-host`` match us. Refuse rather than normalize.
    if "@" in parts.netloc:
        return ""
    return parts.netloc.lower()


def _is_loopback(host: str) -> bool:
    candidate = host.strip().strip("[]")
    if candidate.lower() in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def resolve_bind_host(
    host: str,
    environ: Optional[Mapping[str, str]] = None,
    allow_non_loopback: Optional[bool] = None,
) -> str:
    """Return ``host`` if it is loopback or the opt-in is set; raise otherwise.

    ``allow_non_loopback`` lets a caller that already resolved settings pass the
    value it read (from the process environment *or* ``.env``) instead of the
    opt-in being looked up twice from two different places.
    """
    candidate = (host or "").strip()
    if _is_loopback(candidate):
        return candidate or DEFAULT_HOST
    permitted = (
        load_settings(environ).allow_non_loopback
        if allow_non_loopback is None
        else allow_non_loopback
    )
    if permitted:
        return candidate
    raise BindRefusedError(
        f"refusing to bind {candidate!r}: only a loopback address is allowed. "
        "Set SESSION_UI_ALLOW_NON_LOOPBACK=1 to override, and expose the port over "
        "`tailscale serve` instead of binding a public interface."
    )


# --------------------------------------------------------------------------- heartbeat


@dataclass(frozen=True)
class HeartbeatView:
    """Read-only view of ``heartbeat.json`` for the status page.

    ``state`` is ``"ok"``, ``"missing"``, ``"corrupt"`` or ``"stale"``; the
    ``needs_cookie`` flag is derived from the daemon's waiting/exiting statuses
    and is what tells an operator the page exists to be used.
    """

    state: str
    status: str = ""
    detail: str = ""
    age_seconds: Optional[float] = None
    max_age_seconds: Optional[int] = None
    last_ok_iso: str = ""

    @property
    def needs_cookie(self) -> bool:
        return self.state == "ok" and self.status in NEEDS_COOKIE_STATUSES


def _format_timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def read_heartbeat(heartbeat_path: Path, now: Optional[float] = None) -> HeartbeatView:
    """Read and evaluate the heartbeat file; never raises for a bad/missing file.

    Freshness follows the tracker's own ``--healthcheck`` rule: stale past
    ``HEALTHCHECK_MAX_AGE`` when set, else past ``max(KEEPALIVE_INTERVAL, 60)``.
    """
    path = Path(heartbeat_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return HeartbeatView(state="missing")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("heartbeat payload is not an object")
        timestamp = float(payload["timestamp"])
    except (ValueError, TypeError, KeyError):
        return HeartbeatView(state="corrupt")

    current = float(now) if now is not None else datetime.now(tz=timezone.utc).timestamp()

    def _as_int(key: str, default: int) -> int:
        try:
            return int(payload.get(key) or default)
        except (TypeError, ValueError):
            return default

    healthcheck_max_age = _as_int("healthcheck_max_age", 0)
    keepalive = _as_int("keepalive_interval", 120)
    max_age = (
        healthcheck_max_age
        if healthcheck_max_age > 0
        else max(keepalive, DEFAULT_HEALTHCHECK_MAX_AGE)
    )
    age = max(0.0, current - timestamp)
    status = str(payload.get("status") or "unknown")
    detail = str(payload.get("detail") or "")
    last_ok = _format_timestamp(payload.get("last_ok_timestamp"))

    return HeartbeatView(
        state="stale" if age > max_age else "ok",
        status=status,
        detail=detail,
        age_seconds=age,
        max_age_seconds=max_age,
        last_ok_iso=last_ok,
    )


# --------------------------------------------------------------------------- HTTP types


@dataclass(frozen=True)
class Request:
    """One inbound HTTP request, as the application sees it."""

    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str, default: str = "") -> str:
        """Case-insensitive header lookup (the vhost-independent spelling)."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default


@dataclass(frozen=True)
class Response:
    """One outbound HTTP response."""

    status: int = 200
    body: str = ""
    content_type: str = "text/html; charset=utf-8"


@dataclass(frozen=True)
class VerifyOutcome:
    """Result of the live portal check, with a secret-free message on failure."""

    accepted: bool
    http_status: int
    message: str = ""
    course_count: int = 0


# --------------------------------------------------------------------------- application


class SessionUiApp:
    """The whole web application: one request entry point, two behaviours.

    ``handle()`` is the only public request surface. It delegates the pre-flight
    decision to :meth:`guard_request` — the Tailscale-user check and the
    cross-site ``POST`` check — and then routes ``GET /`` (status) and ``POST /``
    (save a cookie).
    """

    def __init__(
        self,
        env_path: Path,
        heartbeat_path: Path,
        transport: Optional[PortalTransport] = None,
        environ: Optional[Mapping[str, str]] = None,
        portal_timeout: float = DEFAULT_PORTAL_TIMEOUT,
        allowed_user: Optional[str] = None,
    ) -> None:
        self.env_path = Path(env_path)
        self.heartbeat_path = Path(heartbeat_path)
        self.transport = transport
        self.environ = environ
        self.portal_timeout = portal_timeout
        # An explicit argument wins; otherwise read the environment once at build
        # time so a per-request guard never re-reads mutable process state.
        self.allowed_user = (
            (allowed_user or "").strip()
            if allowed_user is not None
            else load_settings(environ).allowed_user
        )
        #: Per-process form token. Not one-shot, so an open tab keeps working
        #: across saves; it is unguessable and never logged or rendered anywhere
        #: except as the hidden input of the form.
        self.csrf_token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)

    # -- the one entry point ---------------------------------------------------
    def handle(self, request: Request) -> Response:
        """Route one request. Every response leaves through here."""
        blocked = self.guard_request(request)
        if blocked is not None:
            return blocked

        path = urllib.parse.urlparse(request.path).path or "/"
        if request.method == "GET" and path in ("/", "/index.html"):
            return self.render_status()
        if request.method == "POST" and path == "/":
            return self.save_session(request)
        if request.method in ("GET", "POST"):
            return self._page(404, "Not found", "There is nothing at that address.")
        return self._page(405, "Method not allowed", "Only GET and POST are supported.")

    def guard_request(self, request: Request) -> Optional[Response]:
        """Pre-flight gate for every request; returning a response blocks it.

        Runs before routing, so ``GET`` and ``POST`` are gated identically. Two
        independent checks, in this order:

        1. **Operator check** — when ``SESSION_UI_ALLOWED_USER`` is set, the
           request must carry a ``Tailscale-User-Login`` header equal to it after
           lowercasing. Not a substring, prefix or suffix test: ``evil-ahmed@…``
           must not satisfy ``ahmed@…``. A missing or different login is
           ``403`` and stops here, so the portal is never contacted and ``.env``
           is never touched.
        2. **Cross-site check** (``POST`` only) — a submission needs a browser
           context that proves it came from this page: ``Origin``, if present,
           must name the authority in ``Host``; ``Sec-Fetch-Site``, if present,
           must be ``same-origin`` or ``none``; and the hidden
           :data:`CSRF_FIELD` token must match this process's token. A request
           with neither ``Origin`` nor ``Sec-Fetch-Site`` is untrusted — absent
           headers are not evidence of a same-origin submit — so it is refused
           even with a plausible token. ``GET`` is read-only and needs no token.
        """
        if self.allowed_user and not self._login_matches(request.header(TAILSCALE_LOGIN_HEADER)):
            return self._forbidden()
        if request.method == "POST" and not self._post_is_same_origin(request):
            return self._forbidden()
        return None

    def _login_matches(self, provided: str) -> bool:
        """Exact, case-insensitive, constant-time comparison of Tailscale logins."""
        if not provided:
            return False
        return hmac.compare_digest(
            self.allowed_user.lower().encode("utf-8", "surrogatepass"),
            provided.lower().encode("utf-8", "surrogatepass"),
        )

    def _post_is_same_origin(self, request: Request) -> bool:
        """True only for a submission this page could have produced itself."""
        origin = request.header("Origin").strip()
        fetch_site = request.header("Sec-Fetch-Site").strip().lower()

        if origin:
            authority = _origin_authority(origin)
            host = request.header("Host").strip().lower()
            if not authority or not host or authority != host:
                return False
        if fetch_site and fetch_site not in TRUSTED_FETCH_SITES:
            return False
        if not origin and not fetch_site:
            return False  # no browser context at all: untrusted

        submitted = self._read_form(request).get(CSRF_FIELD, "")
        if not submitted:
            return False
        # Compare bytes, not str: ``hmac.compare_digest`` raises TypeError for a
        # str holding any non-ASCII character, so a submitted token like
        # ``tökén`` would escape the gate as an unhandled exception instead of a
        # plain 403. Encoding both sides keeps the comparison constant-time.
        return hmac.compare_digest(
            self.csrf_token.encode("utf-8", "surrogatepass"),
            submitted.encode("utf-8", "surrogatepass"),
        )

    # -- GET -------------------------------------------------------------------
    def render_status(self) -> Response:
        view = read_heartbeat(self.heartbeat_path)
        rows = [
            ("Status", view.status or "—"),
            ("Detail", view.detail or "—"),
            ("Last healthy", view.last_ok_iso or "never"),
        ]
        if view.age_seconds is not None:
            rows.append(
                (
                    "Heartbeat age",
                    f"{view.age_seconds:.0f}s (max {view.max_age_seconds}s)",
                )
            )

        if view.state == "missing":
            banner = (
                '<p class="banner warn">No heartbeat file yet — the tracker has not '
                "written one. It may be stopped or still starting.</p>"
            )
        elif view.state == "corrupt":
            banner = (
                '<p class="banner warn">Heartbeat unreadable (corrupt or not JSON). '
                "The tracker may be restarting.</p>"
            )
        elif view.state == "stale":
            banner = (
                f'<p class="banner warn">Heartbeat is stale ({view.age_seconds:.0f}s old, '
                f"max {view.max_age_seconds}s) — the tracker may be stopped.</p>"
            )
        elif view.needs_cookie:
            banner = (
                '<p class="banner urgent">The tracker is waiting for a new session '
                "cookie. Paste one below.</p>"
            )
        else:
            banner = '<p class="banner ok">The tracker is running normally.</p>'

        table = "".join(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
            for label, value in rows
        )
        inner = (
            f"{banner}<table class=\"status\">{table}</table>"
            '<p class="hint">This page is read-only: it never displays the stored '
            "session cookie or any other setting.</p>"
            f"{self._form_html(self.csrf_token)}"
        )
        return self._page(200, "Tracker status", inner)

    # -- POST ------------------------------------------------------------------
    def save_session(self, request: Request) -> Response:
        form = self._read_form(request)
        raw_value = form.get(SESSION_FIELD, "")
        candidate = normalize_session_id(raw_value)
        if not candidate:
            pasted = raw_value.strip()
            if pasted == SESSION_PLACEHOLDER:
                message = (
                    "That is still the example placeholder — paste your real "
                    "ASP.NET_SessionId cookie. Nothing was saved; .env is unchanged."
                )
            elif not pasted:
                message = (
                    "No cookie value found in that paste. Nothing was saved; "
                    ".env is unchanged."
                )
            else:
                message = (
                    "That paste did not contain a usable ASP.NET_SessionId value. "
                    "Nothing was saved; .env is unchanged."
                )
            return self._page(400, "Nothing saved", f"<p>{html.escape(message)}</p>")

        outcome = self.verify_session(candidate)
        if not outcome.accepted:
            return self._page(
                outcome.http_status, "Nothing saved", f"<p>{html.escape(outcome.message)}</p>"
            )

        courses = outcome.course_count
        try:
            write_session_id(self.env_path, candidate)
        except ValueError as exc:
            message = f"{exc} Nothing was saved; .env is unchanged."
            return self._page(400, "Nothing saved", f"<p>{html.escape(message)}</p>")
        except OSError:
            return self._page(
                500,
                "Nothing saved",
                "<p>Could not write the configuration file. Nothing was saved; "
                ".env is unchanged.</p>",
            )

        noun = "course" if courses == 1 else "courses"
        message = (
            f"Saved. The portal accepted the cookie and reported {courses} {noun}. "
            "The tracker reloads .env on its next tick — no restart needed."
        )
        return self._page(200, "Cookie saved", f"<p>{html.escape(message)}</p>")

    def verify_session(self, session_id: str) -> VerifyOutcome:
        """Check a cookie against the live portal without mutating anything.

        Uses the real ``SisClient`` over the injected ``PortalTransport`` seam,
        with the base URL/user agent read from the current ``.env`` and the
        bounded ``portal_timeout``. Every failure mode maps to a secret-free
        message that states ``.env`` was left unchanged.
        """
        values = load_env_dict(self.env_path, environ=self.environ)
        base_url = (values.get("SIS_BASE_URL") or "").strip() or DEFAULT_BASE_URL
        user_agent = (values.get("SIS_USER_AGENT") or "").strip() or DEFAULT_USER_AGENT
        client = SisClient(
            session_id=session_id,
            base_url=base_url,
            user_agent=user_agent,
            request_delay=0.0,
            timeout=int(self.portal_timeout),
            transport=self.transport,
        )
        try:
            roster = client.fetch_roster()
        except SessionExpiredError:
            return VerifyOutcome(
                False,
                400,
                "The portal rejected that cookie (the session is expired or invalid). "
                "Nothing was saved; .env is unchanged.",
            )
        except PortalHttpError:
            return VerifyOutcome(
                False,
                502,
                "The portal could not be checked right now (HTTP or parse error). "
                "Nothing was saved; .env is unchanged.",
            )
        except (TimeoutError, OSError, ValueError):
            return VerifyOutcome(
                False,
                502,
                "The portal check timed out or the network failed. Nothing was saved; "
                ".env is unchanged.",
            )
        except Exception:  # noqa: BLE001 - an unexpected failure must not leak or write
            return VerifyOutcome(
                False,
                500,
                "An unexpected error occurred while checking the cookie. Nothing was "
                "saved; .env is unchanged.",
            )
        return VerifyOutcome(True, 200, course_count=len(roster.courses))

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _read_form(request: Request) -> Dict[str, str]:
        """Parse the url-encoded body into a plain dict of the first values.

        Never raises: ``parse_qs`` raises ``ValueError`` past
        ``max_num_fields``, and a caller that let that escape would drop the
        connection instead of answering. An unparseable body simply carries no
        fields, which fails the token check and becomes a plain ``403``.
        """
        if not request.body:
            return {}
        try:
            text = request.body.decode("utf-8", errors="replace")
            parsed = urllib.parse.parse_qs(
                text, keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS
            )
        except (UnicodeDecodeError, AttributeError, ValueError):
            return {}
        return {key: values[0] for key, values in parsed.items() if values}

    @staticmethod
    def _form_html(token: str = "") -> str:
        placeholder = html.escape(SESSION_PLACEHOLDER)
        hidden = (
            f'<input type="hidden" name="{CSRF_FIELD}" value="{html.escape(token, quote=True)}">'
            if token
            else ""
        )
        return (
            '<form method="post" action="/" autocomplete="off">'
            f"{hidden}"
            f'<label for="{SESSION_FIELD}">New ASP.NET_SessionId</label>'
            f'<textarea id="{SESSION_FIELD}" name="{SESSION_FIELD}" rows="3" '
            f'placeholder="{placeholder}" spellcheck="false"></textarea>'
            "<p class=\"hint\">Raw value, <code>ASP.NET_SessionId=...</code>, or a full "
            "<code>Cookie:</code> header all work. The cookie is verified against the "
            "portal before anything is saved, and is never echoed back.</p>"
            '<button type="submit">Verify and save</button>'
            "</form>"
        )

    @staticmethod
    def _forbidden() -> Response:
        """The one 403 body. Deliberately says nothing about why, or about who.

        Naming the expected login — or the one presented — would turn the gate
        into a lookup oracle for anyone who can reach the port.
        """
        return SessionUiApp._page(
            403,
            "Forbidden",
            "<p>This page is not available to this request. If you reached it over "
            "Tailscale, sign in as the account this server allows. Nothing was "
            "saved; .env is unchanged.</p>",
        )

    @staticmethod
    def _page(status: int, title: str, inner: str) -> Response:
        body = (
            "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)} — COMSATS Portal Tracker</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:44rem;margin:2rem auto;"
            "padding:0 1rem;line-height:1.5}table.status{border-collapse:collapse;width:100%}"
            "table.status th{text-align:left;width:10rem;color:#555;font-weight:600;"
            "padding:.25rem 0}table.status td{padding:.25rem 0}.banner{padding:.75rem 1rem;"
            "border-radius:.4rem}.banner.ok{background:#e7f7ea}.banner.warn{background:#fff4d6}"
            ".banner.urgent{background:#fde2e1}textarea{width:100%;font-family:monospace}"
            ".hint{color:#666;font-size:.9rem}button{padding:.5rem 1rem;font-size:1rem}"
            "</style></head><body>"
            f"<h1>{html.escape(title)}</h1>{inner}"
            "</body></html>"
        )
        return Response(status=status, body=body)


# --------------------------------------------------------------------------- server


def build_app(
    env_path: Path,
    heartbeat_path: Path,
    transport: Optional[PortalTransport] = None,
    environ: Optional[Mapping[str, str]] = None,
    portal_timeout: Optional[float] = None,
    allowed_user: Optional[str] = None,
) -> SessionUiApp:
    """Build the application with explicit paths and an optional portal transport.

    ``transport`` is the test seam: pass ``SimulatedPortal`` to drive the real
    ``SisClient`` offline; leave it ``None`` for production ``UrllibTransport``.
    ``allowed_user`` overrides ``SESSION_UI_ALLOWED_USER`` when given explicitly.
    """
    settings = load_settings(environ)
    timeout = settings.portal_timeout if portal_timeout is None else portal_timeout
    return SessionUiApp(
        env_path=env_path,
        heartbeat_path=heartbeat_path,
        transport=transport,
        environ=environ,
        portal_timeout=timeout,
        allowed_user=settings.allowed_user if allowed_user is None else allowed_user,
    )


def _make_handler_class(app: SessionUiApp, log_stream: Optional[Any] = None) -> type:
    """Bind one application instance to a ``BaseHTTPRequestHandler`` subclass.

    ``log_stream`` redirects the one-per-request log line; ``None`` means
    ``sys.stderr`` resolved at write time. Tests pass a buffer so they can assert
    on exactly what the server emitted, with no global stream swapping.
    """

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SessionUI/1.0"
        sys_version = ""

        def _respond(self, method: str) -> None:  # noqa: N802 - http.server contract
            length = 0
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            body = b""
            if method == "POST" and length > 0:
                body = self.rfile.read(min(length, MAX_REQUEST_BODY))

            response = app.handle(
                Request(
                    method=method,
                    path=self.path,
                    headers=dict(self.headers.items()),
                    body=body,
                )
            )
            payload = response.body.encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)
            self._log(method, response.status)

        def do_GET(self) -> None:  # noqa: N802 - http.server contract
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server contract
            self._respond("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Silence the default request-line logger (it can echo the raw path)."""

        def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
            """Silence the default error logger; ``_log`` is the only log line."""

        def _log(self, method: str, status: int) -> None:
            """One secret-free line per request: method, path, status.

            The query string is dropped and no header or body is ever logged, so a
            cookie value cannot reach the log even if someone hand-crafts a URL.
            """
            stream = sys.stderr if log_stream is None else log_stream
            path = urllib.parse.urlparse(self.path).path or "/"
            stream.write(f"[{APP_NAME}] {method} {path} -> {status}\n")
            stream.flush()

    return _Handler


def _address_family_for(host: str) -> int:
    """Pick AF_INET6 for an IPv6 literal, AF_INET otherwise (no resolution tricks)."""
    candidate = host.strip().strip("[]")
    try:
        if ipaddress.ip_address(candidate).version == 6:
            return socket.AF_INET6
    except ValueError:
        pass
    return socket.AF_INET


def create_server(
    app: SessionUiApp,
    host: Optional[str] = None,
    port: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
    log_stream: Optional[Any] = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) the HTTP server, enforcing the loopback rule."""
    settings = load_settings(environ)
    bind_host = resolve_bind_host(
        settings.host if host is None else host,
        environ,
        allow_non_loopback=settings.allow_non_loopback,
    )
    bind_port = settings.port if port is None else port
    handler = _make_handler_class(app, log_stream=log_stream)

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = _address_family_for(bind_host)

    server = _Server((bind_host, bind_port), handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def serve_forever(
    server: ThreadingHTTPServer, out: Optional[Any] = None
) -> int:
    """Serve until interrupted; prints the bound URL once."""
    stream = sys.stdout if out is None else out
    host, port = server.server_address[:2]
    stream.write(f"[{APP_NAME}] listening on http://{host}:{port}/\n")
    stream.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        server.server_close()
    return 0


def main(
    argv: Optional[list] = None, environ: Optional[Mapping[str, str]] = None
) -> int:
    """CLI entry point: ``python3 session_ui.py [--host H] [--port P]``."""
    settings = load_settings(environ)
    parser = argparse.ArgumentParser(description="Opt-in session-renewal web page")
    parser.add_argument("--host", default=None, help="bind address (loopback only)")
    parser.add_argument("--port", type=int, default=None, help="bind port (0 = ephemeral)")
    parser.add_argument("--env-file", default=None, help="path to .env to update")
    parser.add_argument("--heartbeat-file", default=None, help="path to heartbeat.json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(__file__).resolve().parent
    env_path = (
        Path(args.env_file)
        if args.env_file
        else settings.env_path or resolve_env_file(root, environ)
    )
    heartbeat_path = (
        Path(args.heartbeat_file)
        if args.heartbeat_file
        else settings.heartbeat_path
        or resolve_heartbeat_file(resolve_state_file(root, environ), environ)
    )
    app = build_app(
        env_path,
        heartbeat_path,
        environ=environ,
        portal_timeout=settings.portal_timeout,
    )
    try:
        server = create_server(app, host=args.host, port=args.port, environ=environ)
    except BindRefusedError as exc:
        sys.stderr.write(f"[{APP_NAME}] {exc}\n")
        return 2
    except OSError as exc:
        sys.stderr.write(f"[{APP_NAME}] cannot bind: {exc}\n")
        return 2
    return serve_forever(server)


if __name__ == "__main__":
    sys.exit(main())
