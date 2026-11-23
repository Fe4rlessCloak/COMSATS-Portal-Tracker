#!/usr/bin/env python3
"""Static guardrails for the COMSATS Portal Tracker.

Three checks that the test suite cannot make for itself:

1. Python 3.9 grammar -- the container image runs a newer interpreter than the
   floor this project promises, so a 3.10+ construct (``match``, ``X | Y``
   annotations at runtime) would pass locally and break in the wild.
2. Standard-library-only imports -- the project has no third-party
   dependencies by design; one stray ``pip install`` would break the image.
3. Local module graph -- the flat modules must stay importable from a clean
   checkout, which means no import of a module that is not shipped.

Run directly (``python3 scripts/check.py``) or via CI. Exit code 0 means clean.
"""

from __future__ import annotations

import ast
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


def _sources() -> list[Path]:
    files: list[Path] = []
    for pattern in SOURCE_GLOBS:
        files.extend(sorted(ROOT.glob(pattern)))
    return [path for path in files if "__pycache__" not in path.parts]


def check_grammar(paths: list[Path]) -> list[str]:
    """Every file must parse under the oldest interpreter we support."""
    problems: list[str] = []
    for path in paths:
        try:
            ast.parse(path.read_text(encoding="utf-8"), feature_version=PYTHON_FLOOR)
        except SyntaxError as exc:
            problems.append(
                f"{path.relative_to(ROOT)}:{exc.lineno}: "
                f"not valid Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}: {exc.msg}"
            )
    return problems


def check_stdlib_only(paths: list[Path]) -> list[str]:
    """No third-party imports: every top-level package must be stdlib or local."""
    allowed = set(STDLIB_MODULES) | LOCAL_MODULES | _test_module_names(paths)
    problems: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for name in _imported_roots(node):
                if name not in allowed:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}: "
                        f"third-party import {name!r} (stdlib only)"
                    )
    return problems


def _test_module_names(paths: list[Path]) -> set[str]:
    """Sibling test helpers are local modules too (``support_*``, ``test_*``)."""
    return {path.stem for path in paths if path.parent.name == "tests"}


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
    """Local imports must name a module that actually ships."""
    problems: list[str] = []
    for name in sorted(LOCAL_MODULES):
        if not (ROOT / f"{name}.py").exists():
            problems.append(f"{name}.py: listed as a local module but missing")
    return problems


def main() -> int:
    paths = _sources()
    problems = check_grammar(paths) + check_stdlib_only(paths) + check_shipped_modules()
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s) found.", file=sys.stderr)
        return 1
    print(f"OK: {len(paths)} files parse as Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}, "
          f"imports are stdlib-only, local modules all ship.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
