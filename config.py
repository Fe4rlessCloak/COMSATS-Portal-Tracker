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

import contextlib
import hashlib
import os
import re
import threading
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
# A `SIS_SESSION_ID` assignment line, split into its indent, the `=` and its RHS so a
# rewrite keeps deletion-safe groups around the value it replaces.
_SESSION_LINE_RE = re.compile(
    r"^(?P<lead>[ \t]*(?:export[ \t]+)?)SIS_SESSION_ID(?P<assign>[ \t]*=[ \t]*)(?P<value>.*)$"
)
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


def _env_bytes_codec(raw: bytes) -> Tuple[str, bytes]:
    """The codec :func:`_decode_env_bytes` will use, plus the exact BOM bytes.

    The writer needs both, because "touching no other byte" is only true if the
    re-encode uses the encoding the read used -- ``raw.decode(...)`` consumes a
    BOM, so it has to be re-attached verbatim rather than re-derived from the
    host's byte order. ``utf-16``/``utf-32`` would encode little-endian on this
    machine and silently rewrite a big-endian file.
    """
    for bom, codec in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8"),
    ):
        if raw.startswith(bom):
            return codec, bom
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "cp1252", b""
    return "utf-8", b""


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


@dataclass(frozen=True)
class SessionWrite:
    """Outcome of one ``SIS_SESSION_ID`` rewrite (what changed, not how)."""

    path: Path
    created: bool
    replaced: bool


def _split_line_ending(line: str) -> Tuple[str, str]:
    """Split a raw line into its content and its original ``\\r\\n``/``\\n``/``\\r`` ending."""
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, ""


def _closing_quote_index(text: str, quote: str) -> int:
    """Index of the closing quote in a quoted .env value, honouring ``\\"`` escapes."""
    i = 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i
        i += 1
    return -1


def _replace_value_segment(segment: str, new_value: str) -> str:
    """Rewrite the RHS of a session line, keeping its quotes and trailing comment.

    ``segment`` is everything after the ``=``. A quoted value keeps its quote
    character and whatever follows the closing quote (whitespace and an inline
    ``#`` comment); an unquoted value keeps its inline comment verbatim.
    """
    stripped = segment.strip()
    if stripped[:1] in ('"', "'"):
        quote = stripped[0]
        end_idx = _closing_quote_index(stripped, quote)
        if end_idx != -1:
            tail = stripped[end_idx + 1 :]
            return f"{quote}{new_value}{quote}{tail}"

    comment = _INLINE_COMMENT_RE.search(segment)
    if comment is not None and segment[comment.start() : comment.start() + 1].isspace():
        return f"{new_value}{segment[comment.start() :]}"
    if comment is not None and segment.startswith("#"):
        return f"{new_value} {segment}"
    return new_value


def _validate_session_value(session_id: str) -> str:
    """Normalize a pasted cookie and reject anything unsafe for a single .env line."""
    value = normalize_session_id(session_id)
    if not value:
        raise ValueError("SIS_SESSION_ID is blank or the example placeholder; nothing to save.")
    if any(ch.isspace() or ch in "\"'`\\" or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(
            "SIS_SESSION_ID contains characters that cannot be written to a single "
            ".env line."
        )
    return value


def write_session_id(env_path: Path, session_id: str) -> SessionWrite:
    """Atomically set ``SIS_SESSION_ID`` in ``env_path``, touching no other byte.

    Preserves every comment, blank line, key order and other setting, keeps the
    line's ``export`` prefix, its quote style and its inline comment, keeps CRLF
    line endings, and appends the key when it is absent. The file's own encoding
    is preserved too -- UTF-8 (with or without a BOM), UTF-16 and UTF-32 in
    either byte order, or the cp1252 fallback -- because the reader accepts all
    of them and a UTF-8 rewrite would corrupt the ones it is not. The write is a
    sibling temp file plus ``os.replace``, so a running daemon's
    :class:`EnvWatcher` sees the new inode immediately. A new file is created
    ``0600`` as UTF-8; an existing file keeps its mode. Raises ``ValueError``
    (before any write) for a blank, placeholder, or single-line-unsafe value.
    """
    value = _validate_session_value(session_id)

    raw = read_env_bytes(env_path)
    created = raw is None
    existing_mode: Optional[int] = None
    if raw is None:
        if env_path.exists():
            raise ValueError(f"cannot write SIS_SESSION_ID: {env_path} is a directory")
        text = ""
        codec, bom = "utf-8", b""
    else:
        try:
            existing_mode = env_path.stat().st_mode & 0o777
        except OSError:
            existing_mode = None
        text = _decode_env_bytes(raw)
        codec, bom = _env_bytes_codec(raw)

    lines = text.splitlines(keepends=True)
    replaced = False
    for index, line in enumerate(lines):
        content, ending = _split_line_ending(line)
        prefix = ""
        body = content
        if index == 0 and body.startswith("\ufeff"):
            prefix, body = "\ufeff", body[1:]
        match = _SESSION_LINE_RE.match(body)
        if match is None:
            continue
        lines[index] = (
            f"{prefix}{match.group('lead')}SIS_SESSION_ID{match.group('assign')}"
            f"{_replace_value_segment(match.group('value'), value)}{ending}"
        )
        replaced = True

    if not replaced:
        newline = "\r\n" if "\r\n" in text else "\n"
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] = lines[-1] + newline
        lines.append(f"SIS_SESSION_ID={value}{newline}")

    payload_text = "".join(lines)
    # `_decode_env_bytes` consumes a BOM; the original bytes are re-attached
    # verbatim below, so drop whatever `_decode_env_bytes`/`utf-8-sig` left here
    # rather than letting it be encoded a second time.
    if payload_text.startswith("\ufeff"):
        payload_text = payload_text[1:]
    try:
        payload = bom + payload_text.encode(codec)
    except UnicodeEncodeError:
        raise ValueError(
            f"SIS_SESSION_ID cannot be encoded as {codec}, which is what "
            f"{env_path.name} is written in; nothing was saved."
        )
    _atomic_replace_bytes(env_path, payload, 0o600 if created else existing_mode)
    return SessionWrite(path=env_path, created=created, replaced=replaced)


def _atomic_replace_bytes(path: Path, data: bytes, mode: Optional[int]) -> None:
    """Stage ``data`` in a sibling temp file with ``mode`` and ``os.replace`` it into place."""
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode or 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(str(tmp_path), mode)
        os.replace(str(tmp_path), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            if tmp_path.exists():
                tmp_path.unlink()
        raise


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
