#!/usr/bin/env python3
"""
Leaf configuration module for the COMSATS Portal Tracker (Python 3.9+ stdlib only).

Single source of truth for:

- ``.env`` byte decoding (UTF-8/UTF-16/UTF-32 BOMs + cp1252 fallback)
- ``.env`` text parsing (``export`` prefixes, quoted escapes, inline ``#`` comments)
- ``SIS_SESSION_ID`` normalization (pasted ``Cookie:`` headers, ``ASP.NET_SessionId=...``)
- secret-free missing-session diagnostics
- process-environment vs. file precedence with per-key source provenance
- single-read ``.env`` fingerprint watching (existence, inode, mtime, size, SHA-256)
- typed tracker configuration and the one canonical ``NOTIFIER_TIMEOUT`` rule

This module is a leaf: it must not import ``tracker``, ``probe``, ``sis_client`` or
``notifiers``. Every public entry point accepts an optional ``environ`` mapping
(defaulting to ``os.environ``) so callers and tests can inject plain dictionaries
without mutating process-wide environment state.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

# Portal defaults are mirrored here (not imported from sis_client) so this module
# stays a leaf. tests/test_config.py asserts the two copies stay equal, so drift fails
# CI rather than relying on a human noticing.
DEFAULT_BASE_URL = "https://sis.comsats.edu.pk"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# The single canonical notification timeout rule (default 20s, minimum 1s).
NOTIFIER_TIMEOUT_DEFAULT = 20.0
NOTIFIER_TIMEOUT_MINIMUM = 1.0

SESSION_PLACEHOLDER = "paste_your_ASP_NET_SessionId_here"

KNOWN_ENV_KEYS = (
    "SIS_SESSION_ID",
    "SIS_BASE_URL",
    "SIS_USER_AGENT",
    "SIS_SEMESTER",
    "KEEPALIVE_INTERVAL",
    "FETCH_INTERVAL",
    "REQUEST_DELAY",
    "TRACK_ANNOUNCEMENTS",
    "NTFY_URL",
    "NTFY_TOKEN",
    "WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "CYCLE_TIMEOUT",
    "SESSION_EXPIRED_TIMEOUT",
    "EXPIRED_POLL_INTERVAL",
    "NOTIFIER_TIMEOUT",
    "MAX_STATE_BACKUPS",
    "HEALTHCHECK_MAX_AGE",
)

# Settings that only matter when (re)building notifier instances.
NOTIFIER_ENV_KEYS = (
    "NTFY_URL",
    "NTFY_TOKEN",
    "WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)

# Sentinel: distinguishes "caller already read the file (None = absent)" from
# "no bytes supplied, read the file now".
_UNREAD = object()

_EXPORT_PREFIX_RE = re.compile(r"^export[ \t]+")
_INLINE_COMMENT_RE = re.compile(r"(?:^|\s+)#.*$")
_SESSION_COOKIE_RE = re.compile(r"(?:^|;\s*)asp\.net_sessionid\s*=\s*([^;]*)", re.IGNORECASE)
_SAFE_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")


def _environ(environ: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    """Return the caller-provided environment mapping or the live process environment."""
    return os.environ if environ is None else environ


@dataclass(frozen=True)
class EnvFileSignature:
    """Fingerprint of a ``.env`` file used for change detection."""

    exists: bool
    inode: Optional[int]
    mtime_ns: Optional[int]
    size: Optional[int]
    sha256: str


@dataclass
class TrackerConfig:
    """Typed runtime configuration derived from process env + ``.env`` values."""

    raw: Dict[str, str]
    session_id: str
    base_url: str
    user_agent: str
    keepalive_interval: int
    fetch_interval: int
    request_delay: float
    track_announcements: bool
    sources: Dict[str, str] = field(default_factory=dict)
    cycle_timeout: int = 120
    session_expired_timeout: int = 300
    expired_poll_interval: int = 15
    notifier_timeout: float = NOTIFIER_TIMEOUT_DEFAULT
    max_state_backups: int = 5
    healthcheck_max_age: int = 0
    semester: str = ""

    @property
    def notifier_key(self) -> Tuple[Any, ...]:
        """Comparison key for notifier-relevant settings.

        The daemon rebuilds notifier instances only when this key changes. It may
        contain credentials (tokens), so compare it but never print it.
        """
        return notifier_config_key(self.raw, timeout=self.notifier_timeout)


@dataclass(frozen=True)
class EnvReload:
    """Outcome of one single-read watcher check: change status, reason, fresh config."""

    changed: bool
    description: str
    config: TrackerConfig


def _decode_env_bytes(raw: bytes) -> str:
    """Decode .env bytes supporting UTF-32/UTF-16/UTF-8 BOMs and cp1252 fallback."""
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw.decode("utf-32", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _parse_quoted_env_value(val: str, quote: str) -> Optional[str]:
    """Parse a single- or double-quoted .env value, allowing trailing # comments."""
    if quote == "'":
        end_idx = val.find("'", 1)
        if end_idx == -1:
            return None
        tail = val[end_idx + 1 :].lstrip()
        if tail and not tail.startswith("#"):
            return None
        return val[1:end_idx]

    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    chars: list = []
    i = 1
    n = len(val)
    while i < n:
        ch = val[i]
        if ch == "\\":
            if i + 1 < n:
                nxt = val[i + 1]
                chars.append(escapes.get(nxt, "\\" + nxt))
                i += 2
                continue
            chars.append("\\")
            i += 1
            continue
        if ch == '"':
            tail = val[i + 1 :].lstrip()
            if tail and not tail.startswith("#"):
                return None
            return "".join(chars)
        chars.append(ch)
        i += 1
    return None


def _parse_env_value(raw_val: str) -> str:
    """Parse a .env RHS value, handling quotes, escapes, and inline # comments."""
    val = raw_val.strip()
    if not val:
        return ""
    if val[0] in ('"', "'"):
        parsed = _parse_quoted_env_value(val, val[0])
        if parsed is not None:
            return parsed
    return _INLINE_COMMENT_RE.sub("", val).strip()


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse .env text supporting BOMs, `export KEY=VAL`, quotes, and inline # comments."""
    values: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_key, raw_val = line.split("=", 1)
        key = raw_key.strip().lstrip("\ufeff").strip()
        if _EXPORT_PREFIX_RE.match(key):
            key = _EXPORT_PREFIX_RE.sub("", key, count=1).strip()
        if not key:
            continue
        values[key] = _parse_env_value(raw_val)
    return values


def read_env_bytes(env_path: Path) -> Optional[bytes]:
    """Read a .env file exactly once; returns None when missing, a directory, or unreadable."""
    try:
        if env_path.is_dir():
            print(f"[WARN] ENV_FILE points to a directory instead of a file: {env_path}")
            return None
        return env_path.read_bytes()
    except OSError:
        return None


def parse_env_bytes(raw: bytes) -> Dict[str, str]:
    """Decode and parse .env bytes already read from disk."""
    return parse_env_text(_decode_env_bytes(raw))


def parse_env_file(env_path: Path) -> Dict[str, str]:
    """Read and parse a .env file on disk; returns {} if missing or a directory."""
    raw = read_env_bytes(env_path)
    if raw is None:
        return {}
    return parse_env_bytes(raw)


def normalize_session_id(raw_value: str) -> str:
    """Normalize pasted ASP.NET_SessionId values (Cookie headers, cookie pairs, placeholders)."""
    val = raw_value.strip().strip("\ufeff").strip()
    if not val:
        return ""
    if val.lower().startswith("cookie:"):
        val = val.split(":", 1)[1].strip()
    cookie_match = _SESSION_COOKIE_RE.search(val)
    if cookie_match:
        val = cookie_match.group(1).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        val = val[1:-1].strip()
    if val == SESSION_PLACEHOLDER:
        return ""
    return val


def load_env_with_sources(
    env_path: Path,
    environ: Optional[Mapping[str, str]] = None,
    raw: Any = _UNREAD,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Load settings from process environment overridden by non-empty values in .env.

    ``raw`` may carry already-read file bytes (``None`` meaning "absent") so callers
    such as the watcher never read the file twice for one check.
    """
    env = _environ(environ)
    values: Dict[str, str] = {}
    sources: Dict[str, str] = {}

    for key in KNOWN_ENV_KEYS:
        env_val = (env.get(key) or "").strip()
        if env_val != "":
            values[key] = env_val
            sources[key] = "process_env"

    if raw is _UNREAD:
        file_values = parse_env_file(env_path)
    else:
        file_values = parse_env_bytes(raw) if raw is not None else {}
    for key, val in file_values.items():
        if val.strip() != "":
            had_proc_env = bool((env.get(key) or "").strip())
            values[key] = val
            sources[key] = (
                f"env_file:{env_path} (overrides process_env)"
                if had_proc_env
                else f"env_file:{env_path}"
            )
        elif key not in values:
            values[key] = ""
            sources[key] = f"env_file:{env_path}"

    return values, sources


def load_env_dict(
    env_path: Path,
    environ: Optional[Mapping[str, str]] = None,
    raw: Any = _UNREAD,
) -> Dict[str, str]:
    """Load key=value settings from process environment overridden by .env on disk."""
    values, _ = load_env_with_sources(env_path, environ=environ, raw=raw)
    return values


def env_file_signature(env_path: Path, raw: Any = _UNREAD) -> EnvFileSignature:
    """Fingerprint the .env path, reusing already-read bytes when supplied."""
    if raw is _UNREAD:
        raw = read_env_bytes(env_path)
    if raw is None:
        return EnvFileSignature(
            exists=False, inode=None, mtime_ns=None, size=None, sha256=""
        )
    try:
        st = env_path.stat()
    except OSError:
        return EnvFileSignature(
            exists=False, inode=None, mtime_ns=None, size=None, sha256=""
        )
    return EnvFileSignature(
        exists=True,
        inode=st.st_ino,
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _describe_change(prev: EnvFileSignature, cur: EnvFileSignature) -> str:
    if cur == prev:
        return ""
    reasons: list = []
    if not prev.exists and cur.exists:
        reasons.append("created")
    elif prev.exists and not cur.exists:
        reasons.append("deleted")
    else:
        if prev.inode != cur.inode:
            reasons.append(f"inode {prev.inode}->{cur.inode}")
        if prev.sha256 != cur.sha256:
            reasons.append("content changed")
        else:
            if prev.mtime_ns != cur.mtime_ns:
                reasons.append("mtime changed")
            if prev.size != cur.size:
                reasons.append(f"size {prev.size}->{cur.size}")
            reasons.append("content unchanged")
    return ", ".join(reasons)


class EnvWatcher:
    """Watches a .env file across inode replacements, mtime edits, and content updates.

    Each ``check()`` performs at most ONE read of the file and reuses those bytes for
    both the fingerprint comparison and the freshly parsed configuration.
    """

    def __init__(
        self, env_path: Path, environ: Optional[Mapping[str, str]] = None
    ) -> None:
        self.env_path = env_path
        self.environ = environ
        self._last_sig: EnvFileSignature = env_file_signature(env_path)

    def _observe(self, raw: Optional[bytes]) -> Tuple[EnvFileSignature, bool, str]:
        cur = env_file_signature(self.env_path, raw=raw)
        prev = self._last_sig
        desc = _describe_change(prev, cur)
        self._last_sig = cur
        return cur, bool(desc), desc

    def check(
        self,
        require_session: bool = False,
        environ: Optional[Mapping[str, str]] = None,
    ) -> EnvReload:
        """Single-read check returning change status, description, and fresh config.

        The one read is reused for the fingerprint and for parsing, and ``environ`` (or the
        mapping given at construction) flows into ``load_config``.
        """
        raw = read_env_bytes(self.env_path)
        _, changed, desc = self._observe(raw)
        env = self.environ if environ is None else environ
        cfg = load_config(
            self.env_path,
            require_session=require_session,
            environ=env,
            raw=raw,
        )
        return EnvReload(changed=changed, description=desc, config=cfg)


def parse_int(
    values: Mapping[str, str], key: str, default: int, minimum: int = 5
) -> int:
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def parse_float(
    values: Mapping[str, str], key: str, default: float, minimum: float = 0.0
) -> float:
    raw = (values.get(key) or "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def parse_notifier_timeout(
    values: Mapping[str, str],
    default: float = NOTIFIER_TIMEOUT_DEFAULT,
    minimum: float = NOTIFIER_TIMEOUT_MINIMUM,
) -> float:
    """The one canonical NOTIFIER_TIMEOUT rule: default 20.0s, minimum 1.0s."""
    return parse_float(values, "NOTIFIER_TIMEOUT", default, minimum=minimum)


def notifier_config_key(
    values: Mapping[str, str], timeout: Optional[float] = None
) -> Tuple[Any, ...]:
    """Comparison key for notifier-relevant settings parsed from a raw values mapping.

    ``timeout`` lets a caller that already parsed ``NOTIFIER_TIMEOUT`` (``TrackerConfig``)
    pass it in; otherwise the canonical rule is applied here.
    """
    effective = parse_notifier_timeout(values) if timeout is None else timeout
    key: list = [("NOTIFIER_TIMEOUT", effective)]
    for name in NOTIFIER_ENV_KEYS:
        key.append((name, (values.get(name) or "").strip()))
    return tuple(key)


def describe_missing_session_context(env_path: Path, values: Mapping[str, str]) -> str:
    """Return a secret-free diagnostic line explaining why SIS_SESSION_ID is missing."""
    if not env_path.exists():
        file_desc = f"env file does not exist: {env_path}"
    elif env_path.is_dir():
        file_desc = f"env path is a directory (not a file): {env_path}"
    else:
        try:
            file_desc = f"env file {env_path} exists ({env_path.stat().st_size} bytes)"
        except OSError:
            file_desc = f"env file {env_path} exists (stat failed)"

    if "SIS_SESSION_ID" not in values:
        sid_state = "SIS_SESSION_ID key absent"
    else:
        raw_sid = (values.get("SIS_SESSION_ID") or "").strip()
        if raw_sid == SESSION_PLACEHOLDER:
            sid_state = "SIS_SESSION_ID still set to example placeholder"
        elif not raw_sid:
            sid_state = "SIS_SESSION_ID key present but empty"
        else:
            sid_state = "SIS_SESSION_ID present but empty after normalization"

    parts = [file_desc, sid_state]
    known_set = [
        k for k in KNOWN_ENV_KEYS if k != "SIS_SESSION_ID" and (values.get(k) or "").strip()
    ]
    if known_set:
        parts.append(f"other configured keys: {', '.join(known_set)}")
    unrecognized = [
        k for k in values if k not in KNOWN_ENV_KEYS and _SAFE_KEY_NAME_RE.match(k)
    ][:10]
    if unrecognized:
        parts.append(f"unrecognized keys: {', '.join(unrecognized)}")
    return "; ".join(parts) + "."


def resolve_env_file(
    root: Path, environ: Optional[Mapping[str, str]] = None
) -> Path:
    """Resolve ENV_FILE from the injected/process environment, else ``<root>/.env``."""
    raw = (_environ(environ).get("ENV_FILE") or "").strip()
    return Path(raw) if raw else Path(root) / ".env"


def resolve_state_file(
    root: Path, environ: Optional[Mapping[str, str]] = None
) -> Path:
    """Resolve STATE_FILE from the injected/process environment, else ``<root>/state.json``."""
    raw = (_environ(environ).get("STATE_FILE") or "").strip()
    return Path(raw) if raw else Path(root) / "state.json"


def resolve_heartbeat_file(
    state_path: Path, environ: Optional[Mapping[str, str]] = None
) -> Path:
    """Resolve HEARTBEAT_FILE from the injected/process environment, else beside state."""
    raw = (_environ(environ).get("HEARTBEAT_FILE") or "").strip()
    return Path(raw) if raw else Path(state_path).with_name("heartbeat.json")


def load_config(
    env_path: Path,
    require_session: bool = True,
    environ: Optional[Mapping[str, str]] = None,
    raw: Any = _UNREAD,
) -> TrackerConfig:
    """Build typed configuration from process env + .env (reusing raw bytes if given)."""
    values, sources = load_env_with_sources(env_path, environ=environ, raw=raw)
    session_id = normalize_session_id(values.get("SIS_SESSION_ID", ""))

    if require_session and not session_id:
        diag = describe_missing_session_context(env_path, values)
        raise SystemExit(
            "Missing SIS_SESSION_ID.\n"
            f"{diag}\n"
            f"Copy {env_path.parent / '.env.example'} to {env_path} and set SIS_SESSION_ID."
        )

    return TrackerConfig(
        raw=values,
        session_id=session_id,
        base_url=(values.get("SIS_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/"),
        user_agent=values.get("SIS_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        keepalive_interval=parse_int(values, "KEEPALIVE_INTERVAL", 120, minimum=15),
        fetch_interval=parse_int(values, "FETCH_INTERVAL", 480, minimum=30),
        request_delay=parse_float(values, "REQUEST_DELAY", 0.6),
        track_announcements=values.get("TRACK_ANNOUNCEMENTS", "").strip().lower()
        in ("1", "true", "yes", "on"),
        sources=sources,
        cycle_timeout=parse_int(values, "CYCLE_TIMEOUT", 120, minimum=5),
        session_expired_timeout=parse_int(values, "SESSION_EXPIRED_TIMEOUT", 300, minimum=0),
        expired_poll_interval=parse_int(values, "EXPIRED_POLL_INTERVAL", 15, minimum=1),
        notifier_timeout=parse_notifier_timeout(values),
        max_state_backups=parse_int(values, "MAX_STATE_BACKUPS", 5, minimum=1),
        healthcheck_max_age=parse_int(values, "HEALTHCHECK_MAX_AGE", 0, minimum=0),
        semester=values.get("SIS_SEMESTER", "").strip(),
    )
