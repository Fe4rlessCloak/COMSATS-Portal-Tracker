"""Reusable in-memory HTTP transport stub for Portal (`SisClient`) tests.

The Portal module talks to the network through a single injectable adapter:

    adapter.fetch(url, headers, timeout) -> TransportResponse(status, headers, body, final_url)

`StubTransport` implements that interface fully in memory so tests can exercise the
real protocol logic inside `SisClient` (cookie staging, redirect safety, login
detection, error translation) without patching any private opener internals.

Contract that later tickets depend on:

* ``StubResponse(body, status=200, headers=..., final_url=None)`` describes one canned
  response. ``headers`` may map a name to a single value or to a sequence of values,
  which is how multi-value ``Set-Cookie`` headers are declared.
* ``StubTransport(*queued)`` / ``transport.add(outcome, url=...)`` queue an outcome.
  An outcome is either a ``StubResponse`` or an exception instance; queuing an
  exception makes the next matching fetch raise it (transport/network errors).
* ``url`` matching is exact-URL or path-suffix (``"/Courses"`` matches
  ``https://sis.comsats.edu.pk/Courses``), and ``url=None`` matches any request.
  The first matching queued outcome wins and is consumed, so redirect sequences are
  expressed by queuing hops in order.
* ``transport.requests`` records every outbound request as
  ``StubRequest(url, headers, timeout)`` for assertions.

Python 3.9+ standard library only.
"""

from __future__ import annotations

import email.message
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from sis_client import PortalTransport, TransportResponse

__all__ = [
    "StubResponse",
    "StubRequest",
    "StubTransport",
    "StubTransportError",
    "transport_error",
]


class StubTransportError(OSError):
    """Transport-level failure raised by the stub (stands in for a socket error)."""


def transport_error(message: str = "stub transport error") -> StubTransportError:
    """Return an exception instance ready to be queued on a :class:`StubTransport`."""

    return StubTransportError(message)


@dataclass(frozen=True)
class StubResponse:
    """One canned portal response.

    ``body`` may be ``str`` (encoded as UTF-8) or raw ``bytes``. ``headers`` values may be
    a single string or a sequence of strings; sequences become repeated headers, which is
    the shape used for multi-value ``Set-Cookie``. ``final_url`` defaults to the request URL
    and is what the Portal module uses for login-page detection.
    """

    body: Union[str, bytes] = ""
    status: int = 200
    headers: Mapping[str, Union[str, Sequence[str]]] = field(default_factory=dict)
    final_url: Optional[str] = None

    def to_transport_response(self, request_url: str) -> TransportResponse:
        message = email.message.Message()
        for name, value in self.headers.items():
            values: Sequence[Any]
            if isinstance(value, (list, tuple)):
                values = value
            else:
                values = (value,)
            for item in values:
                message.add_header(str(name), str(item))
        body = self.body.encode("utf-8") if isinstance(self.body, str) else self.body
        return TransportResponse(
            status=int(self.status),
            headers=message,
            body=body,
            final_url=self.final_url or request_url,
        )


@dataclass(frozen=True)
class StubRequest:
    """One recorded outbound request (what the Portal module handed to the adapter)."""

    url: str
    headers: Dict[str, str]
    timeout: Optional[float] = None

    def header(self, name: str, default: str = "") -> str:
        """Case-insensitive header lookup over the recorded outbound headers."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default

    @property
    def cookie_header(self) -> str:
        return self.header("Cookie")


class StubTransport(PortalTransport):
    """In-memory ``fetch(url, headers, timeout)`` adapter for Portal tests."""

    def __init__(
        self,
        *queued: Union[StubResponse, BaseException],
        on_request: Optional[Callable[[StubRequest], None]] = None,
    ) -> None:
        self.requests: List[StubRequest] = []
        self._queue: List[Tuple[Optional[str], Union[StubResponse, BaseException]]] = []
        self.on_request = on_request
        for outcome in queued:
            self.add(outcome)

    # -- registration ---------------------------------------------------------
    def add(
        self, outcome: Union[StubResponse, BaseException], url: Optional[str] = None
    ) -> "StubTransport":
        """Queue ``outcome`` for the next request matching ``url`` (any request if None)."""
        if not isinstance(outcome, (StubResponse, BaseException)):
            raise TypeError(f"unsupported stub outcome: {outcome!r}")
        self._queue.append((url, outcome))
        return self

    def on(self, url: Optional[str], outcome: Union[StubResponse, BaseException]) -> "StubTransport":
        """Alias for ``add(outcome, url=url)`` — reads as "on this URL, respond with ..."."""
        return self.add(outcome, url=url)

    def __len__(self) -> int:
        return len(self._queue)

    # -- adapter interface ----------------------------------------------------
    def fetch(self, url: str, headers: Mapping[str, str], timeout: Optional[float] = None) -> TransportResponse:
        request = StubRequest(url=url, headers=dict(headers), timeout=timeout)
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)

        for index, (pattern, outcome) in enumerate(self._queue):
            if self._matches(pattern, url):
                del self._queue[index]
                break
        else:
            raise AssertionError(
                "StubTransport received an unexpected request: "
                f"{url} (queued patterns: {[p for p, _ in self._queue]!r}, "
                f"already served {len(self.requests) - 1} request(s))"
            )

        if isinstance(outcome, BaseException):
            raise outcome
        return outcome.to_transport_response(url)

    @staticmethod
    def _matches(pattern: Optional[str], url: str) -> bool:
        if pattern is None:
            return True
        return url == pattern or url.endswith(pattern)

    # -- assertions helpers ---------------------------------------------------
    @property
    def request_count(self) -> int:
        return len(self.requests)

    def urls(self) -> List[str]:
        return [request.url for request in self.requests]

    def cookie_header(self, index: int = -1) -> str:
        return self.requests[index].cookie_header

    def remaining_patterns(self) -> List[Optional[str]]:
        return [pattern for pattern, _ in self._queue]
