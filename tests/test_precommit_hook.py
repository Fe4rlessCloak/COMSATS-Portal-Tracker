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
from typing import Union

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

#: No ``scripts/check.py`` at all, standing in for a checkout that predates it.
NO_CHECKER = object()

HOOK = ROOT / ".githooks" / "pre-commit"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "checks.yml"


def _run(args, cwd, timeout=120):
    """Run a command, capturing both streams as text, never raising."""
    return subprocess.run(
        args,
        cwd=str(cwd),
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
        """The hook and the CI workflow must not test different things.

        Parsed from the workflow's ``run`` steps rather than matched as free
        text: `checks.yml` warns that this repo has been bitten by hardcoded
        lists going stale, and a plain `assertIn` would also be satisfied by the
        hook's own comment, so it could never fail for the drift it claims to pin.
        """
        import support_workflow_yaml

        document = support_workflow_yaml.loads(
            CHECKS_WORKFLOW.read_text(encoding="utf-8")
        )
        runs = [
            step.get("run", "")
            for job in document["jobs"].values()
            for step in job.get("steps", [])
        ]
        self.assertTrue(
            any("scripts/check.py" in run for run in runs),
            "the checks workflow no longer runs scripts/check.py",
        )

        hook = HOOK.read_text(encoding="utf-8")
        # The invocation the hook actually executes, not a mention in a comment.
        self.assertRegex(
            hook,
            r"python3\s+\"?\$?[{\w]*\}?/?scripts/check\.py\"?\s+--root",
            "the hook does not invoke scripts/check.py with --root on the staged tree",
        )

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

    def _make_repo(self, check_body: Union[str, object] = None) -> Path:
        """A temp git repo with the real hook and a ``scripts/check.py``.

        The stub ``check.py`` is what makes this a test of the *hook* rather than
        a second test of the checker: it exits with whatever the case needs, so
        the hook's pass and fail paths are both reachable in milliseconds.

        ``check_body`` selects the checker: a source string writes that stub,
        ``None`` copies the repo's real checker for the cases that must exercise
        the real filesystem walk, and ``NO_CHECKER`` writes none at all to stand
        in for a checkout that predates it.

        The checker is written before the initial stage in `_init_repo`, so the
        index the hook exports is the one these tests intend.
        """
        root = Path(tempfile.mkdtemp(prefix="precommit-"))
        self.addCleanup(shutil.rmtree, root, True)

        (root / "scripts").mkdir()
        if check_body is NO_CHECKER:
            pass
        elif check_body is None:
            shutil.copy2(ROOT / "scripts" / "check.py", root / "scripts" / "check.py")
        else:
            (root / "scripts" / "check.py").write_text(str(check_body), encoding="utf-8")

        hooks = root / ".githooks"
        hooks.mkdir()
        # copy2 carries the repo's committed mode; chmod keeps the test honest
        # even if the working tree's mode drifts from the index.
        shutil.copy2(HOOK, hooks / "pre-commit")
        os.chmod(hooks / "pre-commit", 0o755)

        (root / "file.txt").write_text("content\n", encoding="utf-8")
        self._init_repo(root)
        return root

    def _init_repo(self, root: Path) -> None:
        """`git init`, a local identity, the hook path, and an initial stage.

        Both settings are local to the throwaway repo, so the developer's own
        config is never touched. Staging here rather than in `_commit` is
        deliberate: `git add -A` at commit time would sweep a test's carefully
        partial index into a fully-staged one and hide the very distinction the
        partial-stage test exists to check.
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
        staged = _run(["git", "add", "-A"], cwd=root)
        self.assertEqual(staged.returncode, 0, staged.stderr)

    def _commit(self, root: Path):
        """Commit whatever is already staged, letting the hook decide."""
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

    def test_a_partially_staged_file_is_checked_as_staged(self) -> None:
        """The hook must check the index, not the working tree.

        Stage a broken file, then fix the working-tree copy without staging it.
        A working-tree check passes and the broken content is committed anyway --
        the exact false confidence a guardrail must not give. The hook exports
        the index, so the staged (and therefore committed) content is what gets
        checked.
        """
        broken = (
            "#!/usr/bin/env python3\n"
            '"""Clean module."""\n'
            "from __future__ import annotations\n"
            "\n"
            "import sys\n"
            "import requests\n"
        )
        clean = (
            "#!/usr/bin/env python3\n"
            '"""Clean module."""\n'
            "from __future__ import annotations\n"
            "\n"
            "import sys\n"
        )
        root = self._make_repo()

        (root / "staged.py").write_text(broken, encoding="utf-8")
        self.assertEqual(_run(["git", "add", "staged.py"], cwd=root).returncode, 0)
        # Fix the working tree only. The index still holds the broken content.
        (root / "staged.py").write_text(clean, encoding="utf-8")

        result = self._commit(root)

        self.assertNotEqual(
            result.returncode,
            0,
            "the hook passed a commit whose staged content imports 'requests'",
        )
        self.assertIn("requests", result.stderr)
        head = _run(["git", "rev-list", "--count", "HEAD"], cwd=root)
        self.assertNotEqual(head.stdout.strip(), "1", "a commit was created anyway")

    def test_an_unrelated_untracked_file_is_not_checked(self) -> None:
        """A scratch file outside the index must not block an unrelated commit.

        The inverse of the partial-stage case, and the nuisance risk: a broken
        untracked file is not part of this commit, so it must not stop it.
        """
        root = self._make_repo("import sys\nsys.exit(0)\n")
        (root / "clean.py").write_text(
            "#!/usr/bin/env python3\n"
            '"""Clean module."""\n'
            "from __future__ import annotations\n"
            "\n"
            "import sys\n",
            encoding="utf-8",
        )
        # Deliberately broken, and deliberately left untracked.
        (root / "scratch_notes.py").write_text("import requests\n", encoding="utf-8")

        self.assertEqual(_run(["git", "add", "clean.py"], cwd=root).returncode, 0)
        result = _run(["git", "commit", "-m", "clean only"], cwd=root)

        self.assertEqual(
            result.returncode,
            0,
            "an untracked scratch file blocked an unrelated commit",
        )

    def test_the_hook_does_not_block_a_repo_without_check_py(self) -> None:
        """An unrelated or very old checkout must not become uncommittable."""
        root = self._make_repo(check_body=NO_CHECKER)

        result = self._commit(root)

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
