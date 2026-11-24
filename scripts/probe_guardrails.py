#!/usr/bin/env python3
"""Prove scripts/check.py fails in every direction it claims to catch.

Each probe copies the checkout into a temporary directory, breaks exactly one
thing there, runs ``check.py --root <copy>``, and asserts the run failed with a
``[FAIL]`` line and no traceback. One probe asserts the opposite direction: real
stdlib imports must pass. Nothing in this checkout is written at any point, so
the script is safe to run anywhere, at any time, including mid-edit.

Run: python3 scripts/probe_guardrails.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check.py"

# The guardrail reads sources, the Dockerfile, tests/, and the two module-map
# docs -- nothing else. The docs are copied because `check_doc_module_tables`
# reads them; a probe that breaks a row needs the file present in the copy.
COPIED_PATTERNS = (
    "*.py",
    "Dockerfile",
    "tests/*.py",
    "scripts/*.py",
    "AGENTS.md",
    "ARCHITECTURE.md",
)


class Expectation(Enum):
    """What a well-behaved guardrail does with the probe's broken checkout."""

    MUST_FAIL = "fail"
    MUST_PASS = "pass"


@dataclass(frozen=True)
class Probe:
    """One way the guardrail is supposed to behave."""

    label: str
    break_it: Callable[[Path], None]
    expectation: Expectation = Expectation.MUST_FAIL


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _write_bytes(root: Path, relative: str, payload: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _append(root: Path, relative: str, text: str) -> None:
    path = root / relative
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _make_cycle(root: Path, first: str, second: str) -> None:
    """Make ``first.py`` and ``second.py`` import each other."""
    _append(root, f"{first}.py", f"\nimport {second}\n")
    _append(root, f"{second}.py", f"\nimport {first}\n")


def _set_dockerfile(root: Path, transform: Callable[[str], str]) -> None:
    dockerfile = root / "Dockerfile"
    original = dockerfile.read_text(encoding="utf-8")
    updated = transform(original)
    if updated == original:
        raise AssertionError("probe precondition failed: Dockerfile unchanged")
    dockerfile.write_text(updated, encoding="utf-8")


def _drop_doc_row(root: Path, relative: str, module: str) -> None:
    """Delete the module-map row for ``module`` from a doc, leaving the rest."""
    path = root / relative
    original = path.read_text(encoding="utf-8")
    kept = [
        line
        for line in original.splitlines(keepends=True)
        if not line.startswith(f"| `{module}.py`")
    ]
    if len(kept) == len(original.splitlines(keepends=True)):
        raise AssertionError(
            f"probe precondition failed: {relative} has no `{module}.py` row"
        )
    path.write_text("".join(kept), encoding="utf-8")


def _replace_copy_line(text: str, replacement: str) -> str:
    """Swap the first ``COPY ... .py ...`` instruction for ``replacement``."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().upper().startswith("COPY") and ".py" in line:
            lines[index] = replacement
            return "\n".join(lines) + "\n"
    raise AssertionError("probe precondition failed: Dockerfile has no COPY of .py files")


PROBES = (
    Probe(
        "syntax error is reported, not raised",
        lambda root: _write(root, "zz_syntax.py", "def broken(:\n    pass\n"),
    ),
    Probe(
        "3.10 match statement",
        lambda root: _write(
            root,
            "zz_match.py",
            "from __future__ import annotations\n"
            "def pick(x):\n"
            "    match x:\n"
            "        case 1:\n"
            "            return 'one'\n"
            "    return None\n",
        ),
    ),
    Probe(
        "3.11 except* statement",
        lambda root: _write(
            root,
            "zz_except_star.py",
            "from __future__ import annotations\n"
            "try:\n    pass\nexcept* TypeError:\n    pass\n",
        ),
    ),
    Probe(
        "3.12 type alias statement",
        lambda root: _write(
            root, "zz_typealias.py", "from __future__ import annotations\ntype X = int\n"
        ),
    ),
    Probe(
        "non-UTF-8 bytes are reported, not raised",
        lambda root: _write_bytes(
            root, "zz_latin1.py", b"from __future__ import annotations\nx = 1\n\xff\xfe\n"
        ),
    ),
    Probe(
        "third-party import",
        lambda root: _write(
            root,
            "zz_thirdparty.py",
            "from __future__ import annotations\nimport requests\n",
        ),
    ),
    Probe(
        "unknown module is not silently allowed",
        lambda root: _write(
            root,
            "zz_unknown.py",
            "from __future__ import annotations\nimport not_a_real_module\n",
        ),
    ),
    Probe(
        "real stdlib imports are allowed",
        lambda root: _write(
            root,
            "zz_stdlib.py",
            "from __future__ import annotations\n"
            "import math\nimport secrets\nimport configparser\n"
            "import statistics\nimport zoneinfo\nimport graphlib\n",
        ),
        expectation=Expectation.MUST_PASS,
    ),
    Probe(
        "test helper imported from a shipped module",
        lambda root: _write(
            root,
            "zz_testscope.py",
            "from __future__ import annotations\nimport support_stub_transport\n",
        ),
    ),
    Probe(
        "missing __future__ annotations",
        lambda root: _write(root, "zz_future.py", "x: int = 1\n"),
    ),
    Probe(
        "runtime X | Y union (future import does not defer assignments)",
        lambda root: _write(
            root,
            "zz_union.py",
            "from __future__ import annotations\nAlias = int | str\n",
        ),
    ),
    Probe(
        "legal runtime bit-or is not a union",
        lambda root: _write(
            root,
            "zz_bitflags.py",
            "from __future__ import annotations\nimport re\nimport fcntl\n"
            "FLAGS = re.IGNORECASE | re.DOTALL\nLOCK = fcntl.LOCK_EX | fcntl.LOCK_NB\n",
        ),
        expectation=Expectation.MUST_PASS,
    ),
    Probe(
        "legal bit-or on indexed values is not a union",
        lambda root: _write(
            root,
            "zz_subscript.py",
            "from __future__ import annotations\n"
            "rows = [{1}, {2}]\n"
            "env = {'A': 1, 'B': 2}\n"
            "Mask = rows[0] | rows[1]\n"
            "Both = env['A'] | env['B']\n",
        ),
        expectation=Expectation.MUST_PASS,
    ),
    Probe(
        "subscripted type annotation is a union",
        lambda root: _write(
            root,
            "zz_generic.py",
            "from __future__ import annotations\n"
            "Alias = list[int] | None\n",
        ),
    ),
    Probe(
        "Dockerfile drops a module",
        lambda root: _set_dockerfile(root, lambda text: text.replace(" probe.py", "")),
    ),
    Probe(
        "config.py importing a local module (leaf invariant)",
        lambda root: _append(root, "config.py", "\nfrom notifiers import Notification\n"),
    ),
    Probe(
        "local import cycle between non-leaf modules",
        lambda root: _make_cycle(root, "notifiers", "runtime_store"),
    ),
    Probe(
        "Dockerfile copies an unlisted module",
        lambda root: _set_dockerfile(root, lambda text: text + "\nCOPY zz_ghost.py ./\n"),
    ),
    Probe(
        "Dockerfile glob COPY is compared by what it matches",
        lambda root: _set_dockerfile(
            root,
            lambda text: _replace_copy_line(text, "COPY *.py ./"),
        ),
        expectation=Expectation.MUST_PASS,
    ),
    Probe(
        "a shipped module has no row in the AGENTS.md module table",
        lambda root: _drop_doc_row(root, "AGENTS.md", "session_ui"),
    ),
    Probe(
        "a shipped module has no row in the ARCHITECTURE.md module table",
        lambda root: _drop_doc_row(root, "ARCHITECTURE.md", "runtime_store"),
    ),
    Probe(
        "a module table lists a row nothing ships",
        lambda root: _append(
            root,
            "AGENTS.md",
            "| `zz_phantom.py` | A module that does not exist. |\n",
        ),
    ),
)


def _copy_repo(destination: Path) -> None:
    for pattern in COPIED_PATTERNS:
        for source in ROOT.glob(pattern):
            if "__pycache__" in source.parts or not source.is_file():
                continue
            target = destination / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def _first_fail_line(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("[FAIL]"):
            return line
    stripped = output.strip().splitlines()
    return stripped[0] if stripped else "(no output)"


def _outcome(probe: Probe, result: subprocess.CompletedProcess[str]) -> tuple[str | None, str]:
    """Judge one probe: (failure message or None, the line worth printing)."""
    output = result.stdout + result.stderr
    if probe.expectation is Expectation.MUST_PASS:
        if result.returncode != 0:
            fail = _first_fail_line(output)
            return f"rejected a case it should accept ({fail})", fail
        return None, "exit 0"
    if result.returncode == 0:
        return "guardrail passed a case it should reject (exit 0)", "exit 0 (not caught)"
    if "Traceback (most recent call last)" in output:
        return "raised a traceback instead of reporting a [FAIL] line", "traceback, not [FAIL]"
    if "[FAIL]" not in output:
        return "failed without a [FAIL] line", output.strip()
    return None, _first_fail_line(output)


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="guardrail-baseline-") as tmp:
        clean_root = Path(tmp) / "repo"
        _copy_repo(clean_root)
        baseline = _run(clean_root)
    if baseline.returncode != 0:
        print(f"[FAIL] baseline is not clean:\n{baseline.stdout}{baseline.stderr}")
        return 1
    print(f"[PASS] baseline: {baseline.stdout.strip()}")

    for probe in PROBES:
        with tempfile.TemporaryDirectory(prefix="guardrail-probe-") as tmp:
            root = Path(tmp) / "repo"
            _copy_repo(root)
            try:
                probe.break_it(root)
            except AssertionError as exc:
                failures.append(f"{probe.label}: {exc}")
                print(f"[SKIP] {probe.label}: {exc}")
                continue
            result = _run(root)
        problem, detail = _outcome(probe, result)
        if problem:
            failures.append(f"{probe.label}: {problem}")
            print(f"[FAIL] {probe.label}: {problem}")
        else:
            print(f"[PASS] {probe.label}: {detail}")

    total = len(PROBES) + 1
    if failures:
        print(f"\n{len(failures)} of {total} probes failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nAll {total} probes behaved as advertised.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
