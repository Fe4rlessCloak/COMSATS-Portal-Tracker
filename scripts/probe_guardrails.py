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
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check.py"

# The guardrail reads sources, the Dockerfile, and tests/ -- nothing else.
COPIED_PATTERNS = ("*.py", "Dockerfile", "tests/*.py", "scripts/*.py")

MUST_FAIL = "fail"
MUST_PASS = "pass"


@dataclass(frozen=True)
class Probe:
    """One way the guardrail is supposed to behave."""

    label: str
    break_it: Callable[[Path], None]
    expectation: str = MUST_FAIL


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _write_bytes(root: Path, relative: str, payload: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _set_dockerfile(root: Path, transform: Callable[[str], str]) -> None:
    dockerfile = root / "Dockerfile"
    original = dockerfile.read_text(encoding="utf-8")
    updated = transform(original)
    if updated == original:
        raise AssertionError("probe precondition failed: Dockerfile unchanged")
    dockerfile.write_text(updated, encoding="utf-8")


PROBES = (
    Probe(
        "syntax error is reported, not raised",
        lambda root: _write(root, "zz_syntax.py", "def broken(:\n    pass\n"),
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
        expectation=MUST_PASS,
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
            "from __future__ import annotations\nimport re\n"
            "FLAGS = re.IGNORECASE | re.DOTALL\n",
        ),
        expectation=MUST_PASS,
    ),
    Probe(
        "Dockerfile drops a module",
        lambda root: _set_dockerfile(root, lambda text: text.replace(" probe.py", "")),
    ),
    Probe(
        "Dockerfile copies an unlisted module",
        lambda root: _set_dockerfile(root, lambda text: text + "\nCOPY zz_ghost.py ./\n"),
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


def _judge(probe: Probe, result: subprocess.CompletedProcess[str]) -> str | None:
    """Return a failure message, or None when the probe behaved."""
    output = result.stdout + result.stderr
    if probe.expectation == MUST_PASS:
        if result.returncode != 0:
            return f"rejected a case it should accept ({_first_fail_line(output)})"
        return None
    if result.returncode == 0:
        return "guardrail passed a case it should reject (exit 0)"
    if "Traceback (most recent call last)" in output:
        return "raised a traceback instead of reporting a [FAIL] line"
    if "[FAIL]" not in output:
        return "failed without a [FAIL] line"
    return None


def _report(probe: Probe, result: subprocess.CompletedProcess[str], problem: str | None) -> None:
    output = result.stdout + result.stderr
    if problem:
        print(f"[FAIL] {probe.label}: {problem}")
    elif probe.expectation == MUST_PASS:
        print(f"[PASS] {probe.label}: exit 0")
    else:
        print(f"[PASS] {probe.label}: {_first_fail_line(output)}")


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
        problem = _judge(probe, result)
        if problem:
            failures.append(f"{probe.label}: {problem}")
        _report(probe, result, problem)

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
