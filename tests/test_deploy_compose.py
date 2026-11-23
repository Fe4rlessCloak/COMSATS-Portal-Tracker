#!/usr/bin/env python3
"""Contract tests for the prebuilt-image Compose setup and the auto-updater (#16).

Three surfaces are pinned here, each at its public boundary:

* **Compose resolution.** ``docker compose config`` is the authority on what a
  host actually runs, so when the CLI is present the tests read the *resolved*
  JSON and assert on it. A parsed-YAML test runs unconditionally on every
  interpreter leg, because the CI matrix must still catch an image reference
  that drifted even where no Docker CLI exists.
* **The update script's behaviour.** It is a shell script, so its interface is
  argv plus the ``docker`` binary it calls. The tests put a stub ``docker`` on
  ``PATH`` and assert the *sequence of calls*: pull always, recreate only when
  the ID the tag resolves to differs from the ID the container is running, prune
  filtered by label, never the image now in use, never forced. Asserting the
  call log rather than the script's text is what lets the script be rewritten
  without breaking these tests.
* **The systemd units.** Parsed as INI and checked for the facts systemd cares
  about (``Type=oneshot``, a timer that fires every 15 minutes, ``Unit=`` -> the
  service, ``ExecStart=`` -> the script), not for their formatting.

The one thing no test here can prove is that a published image carries the
label: #14's workflow has never run on ``main``. ``tests/test_workflow_
publish.py`` pins the label the workflow *will* attach; the prune is only as
safe as that label being present, which is why the filter is asserted verbatim.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from support_workflow_yaml import loads  # noqa: E402

COMPOSE = ROOT / "docker-compose.yml"
OVERRIDE = ROOT / "docker-compose.local.yml"
DEPLOY = ROOT / "deploy" / "systemd"
SERVICE_UNIT = DEPLOY / "comsats-tracker-update.service"
TIMER_UNIT = DEPLOY / "comsats-tracker-update.timer"
UPDATE_SCRIPT = DEPLOY / "comsats-tracker-update.sh"
README = ROOT / "README.md"

IMAGE = "ghcr.io/fe4rlesscloak/comsats-portal-tracker:latest"
IMAGE_REPO = "ghcr.io/fe4rlesscloak/comsats-portal-tracker"
CONTAINER = "comsats-portal-tracker"
REPO_URL = "https://github.com/Fe4rlessCloak/COMSATS-Portal-Tracker"
SOURCE_LABEL = f"org.opencontainers.image.source={REPO_URL}"
PRUNE_FILTER = f"label={SOURCE_LABEL}"

ENVIRONMENT = {
    "ENV_FILE": "/app/config/.env",
    "STATE_FILE": "/app/data/state.json",
    "HEARTBEAT_FILE": "/app/data/heartbeat.json",
}

CURRENT = "sha256:" + "1" * 64
PREVIOUS = "sha256:" + "2" * 64


def _compose_yaml(path: Path) -> Dict[str, Any]:
    document = loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"{path.name}: root is not a mapping")
    return document


def _tracker(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["services"]["tracker"]


def _env_map(service: Dict[str, Any]) -> Dict[str, str]:
    """Normalise the list form (``- K=V``) the shipped file uses."""
    entries = service.get("environment") or []
    result: Dict[str, str] = {}
    for entry in entries:
        key, _, value = str(entry).partition("=")
        result[key] = value
    return result


def _volume_map(service: Dict[str, Any]) -> Dict[str, bool]:
    """target -> read_only, from the short ``src:dst[:ro]`` syntax."""
    result: Dict[str, bool] = {}
    for entry in service.get("volumes") or []:
        parts = str(entry).split(":")
        result[parts[1]] = len(parts) > 2 and "ro" in parts[2].split(",")
    return result


class ComposeShapeTests(unittest.TestCase):
    """The shipped files, read directly, so every interpreter leg checks them."""

    def setUp(self) -> None:
        self.service = _tracker(_compose_yaml(COMPOSE))

    def test_pulls_the_prebuilt_ghcr_image(self) -> None:
        self.assertEqual(self.service["image"], IMAGE)

    def test_does_not_build_by_default(self) -> None:
        """A server with no checkout cannot build; `build:` here would break it."""
        self.assertNotIn("build", self.service)
        self.assertNotIn("pull_policy", self.service)

    def test_keeps_the_container_name(self) -> None:
        self.assertEqual(self.service["container_name"], CONTAINER)

    def test_keeps_the_restart_policy(self) -> None:
        self.assertEqual(self.service["restart"], "unless-stopped")

    def test_keeps_all_three_environment_variables(self) -> None:
        self.assertEqual(_env_map(self.service), ENVIRONMENT)

    def test_keeps_the_read_only_config_mount_and_read_write_data_mount(self) -> None:
        self.assertEqual(
            _volume_map(self.service), {"/app/config": True, "/app/data": False}
        )

    def test_override_enables_a_local_build(self) -> None:
        self.assertEqual(_tracker(_compose_yaml(OVERRIDE))["build"], ".")

    def test_override_keeps_the_base_image_reference(self) -> None:
        """Re-declaring `image:` here would let the two files drift apart."""
        self.assertNotIn("image", _tracker(_compose_yaml(OVERRIDE)))

    def test_override_does_not_duplicate_the_runtime_contract(self) -> None:
        for key in ("container_name", "restart", "environment", "volumes"):
            self.assertNotIn(key, _tracker(_compose_yaml(OVERRIDE)), key)


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return probe.returncode == 0


@unittest.skipUnless(
    _docker_compose_available(),
    "docker compose CLI not installed; ComposeShapeTests still runs everywhere",
)
class ComposeResolutionTests(unittest.TestCase):
    """`docker compose config` is what the host runs -- assert on its output."""

    def _resolve(self, *files: str) -> Dict[str, Any]:
        command = ["docker", "compose"]
        for name in files:
            command += ["-f", name]
        command += ["config", "--format", "json"]
        result = subprocess.run(
            command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def test_default_config_resolves_to_the_pulled_image(self) -> None:
        service = self._resolve("docker-compose.yml")["services"]["tracker"]
        self.assertEqual(service["image"], IMAGE)
        self.assertNotIn("build", service)

    def test_override_config_resolves_to_a_build_of_the_same_image(self) -> None:
        service = self._resolve("docker-compose.yml", "docker-compose.local.yml")[
            "services"
        ]["tracker"]
        self.assertEqual(service["image"], IMAGE)
        self.assertEqual(Path(service["build"]["context"]).resolve(), ROOT)

    def test_both_shapes_keep_identity_mounts_and_policy(self) -> None:
        for files in (
            ("docker-compose.yml",),
            ("docker-compose.yml", "docker-compose.local.yml"),
        ):
            service = self._resolve(*files)["services"]["tracker"]
            self.assertEqual(service["container_name"], CONTAINER, files)
            self.assertEqual(service["restart"], "unless-stopped", files)
            self.assertEqual(service["environment"], ENVIRONMENT, files)
            volumes = {
                volume["target"]: volume.get("read_only", False)
                for volume in service["volumes"]
            }
            self.assertEqual(volumes, {"/app/config": True, "/app/data": False}, files)


# A stub `docker` that answers the four questions the update script asks and logs
# every invocation, so the script's behaviour is assertable offline: no daemon,
# no registry, no pulled image.
STUB_DOCKER = """#!/usr/bin/env bash
set -u
printf '%s\\n' "$*" >> "$STUB_LOG"
if [ "$1" = "compose" ]; then
  exit 0
fi
if [ "$1" = "image" ]; then
  if [ "$2" = "inspect" ]; then
    cat "$STUB_DIR/latest_id" 2>/dev/null || exit 1
    exit 0
  fi
  exit 0
fi
if [ "$1" = "inspect" ]; then
  if [ -f "$STUB_DIR/container_id" ]; then cat "$STUB_DIR/container_id"; else exit 1; fi
  exit 0
fi
if [ "$1" = "images" ]; then
  cat "$STUB_DIR/label_ids" 2>/dev/null || exit 0
  exit 0
fi
exit 0
"""


class UpdateScriptTests(unittest.TestCase):
    """Drive the script against a stub `docker` and read the call log."""

    def setUp(self) -> None:
        self.assertTrue(
            UPDATE_SCRIPT.is_file(), f"missing {UPDATE_SCRIPT.relative_to(ROOT)}"
        )
        self.tmp = Path(tempfile.mkdtemp(prefix="tracker-update-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.stack = self.tmp / "stack"
        self.stack.mkdir()
        (self.stack / "docker-compose.yml").write_text(
            COMPOSE.read_text(encoding="utf-8"), encoding="utf-8"
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.stub = bin_dir / "docker"
        self.stub.write_text(STUB_DOCKER, encoding="utf-8")
        self.stub.chmod(0o755)

        self.state = self.tmp / "state"
        self.state.mkdir()
        self.log = self.tmp / "calls.log"
        self.log.write_text("", encoding="utf-8")

        self.env = {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "STUB_LOG": str(self.log),
            "STUB_DIR": str(self.state),
            "STACK_DIR": str(self.stack),
            "HOME": str(self.tmp),
        }

    def _state(self, latest: str, container: Optional[str]) -> None:
        (self.state / "latest_id").write_text(latest + "\n", encoding="utf-8")
        if container is None:
            (self.state / "container_id").unlink(missing_ok=True)
        else:
            (self.state / "container_id").write_text(container, encoding="utf-8")

    def _labels(self, *ids: str) -> None:
        (self.state / "label_ids").write_text(
            "\n".join(ids) + ("\n" if ids else ""), encoding="utf-8"
        )

    def _run(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["bash", str(UPDATE_SCRIPT)] + list(args),
            env=self.env,
            cwd=str(self.tmp),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _calls(self) -> List[str]:
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def _compose_calls(self, verb: str) -> List[str]:
        """Compose invocations whose subcommand is ``verb``.

        Substring matching would be wrong here twice over: the project directory
        itself is named ``tracker-update-...`` (contains "up"), and ``up -d``
        ends in ``-d``.
        """
        return [
            call
            for call in self._calls()
            if call.startswith("compose") and f" {verb} " in call + " "
        ]

    def test_runs_compose_from_the_stack_directory(self) -> None:
        self._state(CURRENT, CURRENT)
        self._labels(CURRENT)
        self._run()
        self.assertTrue(
            all(str(self.stack) in call for call in self._calls() if call.startswith("compose")),
            self._calls(),
        )

    def test_pulls_before_deciding_anything(self) -> None:
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS)
        self._run()
        calls = self._calls()
        self.assertTrue(calls[0].startswith("compose"), calls)
        self.assertIn("pull", calls[0])

    def test_unchanged_image_does_not_recreate_the_container(self) -> None:
        self._state(CURRENT, CURRENT)
        self._labels(CURRENT)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self._compose_calls("up"), [])
        self.assertIn("unchanged", result.stdout.lower())

    def test_new_image_recreates_the_container_after_pulling(self) -> None:
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        ups = self._compose_calls("up")
        self.assertEqual(len(ups), 1, self._calls())
        self.assertIn("-d", ups[0])
        self.assertLess(
            self._calls().index(self._compose_calls("pull")[0]), self._calls().index(ups[0])
        )

    def test_first_run_without_a_container_starts_the_stack(self) -> None:
        self._state(CURRENT, None)
        self._labels(CURRENT)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self._compose_calls("up")), 1, self._calls())

    def test_decision_is_made_on_the_image_id_not_the_tag(self) -> None:
        """`latest` always reads `latest`, so only the IDs can differ."""
        self._state(CURRENT, CURRENT)
        self._labels(CURRENT)
        self._run()
        inspects = [c for c in self._calls() if c.startswith("image inspect")]
        self.assertEqual(len(inspects), 1, self._calls())
        self.assertIn("{{.Id}}", inspects[0])

    def test_prune_filters_on_the_exact_source_label(self) -> None:
        self._state(CURRENT, CURRENT)
        self._labels(CURRENT)
        self._run()
        images_calls = [c for c in self._calls() if c.startswith("images")]
        self.assertEqual(len(images_calls), 1, self._calls())
        self.assertIn(PRUNE_FILTER, images_calls[0])
        # Dangling-only or name-based pruning would hit unrelated host images.
        self.assertNotIn("dangling", images_calls[0])
        self.assertNotIn("reference", images_calls[0])

    def test_prune_removes_superseded_images_only(self) -> None:
        older = "sha256:" + "3" * 64
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS, older)
        self._run()
        removed = [c for c in self._calls() if c.startswith("image rm")]
        self.assertEqual(sorted(removed), sorted([f"image rm {PREVIOUS}", f"image rm {older}"]))

    def test_prune_never_removes_the_image_in_use(self) -> None:
        self._state(CURRENT, CURRENT)
        self._labels(CURRENT)
        self._run()
        self.assertEqual([c for c in self._calls() if c.startswith("image rm")], [])

    def test_prune_never_forces(self) -> None:
        """`image rm` without -f refuses an in-use image; -f would not.

        Only removals are checked: `compose -f FILE` is the compose file flag,
        not a force.
        """
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS)
        self._run()
        removals = [call for call in self._calls() if call.startswith("image rm")]
        self.assertTrue(removals, self._calls())
        for call in removals:
            self.assertNotIn(" -f", call)
            self.assertNotIn("--force", call)

    def test_prune_tolerates_an_image_that_refuses_to_go(self) -> None:
        """A refused removal must not fail the unit or stop later candidates."""
        self.stub.write_text(
            STUB_DOCKER.replace(
                'if [ "$1" = "compose" ]; then\n  exit 0\nfi',
                'if [ "$1" = "compose" ]; then\n  exit 0\nfi\n'
                'if [ "$1" = "image" ] && [ "$2" = "rm" ]; then\n'
                '  echo "image is being used" >&2\n  exit 1\nfi',
            ),
            encoding="utf-8",
        )
        self.stub.chmod(0o755)
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_pull_failure_fails_the_unit_without_recreating(self) -> None:
        self.stub.write_text(
            STUB_DOCKER.replace(
                'if [ "$1" = "compose" ]; then\n  exit 0\nfi',
                'if [ "$1" = "compose" ]; then\n  echo "pull failed" >&2\n  exit 1\nfi',
            ),
            encoding="utf-8",
        )
        self.stub.chmod(0o755)
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._compose_calls("up"), [])

    def test_dry_run_prunes_and_recreates_nothing(self) -> None:
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS)
        result = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([c for c in self._calls() if c.startswith("image rm")], [])
        self.assertEqual(self._compose_calls("up"), [])
        self.assertEqual(self._compose_calls("pull"), [])

    def test_dry_run_names_the_images_it_would_remove(self) -> None:
        self._state(CURRENT, PREVIOUS)
        self._labels(CURRENT, PREVIOUS)
        result = self._run("--dry-run")
        self.assertIn(PREVIOUS, result.stdout)

    def test_rejects_an_unknown_argument(self) -> None:
        self._state(CURRENT, CURRENT)
        self._labels()
        result = self._run("--nonsense")
        self.assertNotEqual(result.returncode, 0)


class UpdateScriptTextTests(unittest.TestCase):
    """The safety properties a call log cannot show: what it refuses to do."""

    def setUp(self) -> None:
        self.text = UPDATE_SCRIPT.read_text(encoding="utf-8")
        # Comments explain *why* a dangerous command is absent, and a comment
        # saying "not `docker image prune`" would fail a naive substring check.
        # The safety property is about what the script executes.
        self.code = "\n".join(
            line for line in self.text.splitlines() if not line.lstrip().startswith("#")
        )
        # The heredoc-free script only has doc comments; the `-h` help text is
        # the one place a `#` is data, and it names no docker verb.
        self.assertIn("set -euo pipefail", self.code)

    def test_is_a_bash_script_and_parses(self) -> None:
        self.assertTrue(self.text.startswith("#!/usr/bin/env bash"))
        result = subprocess.run(
            ["bash", "-n", str(UPDATE_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_carries_the_exact_image_and_label(self) -> None:
        self.assertIn(f'IMAGE="{IMAGE}"', self.code)
        self.assertIn(f'SOURCE_LABEL="{SOURCE_LABEL}"', self.code)

    def test_never_prunes_by_name_or_dangling_state(self) -> None:
        for forbidden in ("image prune", "container prune", "system prune", "until="):
            self.assertNotIn(forbidden, self.code)

    def test_never_forces_removal(self) -> None:
        # `-f` alone is legitimate (`compose -f FILE`), so only the removal
        # forms that would override the in-use guard are forbidden.
        for forbidden in ("--force", "image rm -f", "rmi -f", "prune -f"):
            self.assertNotIn(forbidden, self.code)

    def test_never_restarts_anything_else(self) -> None:
        for forbidden in (
            "systemctl",
            "docker restart",
            "compose restart",
            "compose down",
            "compose stop",
        ):
            self.assertNotIn(forbidden, self.code)

    def test_deletes_only_after_the_up(self) -> None:
        """A prune before `up` could delete the image the container still needs."""
        self.assertLess(self.code.index("compose up -d"), self.code.index("image rm"))


class UnitFileTests(unittest.TestCase):
    @staticmethod
    def _ini(path: Path) -> Dict[str, Dict[str, str]]:
        parser = configparser.RawConfigParser()
        parser.optionxform = str  # systemd keys are case-sensitive
        parser.read_string(path.read_text(encoding="utf-8"))
        return {section: dict(parser.items(section)) for section in parser.sections()}

    def test_service_is_a_oneshot_that_runs_the_update_script(self) -> None:
        service = self._ini(SERVICE_UNIT)["Service"]
        self.assertEqual(service["Type"], "oneshot")
        self.assertIn("comsats-tracker-update.sh", service["ExecStart"])

    def test_service_has_a_working_directory_for_the_compose_file(self) -> None:
        self.assertIn("WorkingDirectory", self._ini(SERVICE_UNIT)["Service"])

    def test_service_binds_no_docker_socket_and_no_privileges(self) -> None:
        service = self._ini(SERVICE_UNIT)["Service"]
        self.assertNotIn("docker.sock", SERVICE_UNIT.read_text(encoding="utf-8"))
        self.assertNotIn("Privileged", service)

    def test_timer_fires_every_fifteen_minutes(self) -> None:
        timer = self._ini(TIMER_UNIT)
        self.assertEqual(timer["Timer"]["OnUnitActiveSec"], "15min")
        self.assertIn("OnBootSec", timer["Timer"])
        self.assertEqual(timer["Timer"]["Unit"], SERVICE_UNIT.name)

    def test_timer_is_enabled_by_timers_target(self) -> None:
        self.assertEqual(self._ini(TIMER_UNIT)["Install"]["WantedBy"], "timers.target")

    def test_units_explain_the_label_scope(self) -> None:
        """The comment is the operator's only explanation of why prune is safe."""
        text = SERVICE_UNIT.read_text(encoding="utf-8") + UPDATE_SCRIPT.read_text(
            encoding="utf-8"
        )
        self.assertIn("label", text.lower())
        self.assertRegex(text, r"(?i)(unrelated|other)\b.{0,80}(service|container|image)")

    def test_unit_files_do_not_name_a_host_specific_path(self) -> None:
        for path in (SERVICE_UNIT, TIMER_UNIT):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"/home/[A-Za-z0-9._-]+/", str(path))


class ReadmeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")

    def test_documents_the_prebuilt_image(self) -> None:
        self.assertIn(IMAGE, self.text)

    def test_documents_the_systemd_units_and_journalctl(self) -> None:
        self.assertIn("comsats-tracker-update.timer", self.text)
        self.assertIn("journalctl -u comsats-tracker-update", self.text)

    def test_documents_rollback_by_pinned_sha_tag(self) -> None:
        self.assertRegex(self.text, r"sha-[0-9a-f]{7}|sha-<")

    def test_documents_the_local_build_override(self) -> None:
        self.assertIn("docker-compose.local.yml", self.text)

    def test_documents_public_package_visibility(self) -> None:
        self.assertRegex(
            self.text, r"(?i)public.{0,80}(package|visibility)|visibility.{0,40}public"
        )

    def test_documents_that_no_checkout_is_needed(self) -> None:
        self.assertRegex(self.text, r"(?i)(no|without a|not)\b.{0,40}(source )?checkout")


class HygieneTests(unittest.TestCase):
    """Committed templates and docs must carry no personal or secret material."""

    def setUp(self) -> None:
        self.files = [
            COMPOSE,
            OVERRIDE,
            SERVICE_UNIT,
            TIMER_UNIT,
            UPDATE_SCRIPT,
            README,
        ]

    def test_no_credentials(self) -> None:
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            for pattern in (
                r"ghp_[A-Za-z0-9]{20,}",
                r"github_pat_[A-Za-z0-9_]{20,}",
                r"AKIA[0-9A-Z]{16}",
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                r"ASP_NET_SessionId=[A-Za-z0-9]{8,}",
            ):
                self.assertIsNone(re.search(pattern, text), f"{path.name}: {pattern}")

    def test_no_email_addresses(self) -> None:
        for path in self.files:
            self.assertIsNone(
                re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", path.read_text(encoding="utf-8")),
                path.name,
            )

    def test_only_loopback_ip_literals(self) -> None:
        for path in self.files:
            found = set(
                re.findall(
                    r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
                    path.read_text(encoding="utf-8"),
                )
            )
            self.assertEqual(found - {"127.0.0.1"}, set(), path.name)

    def test_no_host_specific_paths(self) -> None:
        for path in self.files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"/home/[A-Za-z0-9._-]+/", path.name)
            self.assertNotRegex(text, r"/srv/[A-Za-z0-9._-]+/", path.name)


if __name__ == "__main__":
    unittest.main()
