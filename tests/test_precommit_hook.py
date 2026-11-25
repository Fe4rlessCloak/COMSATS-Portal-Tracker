#!/usr/bin/env python3
"""Contract tests for the opt-in pre-commit hook (``.githooks/pre-commit``).

The hook is the one guardrail in this repo that no CI job can exercise: it runs
on a developer's machine, before a push. A hook that is silently broken, or that
is not executable, is worse than no hook at all -- it looks installed and catches
nothing. So these tests do not read the file's text; they run it.

Three properties are pinned, and each is checked by executing the hook in a
throwaway git repo:

* a clean tree exits 0 and lets the commit through;
* a tree that fails ``scripts/check.py`` exits 1 and blocks the commit;
* the hook is executable, and delegates to the same command CI runs, so the two
  cannot drift apart into a hook that passes what CI rejects.

The hook is also proved to be *opt-in and documented*: ``core.hooksPath`` is a
local git setting, so the repo has to say how to enable it or nobody will.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "pre-commit"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "checks.yml"


def _run(args, cwd, env=None, timeout=120):
    """Run a command, capturing both streams as text, never raising."""
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class HookFileTests(unittest.TestCase):
    """Properties of the file itself, checked without executing it."""

    def test_the_hook_exists(self) -> None:
        self.assertTrue(HOOK.is_file(), f"{HOOK} is missing")

    def test_the_hook_is_executable(self) -> None:
        """Git silently ignores a non-executable hook, so this is load-bearing."""
        mode = HOOK.stat().st_mode
        self.assertTrue(
            mode & stat.S_IXUSR,
            "pre-commit is not executable on disk; git would skip it without a word",
        )

    def test_git_records_the_hook_as_executable(self) -> None:
        """The mode *git* records is what other clones get, not this disk's mode.

        With ``core.fileMode=false`` a ``chmod`` is invisible to git, so a hook
        can be executable here and still ship as ``100644`` -- which git then
        refuses to run in every other clone. Assert the recorded mode, and the
        shipped one, rather than trusting the working tree.
        """
        if shutil.which("git") is None:
            self.skipTest("git is not installed")

        recorded = subprocess.run(
            ["git", "ls-files", "-s", ".githooks/pre-commit"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        entry = recorded.stdout.strip()
        self.assertTrue(entry, ".githooks/pre-commit is not tracked by git")
        self.assertTrue(
            entry.startswith("100755"),
            f"git records the hook as {entry.split()[0]}, so a fresh clone gets a "
            "non-executable hook that git silently skips. Fix with: "
            "git update-index --chmod=+x .githooks/pre-commit",
        )

    def test_the_hook_delegates_to_the_same_command_ci_runs(self) -> None:
        """The hook and the CI workflow must not test different things."""
        workflow = CHECKS_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("python3 scripts/check.py", workflow)

        hook = HOOK.read_text(encoding="utf-8")
        self.assertIn("python3 scripts/check.py", hook)

    def test_the_hook_is_documented_as_opt_in(self) -> None:
        """`core.hooksPath` is local, so the enable command has to be written down."""
        hook = HOOK.read_text(encoding="utf-8")
        self.assertIn("git config core.hooksPath .githooks", hook)

    def test_the_readme_tells_a_developer_how_to_enable_it(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("core.hooksPath .githooks", readme)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class HookBehaviourTests(unittest.TestCase):
    """Run the hook for real, in a throwaway repo, and read its exit code."""

    def _make_repo(self, check_body: str) -> Path:
        """A temp git repo with the real hook and a fake scripts/check.py.

        The fake ``check.py`` is what makes this a test of the *hook* rather than
        a second test of the checker: it exits with whatever the case needs, so
        the hook's pass and fail paths are both reachable in milliseconds.
        """
        root = Path(tempfile.mkdtemp(prefix="precommit-"))
        self.addCleanup(shutil.rmtree, root, True)

        (root / "scripts").mkdir()
        (root / "scripts" / "check.py").write_text(check_body, encoding="utf-8")

        hooks = root / ".githooks"
        hooks.mkdir()
        shutil.copy2(HOOK, hooks / "pre-commit")
        os.chmod(hooks / "pre-commit", 0o755)

        (root / "file.txt").write_text("content\n", encoding="utf-8")
        self._init_repo(root)
        return root

    def _init_repo(self, root: Path) -> None:
        """`git init` plus a local identity and the hook path.

        Both settings are local to the throwaway repo, so the developer's own
        config is never touched.
        """
        self.assertEqual(_run(["git", "init"], cwd=root).returncode, 0)
        for key, value in (
            ("user.email", "test@example.com"),
            ("user.name", "Test"),
            ("commit.gpgsign", "false"),
            ("core.hooksPath", ".githooks"),
        ):
            result = _run(["git", "config", key, value], cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def _commit(self, root: Path):
        added = _run(["git", "add", "-A"], cwd=root)
        self.assertEqual(added.returncode, 0, added.stderr)
        return _run(["git", "commit", "-m", "test commit"], cwd=root)

    def test_a_clean_tree_commits(self) -> None:
        root = self._make_repo("import sys\nsys.exit(0)\n")

        result = self._commit(root)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            _run(["git", "rev-list", "--count", "HEAD"], cwd=root).stdout.strip(), "1"
        )

    def test_a_failing_check_blocks_the_commit(self) -> None:
        root = self._make_repo(
            "import sys\n"
            "print('FAIL: something is wrong', file=sys.stderr)\n"
            "sys.exit(1)\n"
        )

        result = self._commit(root)

        self.assertNotEqual(result.returncode, 0, "the hook let a failing tree commit")
        self.assertIn("scripts/check.py failed", result.stderr)
        # Nothing was committed, so HEAD does not resolve yet.
        head = _run(["git", "rev-list", "--count", "HEAD"], cwd=root)
        self.assertNotEqual(head.stdout.strip(), "1", "a commit was created anyway")

    def test_the_hook_names_the_bypass_for_a_deliberate_commit(self) -> None:
        """A blocked developer needs the escape hatch spelled out, not guessed."""
        root = self._make_repo("import sys\nsys.exit(1)\n")

        result = self._commit(root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--no-verify", result.stderr)

    def test_the_hook_does_not_block_a_repo_without_check_py(self) -> None:
        """An unrelated or very old checkout must not become uncommittable."""
        root = Path(tempfile.mkdtemp(prefix="precommit-bare-"))
        self.addCleanup(shutil.rmtree, root, True)
        hooks = root / ".githooks"
        hooks.mkdir()
        shutil.copy2(HOOK, hooks / "pre-commit")
        os.chmod(hooks / "pre-commit", 0o755)
        (root / "file.txt").write_text("content\n", encoding="utf-8")
        self._init_repo(root)

        result = self._commit(root)

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
