#!/usr/bin/env python3
"""Contract tests for the GHCR publish workflow (#14).

The publish workflow is the one artifact in this repo that CI cannot test: it
only runs after a merge to ``main``, so a typo in a tag or a label is discovered
by an empty registry, not a red build. These tests pin the parts other tickets
depend on -- the ``sha-<7>`` tag shape (#16 rolls back by pinning it, #18's docs
quote it) and ``org.opencontainers.image.source`` (#16's prune filters on it).

They assert the parsed structure, not the file's text, so reformatting or
reordering keys cannot break them, but a changed trigger, tag, platform or label
does. ``support_workflow_yaml`` raises on anything it cannot parse rather than
skipping it, so a construct it cannot read fails the run instead of hiding.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from support_workflow_yaml import UnsupportedYaml, loads  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish-image.yml"
CHECKS = WORKFLOWS / "checks.yml"

IMAGE = "ghcr.io/fe4rlesscloak/comsats-portal-tracker"
REPO_URL = "https://github.com/Fe4rlessCloak/COMSATS-Portal-Tracker"


def _load(path: Path) -> Dict[str, Any]:
    document = loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"{path.name}: root is not a mapping")
    return document


def _text(value: Any) -> str:
    """Workflow values arrive as strings, bools or None; compare as text."""
    if value is None:
        return ""
    return str(value)


def _build_job(document: Dict[str, Any]) -> Dict[str, Any]:
    jobs = document["jobs"]
    if len(jobs) != 1:
        raise AssertionError(f"expected exactly one job, found {sorted(jobs)}")
    return jobs["build"]


_WHOLE_ENV_REF = re.compile(r"^\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def _resolve_env(value: Any, document: Dict[str, Any]) -> str:
    """Substitute a whole-value ``${{ env.NAME }}`` from the workflow's ``env:``.

    Asserting the literal is what makes a wrong repository URL or a differently
    spelled image reference fail. Comparing against ``${{ env.IMAGE }}`` instead
    would pass no matter what ``IMAGE`` held, so the indirection is resolved
    here rather than tolerated.
    """
    text = _text(value)
    match = _WHOLE_ENV_REF.match(text)
    if not match:
        return text
    name = match.group(1)
    env = document.get("env") or {}
    if name not in env:
        raise AssertionError(f"${{{{ env.{name} }}}} is not defined in the workflow env:")
    return _text(env[name])


def _steps(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return job["steps"]


def _step_using(job: Dict[str, Any], action: str) -> Dict[str, Any]:
    for step in _steps(job):
        if _text(step.get("uses")).startswith(action):
            return step
    raise AssertionError(f"no step uses {action}")


class YamlReaderTests(unittest.TestCase):
    """The reader must refuse to guess, or the workflow assertions mean nothing."""

    def test_keeps_on_as_a_string_key(self) -> None:
        document = loads("on:\n  push:\n")
        self.assertEqual(list(document), ["on"])

    def test_reads_nested_mappings_and_sequences(self) -> None:
        document = loads(
            "jobs:\n"
            "  build:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "        with:\n"
            "          ref: abc\n"
            "      - run: echo hi\n"
        )
        step = document["jobs"]["build"]["steps"][0]
        self.assertEqual(step["uses"], "actions/checkout@v4")
        self.assertEqual(step["with"], {"ref": "abc"})
        self.assertEqual(document["jobs"]["build"]["steps"][1]["run"], "echo hi")

    def test_reads_literal_block_scalars(self) -> None:
        document = loads("run: |\n  one\n  two\nnext: 1\n")
        self.assertEqual(document["run"], "one\ntwo\n")
        self.assertEqual(document["next"], "1")

    def test_strips_trailing_comments_but_not_quoted_hashes(self) -> None:
        document = loads('a: 1  # note\nb: "x # y"\n')
        self.assertEqual(document["a"], "1")
        self.assertEqual(document["b"], "x # y")

    def test_honours_chomping_indicators(self) -> None:
        """``>-`` must not report a trailing newline YAML would strip."""
        self.assertEqual(loads("a: |-\n  x\n")["a"], "x")
        self.assertEqual(loads("a: |\n  x\n")["a"], "x\n")
        self.assertEqual(loads("a: >-\n  x\n  y\n")["a"], "x y")

    def test_rejects_anchors(self) -> None:
        with self.assertRaises(UnsupportedYaml):
            loads("defaults: &base\n  a: 1\n")

    def test_rejects_duplicate_keys(self) -> None:
        with self.assertRaises(UnsupportedYaml):
            loads("a: 1\na: 2\n")

    def test_rejects_flow_mappings(self) -> None:
        with self.assertRaises(UnsupportedYaml):
            loads("a: {b: 1}\n")

    def test_rejects_unbalanced_flow_sequence(self) -> None:
        with self.assertRaises(UnsupportedYaml):
            loads("a: [1, 2\n")


class ConcurrencyTests(unittest.TestCase):
    """Publishes must serialise across commits, or `latest` can go backwards.

    `latest` is a single shared tag. Keying the concurrency group on
    `head_sha` -- as this workflow originally did -- gives every commit its own
    group, so two green merges build in parallel and whichever finishes second
    wins the tag. A slower run for an older commit would then repoint `latest`
    at older code, and the 15-minute systemd timer would recreate the server
    onto it, breaking #12 story 5 and #16's "only when a newer image was pulled".
    """

    def setUp(self) -> None:
        self.document = _load(PUBLISH)
        self.job = _build_job(self.document)

    def test_concurrency_group_is_repository_scoped(self) -> None:
        group = _text(self.job["concurrency"]["group"])

        self.assertNotIn("head_sha", group)
        self.assertNotIn("github.sha", group)
        self.assertNotIn("github.run_id", group)
        self.assertTrue(group.startswith("publish-image"), group)

    def test_concurrency_group_is_a_literal_not_a_per_run_expression(self) -> None:
        """A literal is the only form that cannot drift per commit."""
        group = _text(self.job["concurrency"]["group"])

        self.assertEqual(group, "publish-image")

    def test_cancel_in_progress_is_false(self) -> None:
        """Serialising is only meaningful if the queued run is not cancelled.

        With `cancel-in-progress: true` the newer run would kill the older one,
        which reorders publishes instead of ordering them.
        """
        self.assertIs(self.job["concurrency"]["cancel-in-progress"], False)


class TriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load(PUBLISH)

    def test_named_publish_image(self) -> None:
        self.assertEqual(self.document["name"], "publish-image")

    def test_trigger_is_checks_completion_only(self) -> None:
        trigger = self.document["on"]
        self.assertEqual(sorted(trigger), ["workflow_run"])
        run = trigger["workflow_run"]
        self.assertEqual(run["workflows"], ["checks"])
        self.assertEqual(run["types"], ["completed"])
        self.assertEqual(run["branches"], ["main"])

    def test_never_triggers_on_pull_request_or_push(self) -> None:
        trigger = self.document["on"]
        for forbidden in ("pull_request", "pull_request_target", "push", "schedule"):
            self.assertNotIn(forbidden, trigger)

    def test_gates_on_success_push_and_main(self) -> None:
        condition = _text(_build_job(self.document).get("if"))
        for clause in (
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.event == 'push'",
            "github.event.workflow_run.head_branch == 'main'",
        ):
            self.assertIn(clause, condition)

    def test_no_step_bypasses_the_job_gate(self) -> None:
        """A step-level `if` could re-open the run the job gate closed."""
        for step in _steps(_build_job(self.document)):
            self.assertNotIn("if", step, f"step {_text(step.get('name'))} has an if:")


class PermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load(PUBLISH)

    def test_permissions_are_contents_read_and_packages_write(self) -> None:
        self.assertEqual(
            self.document["permissions"], {"contents": "read", "packages": "write"}
        )

    def test_no_write_all_or_id_token_escalation(self) -> None:
        text = PUBLISH.read_text(encoding="utf-8")
        self.assertNotIn("write-all", text)
        self.assertNotIn("id-token", text)

    def test_checks_workflow_stays_read_only(self) -> None:
        """checks.yml must not gain packages: write, or a PR could reach GHCR."""
        checks = CHECKS.read_text(encoding="utf-8")
        self.assertNotIn("packages:", checks)
        self.assertNotIn("permissions:", checks)


class ImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load(PUBLISH)
        self.job = _build_job(self.document)
        self.build = _step_using(self.job, "docker/build-push-action")
        self.meta = _step_using(self.job, "docker/metadata-action")["with"]
        self.tags = [_text(t) for t in _text(self.meta["tags"]).splitlines() if _text(t)]

    def test_image_reference_is_lowercase_ghcr(self) -> None:
        """GHCR rejects an uppercase path; ``github.repository`` has one."""
        self.assertEqual(_resolve_env(self.meta["images"], self.document), IMAGE)
        self.assertEqual(IMAGE, IMAGE.lower())
        self.assertTrue(IMAGE.startswith("ghcr.io/"))

    def test_image_reference_is_not_derived_from_github_repository(self) -> None:
        self.assertNotIn("github.repository", _text(self.meta["images"]))

    def test_pushes_both_latest_and_seven_character_sha(self) -> None:
        self.assertIn("latest", self.tags)
        self.assertTrue(
            any(tag.startswith("sha-") for tag in self.tags), self.tags
        )
        self.assertFalse(any(tag == "main" for tag in self.tags), self.tags)

    def test_sha_tag_is_built_from_the_seven_character_output(self) -> None:
        sha_tags = [tag for tag in self.tags if tag.startswith("sha-")]
        self.assertEqual(sha_tags, ["sha-${{ steps.commit.outputs.short }}"])

    def test_short_sha_is_sliced_to_seven(self) -> None:
        """#16 pins `sha-<7>`; `--short` would grow silently and break it."""
        derive = [s for s in _steps(self.job) if s.get("id") == "commit"][0]
        body = derive["run"]
        self.assertIn("HEAD_SHA:0:7", body)
        self.assertNotIn("rev-parse --short", body)

    def test_targets_linux_amd64_only(self) -> None:
        self.assertEqual(self.build["with"]["platforms"], "linux/amd64")

    def test_pushes_instead_of_loading(self) -> None:
        self.assertIs(self.build["with"]["push"], True)
        self.assertNotIn("load", self.build["with"])

    def test_builds_from_the_checked_out_tree(self) -> None:
        self.assertEqual(self.build["with"]["context"], ".")

    def test_checkout_pins_the_tested_commit(self) -> None:
        """workflow_run defaults to main's tip, not the commit checks validated."""
        checkout = _step_using(self.job, "actions/checkout")
        self.assertEqual(
            checkout["with"]["ref"], "${{ github.event.workflow_run.head_sha }}"
        )


class LabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load(PUBLISH)
        raw = _step_using(_build_job(self.document), "docker/metadata-action")["with"]
        self.labels = [_text(line) for line in _text(raw["labels"]).splitlines() if _text(line)]

    def _label(self, name: str) -> str:
        for label in self.labels:
            if label.startswith(name + "="):
                return label.split("=", 1)[1]
        raise AssertionError(f"missing label {name}; have {self.labels}")

    def test_source_label_points_at_the_repository(self) -> None:
        """#16 prunes by this label; a wrong URL silently excludes the image."""
        self.assertEqual(
            _resolve_env(self._label("org.opencontainers.image.source"), self.document),
            REPO_URL,
        )

    def test_revision_label_is_the_tested_sha(self) -> None:
        self.assertEqual(
            self._label("org.opencontainers.image.revision"),
            "${{ github.event.workflow_run.head_sha }}",
        )

    def test_title_and_description_are_present(self) -> None:
        self.assertTrue(self._label("org.opencontainers.image.title"))
        self.assertTrue(self._label("org.opencontainers.image.description"))

    def test_no_label_embeds_a_secret(self) -> None:
        for label in self.labels:
            self.assertNotIn("secrets.", label)


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.job = _build_job(_load(PUBLISH))

    def test_logs_in_to_ghcr_with_the_builtin_token(self) -> None:
        login = _step_using(self.job, "docker/login-action")["with"]
        self.assertEqual(login["registry"], "ghcr.io")
        self.assertEqual(login["password"], "${{ secrets.GITHUB_TOKEN }}")

    def test_the_only_secret_is_the_github_token(self) -> None:
        text = PUBLISH.read_text(encoding="utf-8")
        referenced = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", text))
        self.assertEqual(referenced, {"GITHUB_TOKEN"})


class HygieneTests(unittest.TestCase):
    """The image and the workflow must ship no credential and no real identity."""

    def setUp(self) -> None:
        self.text = PUBLISH.read_text(encoding="utf-8")

    def test_no_credential_shaped_strings(self) -> None:
        for pattern in (
            r"ghp_[A-Za-z0-9]{20,}",
            r"github_pat_[A-Za-z0-9_]{20,}",
            r"AKIA[0-9A-Z]{16}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"\bsk-[A-Za-z0-9]{20,}",
        ):
            self.assertIsNone(re.search(pattern, self.text), pattern)

    def test_no_email_addresses(self) -> None:
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", self.text))

    def test_no_ip_addresses(self) -> None:
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", self.text))

    def test_reads_no_dotenv_file(self) -> None:
        self.assertNotIn(".env", self.text)

    def test_no_compose_or_host_side_commands(self) -> None:
        """The workflow publishes; it must not touch the live stack."""
        for forbidden in ("docker compose", "systemctl", "rsync", "ssh "):
            self.assertNotIn(forbidden, self.text)

    def test_hostnames_are_limited_to_known_registries(self) -> None:
        hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", self.text))
        hosts |= {host.split("/")[0] for host in re.findall(r"\bghcr\.io/\S+", self.text)}
        self.assertEqual(hosts - {"github.com", "ghcr.io"}, set())


class DockerfileTests(unittest.TestCase):
    """What the workflow publishes must be the shipped set, and only that.

    ``check.py`` enforces Dockerfile/``LOCAL_MODULES`` agreement, so this does
    not re-implement it. The expected set is derived from the checkout instead:
    every root-level ``.py`` file is a shipped module, and the comparison still
    catches the case a literal would -- a module dropped from the Dockerfile and
    ``LOCAL_MODULES`` together would keep ``check.py`` green while vanishing
    from the image.
    """

    def setUp(self) -> None:
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.shipped = {path.stem for path in ROOT.glob("*.py")}

    def test_dockerfile_copies_every_shipped_module(self) -> None:
        copy_line = [
            line
            for line in self.dockerfile.splitlines()
            if line.upper().startswith("COPY") and ".py" in line
        ]
        self.assertTrue(copy_line, "Dockerfile has no COPY of .py files")
        copied = {word.strip(".,").removesuffix(".py")
                  for word in copy_line[0].split() if word.endswith(".py")}
        # No literal to go stale: the checkout is the only source of truth, so
        # adding a module cannot make this test disagree with reality.
        self.assertEqual(copied, self.shipped)

    def test_dockerfile_has_no_build_time_secret(self) -> None:
        for forbidden in ("ARG ", ".env", "PASSWORD", "TOKEN"):
            self.assertNotIn(forbidden, self.dockerfile)


class IntegrationTests(unittest.TestCase):
    def test_checks_workflow_is_named_what_the_trigger_expects(self) -> None:
        """workflow_run matches on the workflow's `name`, not its file name."""
        self.assertEqual(_load(CHECKS)["name"], "checks")

    def test_publish_workflow_listed_as_a_workflow_file(self) -> None:
        self.assertTrue(PUBLISH.is_file())
        self.assertEqual(sorted(p.name for p in WORKFLOWS.glob("*.y*ml")),
                         ["checks.yml", "publish-image.yml"])


if __name__ == "__main__":
    unittest.main()
