#!/usr/bin/env python3
"""The `heartbeat.json` rules are one definition, not two copies.

`heartbeat.json` is a frozen external contract, and two modules interpret it:
`runtime_store.check_healthcheck` (the container HEALTHCHECK and the
`--healthcheck` CLI) and `session_ui.read_heartbeat` (the status page's
stale/needs-cookie banner). Each used to carry its own copy of the same
staleness formula and the same status set, so editing one and not the other
would make the page and the container disagree about whether the tracker is
healthy.

These tests pin the rule once and then prove both readers still agree with it,
which is what makes the shared definition worth having. Nothing is patched:
each payload is written with a real timestamp offset from the real clock, and
`read_heartbeat` is given the same instant explicitly.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import config  # noqa: E402
import runtime_store  # noqa: E402
import session_ui  # noqa: E402


class StalenessRuleTests(unittest.TestCase):
    """The rule itself, against worked examples rather than a re-derivation."""

    def test_positive_healthcheck_max_age_wins(self) -> None:
        payload = {"healthcheck_max_age": 300, "keepalive_interval": 15}

        self.assertEqual(config.heartbeat_stale_after(payload), 300)

    def test_unset_falls_back_to_the_keepalive_interval(self) -> None:
        self.assertEqual(config.heartbeat_stale_after({"keepalive_interval": 480}), 480)
        self.assertEqual(
            config.heartbeat_stale_after(
                {"healthcheck_max_age": 0, "keepalive_interval": 480}
            ),
            480,
        )

    def test_the_window_is_never_under_a_minute(self) -> None:
        self.assertEqual(config.heartbeat_stale_after({"keepalive_interval": 15}), 60)
        self.assertEqual(config.heartbeat_stale_after({}), 120)

    def test_garbage_values_do_not_raise(self) -> None:
        self.assertEqual(
            config.heartbeat_stale_after(
                {"healthcheck_max_age": "nope", "keepalive_interval": None}
            ),
            120,
        )

    def test_the_status_set_is_the_three_documented_ones(self) -> None:
        self.assertEqual(
            config.NEEDS_COOKIE_STATUSES,
            {"waiting_for_session", "exiting_session_expired", "wedged_exiting"},
        )
        for status in sorted(config.NEEDS_COOKIE_STATUSES):
            self.assertTrue(config.heartbeat_needs_cookie(status))
        for status in ("ok", "running", "", None, "OK"):
            self.assertFalse(config.heartbeat_needs_cookie(status))


class BothReadersAgreeTests(unittest.TestCase):
    """The two callers must reach the same verdict for the same payload."""

    def _payload(self, age: float, **overrides: Any) -> Dict[str, Any]:
        """A heartbeat written ``age`` seconds ago, with sane defaults overlaid."""
        payload: Dict[str, Any] = {
            "timestamp": time.time() - age,
            "iso": "2026-11-23T10:00:00+00:00",
            "pid": 4321,
            "status": "ok",
            "detail": "",
            "last_ok_timestamp": time.time() - age,
            "keepalive_interval": 120,
            "fetch_interval": 480,
            "healthcheck_max_age": 0,
        }
        payload.update(overrides)
        return payload

    def _write(self, payload: Dict[str, Any]) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        with handle as stream:
            json.dump(payload, stream)
        path = Path(handle.name)
        self.addCleanup(path.unlink)
        return path

    def _runtime_store_verdict(self, payload: Dict[str, Any]) -> bool:
        """True when `check_healthcheck` calls this payload healthy."""
        heartbeat = self._write(payload)
        store = runtime_store.RuntimeStore(
            heartbeat.with_name("state.json"), heartbeat
        )
        ok, _ = store.check_healthcheck()
        return ok

    def _session_ui_state(self, payload: Dict[str, Any]) -> str:
        heartbeat = self._write(payload)
        return session_ui.read_heartbeat(heartbeat, now=time.time()).state

    def test_staleness_boundary_matches_at_every_window(self) -> None:
        for overrides, window in (
            ({"healthcheck_max_age": 300, "keepalive_interval": 15}, 300),
            ({"healthcheck_max_age": 0, "keepalive_interval": 480}, 480),
            ({"keepalive_interval": 15}, 60),
            ({}, 120),
        ):
            for age, expected_stale in (
                (max(0.0, window - 30.0), False),
                (window + 30.0, True),
            ):
                with self.subTest(overrides=overrides, age=age):
                    payload = self._payload(age, **overrides)
                    healthy = self._runtime_store_verdict(payload)
                    state = self._session_ui_state(payload)

                    self.assertEqual(state, "stale" if expected_stale else "ok", payload)
                    self.assertEqual(
                        healthy,
                        not expected_stale,
                        "the container HEALTHCHECK and the page disagree about %r"
                        % (payload,),
                    )

    def test_cookie_status_matches_on_both_sides(self) -> None:
        for status in sorted(config.NEEDS_COOKIE_STATUSES) + ["ok", "running"]:
            with self.subTest(status=status):
                payload = self._payload(0.0, status=status)
                healthy = self._runtime_store_verdict(payload)
                view = session_ui.read_heartbeat(self._write(payload), now=time.time())

                self.assertEqual(view.needs_cookie, not healthy, payload)


if __name__ == "__main__":
    unittest.main()
