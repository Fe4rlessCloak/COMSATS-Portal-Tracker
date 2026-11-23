#!/usr/bin/env python3
"""Static guardrails for the COMSATS Portal Tracker.

Four checks that the test suite cannot make for itself:

1. Python 3.9 grammar -- the container image runs a newer interpreter than the
   floor this project promises, so a 3.10+ construct (``match``, ``X | Y``
   annotations at runtime) would pass locally and break in the wild.
2. Mandatory ``from __future__ import annotations`` -- what makes a ``X | Y``
   annotation safe on 3.9. ``ast.parse(..., feature_version=(3, 9))`` accepts
   ``int | str`` as ordinary bitwise-OR syntax, so the grammar check alone
   cannot see the hazard; the future import is checked instead.
3. Standard-library-only imports -- the project has no third-party
   dependencies by design; one stray ``pip install`` would break the image.
   Test-sibling modules are only importable from files under ``tests/``.
4. Shipped-module integrity -- every name in ``LOCAL_MODULES`` must exist as
   ``<name>.py`` and appear in the ``Dockerfile``'s ``COPY``, and the
   ``Dockerfile`` must copy nothing the allowlist does not know about.

Each file is read and parsed once, so the checks all see the same syntax tree
and a file that does not parse is reported instead of crashing the run.

Run directly (``python3 scripts/check.py``) or via CI. Exit code 0 means clean.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Modules that ship in this repo and are therefore allowed as imports.
LOCAL_MODULES = {
    "config",
    "notifiers",
    "probe",
    "runtime_store",
    "sis_client",
    "snapshot_transition",
    "tracker",
}

SOURCE_GLOBS = ("*.py", "scripts/*.py", "tests/*.py")

PYTHON_FLOOR = (3, 9)

# ``sys.stdlib_module_names`` only exists on 3.10+, but the supported floor is
# 3.9 -- so the allowlist is spelled out rather than read from the interpreter.
STDLIB_MODULES = frozenset(
    """
    __future__ abc argparse ast asyncio base64 binascii bisect builtins calendar
    collections concurrent contextlib copy copyreg csv ctypes dataclasses datetime
    decimal difflib dis email enum errno faulthandler fcntl filecmp fileinput
    fnmatch fractions functools gc getpass gettext glob gzip hashlib heapq hmac
    html http imaplib importlib inspect io ipaddress itertools json keyword
    linecache locale logging lzma mailbox mimetypes mmap multiprocessing numbers
    operator os pathlib pickle pkgutil platform plistlib pprint pty queue quopri
    random re reprlib select shlex shutil signal smtplib socket socketserver
    sqlite3 ssl stat string struct subprocess sys tarfile tempfile textwrap
    threading time token tokenize traceback tracemalloc types typing unicodedata
    unittest urllib uuid warnings weakref xml zipfile zipimport zlib
    """.split()
)

_COPY_LINE = re.compile(r"^\s*COPY\b(.*)$", re.IGNORECASE)


def _sources() -> list[Path]:
    files: list[Path] = []
    for pattern in SOURCE_GLOBS:
        files.extend(sorted(ROOT.glob(pattern)))
    return [path for path in files if "__pycache__" not in path.parts]


def _parse(paths: list[Path]) -> tuple[dict[Path, ast.AST], list[str]]:
    """Parse every file once under the floor grammar.

    Returns the trees that parsed plus the problems for the ones that did not.
    Files that fail to parse are absent from the tree map, so later checks skip
    them rather than crashing on the same syntax error a second time.
    """
    trees: dict[Path, ast.AST] = {}
    problems: list[str] = []
    for path in paths:
        try:
            trees[path] = ast.parse(
                path.read_text(encoding="utf-8"), feature_version=PYTHON_FLOOR
            )
        except SyntaxError as exc:
            problems.append(
                f"{path.relative_to(ROOT)}:{exc.lineno}: "
                f"not valid Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}: {exc.msg}"
            )
    return trees, problems


def check_future_annotations(trees: dict[Path, ast.AST]) -> list[str]:
    """Every file must opt into deferred annotations.

    On 3.9 ``x: int | str`` evaluates ``int | str`` at function-definition time
    and raises ``TypeError``; with the future import the annotation is a string
    and never evaluates. The grammar floor cannot catch it, so the import is
    mandatory -- the same rule ``AGENTS.md`` states.
    """
    problems: list[str] = []
    for path, tree in trees.items():
        if _has_future_annotations(tree):
            continue
        problems.append(
            f"{path.relative_to(ROOT)}:1: missing 'from __future__ import annotations' "
            f"(required: keeps 'X | Y' annotations from evaluating on "
            f"{PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]})"
        )
    return problems


def _has_future_annotations(tree: ast.AST) -> bool:
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.ImportFrom) or node.module != "__future__":
            continue
        if any(alias.name == "annotations" for alias in node.names):
            return True
    return False


def check_stdlib_only(trees: dict[Path, ast.AST]) -> list[str]:
    """No third-party imports: every top-level package must be stdlib or local."""
    test_modules = _test_module_names()
    problems: list[str] = []
    for path, tree in trees.items():
        allowed = set(STDLIB_MODULES) | LOCAL_MODULES
        if _is_test_file(path):
            # Sibling helpers (``support_*``, ``test_*``) are only importable
            # from inside tests/ -- shipped modules must not reach into them.
            allowed |= test_modules
        for node in ast.walk(tree):
            for name in _imported_roots(node):
                if name not in allowed:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}: "
                        f"third-party import {name!r} (stdlib only)"
                    )
    return problems


def _is_test_file(path: Path) -> bool:
    return path.parent.name == "tests"


def _test_module_names() -> set[str]:
    """Sibling test helpers are local modules too (``support_*``, ``test_*``)."""
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir():
        return set()
    return {path.stem for path in tests_dir.glob("*.py")}


def _imported_roots(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        # Relative imports have no top-level package to check.
        if node.level or not node.module:
            return []
        return [node.module.split(".")[0]]
    return []


def check_shipped_modules() -> list[str]:
    """Local modules, the checkout, and the Dockerfile must agree."""
    problems: list[str] = []
    for name in sorted(LOCAL_MODULES):
        if not (ROOT / f"{name}.py").exists():
            problems.append(f"{name}.py: listed as a local module but missing")
    copied = _dockerfile_copies()
    if copied is None:
        problems.append("Dockerfile: unreadable, cannot verify the image ships the modules")
        return problems
    for name in sorted(LOCAL_MODULES - copied):
        problems.append(
            f"{name}.py: listed as a local module but the Dockerfile does not COPY it"
        )
    for name in sorted(copied - LOCAL_MODULES):
        problems.append(
            f"{name}.py: COPY'd by the Dockerfile but missing from LOCAL_MODULES"
        )
    return problems


def _dockerfile_copies() -> set[str] | None:
    """Top-level module names the Dockerfile's ``COPY`` instructions ship.

    Handles backslash-continued instructions and ``--chown``-style flags. Only
    explicitly named ``.py`` sources are collected: a directory or glob source
    cannot be enumerated statically, so it contributes nothing.
    """
    dockerfile = ROOT / "Dockerfile"
    if not dockerfile.is_file():
        return None
    text = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ")
    names: set[str] = set()
    for line in text.splitlines():
        match = _COPY_LINE.match(line)
        if not match:
            continue
        parts = [part for part in match.group(1).split() if not part.startswith("--")]
        if len(parts) < 2:
            continue
        for source in parts[:-1]:
            if not source.endswith(".py"):
                continue
            names.add(Path(source).stem)
    return names


def main() -> int:
    paths = _sources()
    trees, problems = _parse(paths)
    problems += check_future_annotations(trees)
    problems += check_stdlib_only(trees)
    problems += check_shipped_modules()
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s) found.", file=sys.stderr)
        return 1
    print(f"OK: {len(trees)} files parse as Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}, "
          "annotations are deferred, imports are stdlib-only, local modules all ship.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
