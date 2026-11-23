#!/usr/bin/env python3
"""Static guardrails for the COMSATS Portal Tracker.

Four checks that the test suite cannot make for itself:

1. Python 3.9 grammar -- the container image runs a newer interpreter than the
   floor this project promises, so a 3.10+ construct (``match``, ``except*``,
   ``type X = int``) would pass locally and break in the wild.
2. Deferred annotations -- ``from __future__ import annotations`` in every file,
   plus no eagerly-evaluated ``X | Y`` outside an annotation. ``ast.parse(...,
   feature_version=(3, 9))`` accepts ``x: int | str`` as plain bitwise-OR
   syntax, so the grammar floor cannot see that hazard; the future import is
   what makes an annotation safe, and an assignment like ``Alias = int | str``
   is not deferred by it at all.
3. Standard-library-only imports -- the project has no third-party
   dependencies by design; one stray ``pip install`` would break the image.
   The allowlist is the Python 3.9 stdlib, so a module introduced later (say
   ``tomllib``) is an error even when the running interpreter has it.
   Test-sibling modules are importable only from files under ``tests/``.
4. Image integrity -- every name in ``LOCAL_MODULES`` must exist as
   ``<name>.py`` and appear in the ``Dockerfile``'s ``COPY``, and the
   ``Dockerfile`` must copy nothing the allowlist does not know about.

Every file is read and parsed once, so the checks share one syntax tree and a
file that cannot be decoded or parsed is reported instead of crashing the run.

``--root DIR`` points the checks at another checkout. ``scripts/probe_guardrails.py``
uses it to build a throwaway copy of the repo, so proving the guardrail fails
never writes into this tree.

Run directly (``python3 scripts/check.py``) or via CI. Exit code 0 means clean.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

# Modules that ship in this repo (as ``<name>.py`` at the root) and are
# therefore allowed as imports.
LOCAL_MODULES = frozenset(
    {
        "config",
        "notifiers",
        "probe",
        "runtime_store",
        "sis_client",
        "snapshot_transition",
        "tracker",
    }
)

SOURCE_GLOBS = ("*.py", "scripts/*.py", "tests/*.py")

PYTHON_FLOOR = (3, 9)

# The Python 3.9 "Python Module Index" (docs.python.org/3.9/py-modindex.html),
# top-level names only. ``sys.stdlib_module_names`` would be the clean way to
# get this, but it does not exist on 3.9 and this script must run there.
# Listing the *floor's* stdlib rather than the running interpreter's is also
# what keeps the promise honest: a module added in 3.10+ (``tomllib``) stays an
# error, which is the entire point of the check.
STDLIB_MODULES = frozenset(
    """
    __future__ __main__ _thread abc aifc argparse array ast asynchat asyncio asyncore
    atexit audioop base64 bdb binascii binhex bisect builtins bz2 cProfile calendar cgi
    cgitb chunk cmath cmd code codecs codeop collections colorsys compileall concurrent
    configparser contextlib contextvars copy copyreg crypt csv ctypes curses dataclasses
    datetime dbm decimal difflib dis distutils doctest email encodings ensurepip enum
    errno faulthandler fcntl filecmp fileinput fnmatch formatter fractions ftplib
    functools gc getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac
    html http imaplib imghdr imp importlib inspect io ipaddress itertools json keyword
    lib2to3 linecache locale logging lzma mailbox mailcap marshal math mimetypes mmap
    modulefinder msilib msvcrt multiprocessing netrc nis nntplib numbers operator
    optparse os ossaudiodev parser pathlib pdb pickle pickletools pipes pkgutil
    platform plistlib poplib posix pprint profile pstats pty pwd py_compile pyclbr
    pydoc queue quopri random re readline reprlib resource rlcompleter runpy sched
    secrets select selectors shelve shlex shutil signal site smtpd smtplib sndhdr
    socket socketserver spwd sqlite3 ssl stat statistics string stringprep struct
    subprocess sunau symbol symtable sys sysconfig syslog tabnanny tarfile telnetlib
    tempfile termios test textwrap threading time timeit tkinter token tokenize trace
    traceback tracemalloc tty turtle turtledemo types typing unicodedata unittest
    urllib uu uuid venv warnings wave weakref webbrowser winreg winsound wsgiref
    xdrlib xml xmlrpc zipapp zipfile zipimport zlib zoneinfo
    """.split()
)

_COPY_INSTRUCTION = re.compile(r"^\s*COPY\b(.*)$", re.IGNORECASE)


def _sources(root: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in SOURCE_GLOBS:
        files.extend(sorted(root.glob(pattern)))
    return [path for path in files if "__pycache__" not in path.parts]


def _label(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _parse(root: Path, paths: list[Path]) -> tuple[dict[Path, ast.AST], list[str]]:
    """Parse every file once under the floor grammar.

    Returns the trees that parsed plus the problems for the rest. A file that
    fails is absent from the tree map, so the later checks skip it instead of
    hitting the same error again -- and no single bad file can raise out of
    ``main()``. Both failure modes are covered: bytes that are not UTF-8, and
    source that does not parse.
    """
    trees: dict[Path, ast.AST] = {}
    problems: list[str] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(
                f"{_label(root, path)}: unreadable as UTF-8 text: {exc}"
            )
            continue
        try:
            trees[path] = ast.parse(source, feature_version=PYTHON_FLOOR)
        except SyntaxError as exc:
            problems.append(
                f"{_label(root, path)}:{exc.lineno if exc.lineno is not None else '?'}: "
                f"not valid Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}: {exc.msg}"
            )
    return trees, problems


def check_future_annotations(root: Path, trees: dict[Path, ast.AST]) -> list[str]:
    """Every file must opt into deferred annotations.

    On 3.9 ``x: int | str`` evaluates ``int | str`` at definition time and
    raises ``TypeError``; with the future import the annotation stays a string
    and never evaluates. The grammar floor cannot catch it, so the import is
    mandatory -- the same rule ``AGENTS.md`` states.
    """
    problems: list[str] = []
    for path, tree in trees.items():
        if _has_future_annotations(tree):
            continue
        problems.append(
            f"{_label(root, path)}:1: missing 'from __future__ import annotations' "
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


def check_stdlib_only(root: Path, trees: dict[Path, ast.AST]) -> list[str]:
    """No third-party imports: every top-level package must be stdlib or local."""
    test_modules = _test_module_names(root)
    problems: list[str] = []
    for path, tree in trees.items():
        allowed = set(STDLIB_MODULES) | LOCAL_MODULES
        in_tests = path.parent.name == "tests"
        if in_tests:
            # Sibling helpers (``support_*``, ``test_*``) are importable only
            # from inside tests/ -- shipped modules must not reach into them.
            allowed |= test_modules
        for node in ast.walk(tree):
            for name in _imported_roots(node):
                if name in allowed:
                    continue
                problems.append(
                    f"{_label(root, path)}:{getattr(node, 'lineno', '?')}: "
                    f"{_describe_breach(name, in_tests, test_modules)}"
                )
    return problems


def _describe_breach(name: str, in_tests: bool, test_modules: set[str]) -> str:
    """Name the rule that actually fired, not just the broadest one."""
    if not in_tests and name in test_modules:
        return (
            f"test-only module {name!r} imported outside tests/ "
            f"(test helpers are not shipped)"
        )
    return f"third-party import {name!r} (stdlib only)"


def check_runtime_unions(root: Path, trees: dict[Path, ast.AST]) -> list[str]:
    """``X | Y`` outside an annotation or a ``from __future__`` module.

    ``from __future__ import annotations`` only defers *annotations*. A module
    level ``Alias = int | str`` still evaluates ``int | str`` eagerly, and on
    3.9 ``type.__or__`` does not exist, so it raises ``TypeError`` at import
    time. That is a real 3.9 break that neither the grammar floor nor the
    future-import check can see, so it is flagged here.

    Deliberately narrow: only operands that are bare names or attribute access
    on names are considered, and only where neither side could be a runtime
    ``set``/``dict``/``int``.``|`` expression. Anything else is skipped -- a
    false alarm here would be worse than a missed exotic case.
    """
    problems: list[str] = []
    for path, tree in trees.items():
        inside_annotation = _annotation_node_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
                continue
            if id(node) in inside_annotation:
                continue
            if _looks_like_type_union(node):
                problems.append(
                    f"{_label(root, path)}:{node.lineno}: 'X | Y' evaluated at runtime "
                    f"(3.9 has no PEP 604 -- use typing.Union, or move it into an annotation)"
                )
    return problems


def _annotation_node_ids(tree: ast.AST) -> set[int]:
    """Ids of every node sitting inside an annotation.

    Annotations are deferred by the future import, so a union there is safe on
    3.9; a union anywhere else is not.
    """
    inside: set[int] = set()
    for node in ast.walk(tree):
        for candidate in _annotations_of(node):
            for child in ast.walk(candidate):
                inside.add(id(child))
    return inside


def _annotations_of(node: ast.AST) -> list[ast.expr]:
    """Every annotation expression directly attached to ``node``."""
    if isinstance(node, (ast.AnnAssign, ast.arg)):
        annotation = getattr(node, "annotation", None)
        return [annotation] if annotation is not None else []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        returns = [node.returns] if node.returns is not None else []
        return returns
    return []


def _looks_like_type_union(node: ast.BinOp) -> bool:
    """True when both sides read as types rather than runtime values."""
    return _is_type_operand(node.left) and _is_type_operand(node.right)


def _is_type_operand(node: ast.AST) -> bool:
    """True when ``node`` reads as a type rather than a runtime value.

    ``Class``/``int``/``list[int]`` qualify; ``re.IGNORECASE``,
    ``fcntl.LOCK_EX`` and ``rows[0]`` do not. Two rules do the work:

    * a bare name or attribute counts only when it is capitalised and not
      ALL_CAPS -- constants and flag sets are the common lawful ``|``
      expressions in this codebase;
    * a subscript counts only when it is a *generic type*: the value is a known
      type name closing over types (``list[int]``, ``Optional[Config]``). A
      subscript slicing a runtime container (``rows[0]``, ``env["A"]``) is not,
      and treating every subscript as a type was a false alarm the docstring
      above explicitly forbids.
    """
    if isinstance(node, ast.Name):
        return _looks_like_type_name(node.id)
    if isinstance(node, ast.Attribute):
        return _looks_like_type_name(node.attr)
    if isinstance(node, ast.Constant):
        # ``X | None`` is the common optional-union spelling; no other literal
        # is a type.
        return node.value is None
    if isinstance(node, ast.Subscript):
        return _is_generic_type(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _looks_like_type_union(node)
    return False


def _is_generic_type(node: ast.Subscript) -> bool:
    """``list[int]``/``Optional[Config]`` yes; ``rows[0]``/``env["A"]`` no.

    The base must itself read as a type, and every subscripted argument must be
    either a type or an ``int`` (``tuple[int, str]``) -- so a container indexed
    by a variable or a string literal is excluded.
    """
    return _is_type_operand(node.value) and all(
        _is_type_argument(child) for child in _slice_elements(node.slice)
    )


def _slice_elements(node: ast.AST) -> list[ast.AST]:
    """The subscript arguments, flattened from a tuple when present."""
    if isinstance(node, ast.Tuple):
        return list(node.elts)
    return [node]


def _is_type_argument(node: ast.AST) -> bool:
    """A generic argument: a type, or an ``int`` literal for fixed tuples."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    return _is_type_operand(node)


def _looks_like_type_name(name: str) -> bool:
    """``Foo``/``int`` yes; ``FOO_BAR``/``IGNORECASE`` no."""
    if not name or not name[0].isupper():
        return name in _BUILTIN_TYPES
    return not name.isupper()


_BUILTIN_TYPES = frozenset(
    {"int", "str", "float", "bool", "bytes", "list", "dict", "set", "tuple", "type", "None"}
)


def _test_module_names(root: Path) -> set[str]:
    """Sibling test helpers are local modules too (``support_*``, ``test_*``)."""
    tests_dir = root / "tests"
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


def check_shipped_modules(root: Path) -> list[str]:
    """Local modules, the checkout, and the Dockerfile must agree."""
    problems: list[str] = []
    for name in sorted(LOCAL_MODULES):
        if not (root / f"{name}.py").exists():
            problems.append(f"{name}.py: listed as a local module but missing")
    copied = _dockerfile_copies(root)
    if copied is None:
        problems.append(
            "Dockerfile: missing or unreadable, cannot verify the image contents"
        )
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


def _dockerfile_copies(root: Path) -> set[str] | None:
    """Top-level module names the Dockerfile's ``COPY`` instructions ship.

    Handles backslash-continued instructions and ``--chown``-style flags. Two
    source shapes are understood:

    * explicitly named ``*.py`` sources (``config.py``) contribute that name;
    * a glob or bare directory source (``*.py ./``) is expanded against the
      root, so a Dockerfile that copies everything is compared by what it
      actually copies instead of reporting the literal ``*.py`` as a module
      that is missing from the allowlist.
    """
    try:
        text = (root / "Dockerfile").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    names: set[str] = set()
    for line in text.replace("\\\n", " ").splitlines():
        match = _COPY_INSTRUCTION.match(line)
        if not match:
            continue
        parts = [part for part in match.group(1).split() if not part.startswith("--")]
        if len(parts) < 2:
            continue
        for source in parts[:-1]:
            names |= _copied_module_names(root, source)
    return names


def _copied_module_names(root: Path, source: str) -> set[str]:
    """The module names one ``COPY`` source contributes."""
    if any(ch in source for ch in "*?["):
        # A glob: expand it, so `*.py ./` compares by what it matches.
        return {path.stem for path in root.glob(source) if path.name.endswith(".py")}
    if source == "." or source == "./" or source.endswith("/"):
        return {path.stem for path in root.glob("*.py")}
    if source.endswith(".py"):
        return {Path(source).stem}
    return set()


def run_checks(root: Path) -> tuple[list[str], str]:
    """Return (problems, success summary) for one checkout."""
    paths = _sources(root)
    trees, problems = _parse(root, paths)
    problems += check_future_annotations(root, trees)
    problems += check_runtime_unions(root, trees)
    problems += check_stdlib_only(root, trees)
    problems += check_shipped_modules(root)
    summary = (
        f"OK: {len(trees)} files parse as Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}, "
        "annotations are deferred, imports are stdlib-only, local modules all ship."
    )
    return problems, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Static guardrails for the tracker.")
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="checkout to inspect (default: the repo this script lives in)",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print(f"[FAIL] {root}: not a directory", file=sys.stderr)
        return 1

    problems, summary = run_checks(root)
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s) found.", file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
