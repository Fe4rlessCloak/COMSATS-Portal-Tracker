#!/usr/bin/env python3
"""Prove scripts/check.py fails in every direction it claims to catch.

Each probe writes a temporary file into the repo, runs the guardrail as a
subprocess, then removes the file. Nothing is committed and the repo is left
byte-identical: the probes only add and delete untracked scratch files.

Run: python3 scripts/probe_guardrails.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBES: list[tuple[str, str, str]] = []


def probe(label: str, filename: str, source: str) -> None:
    PROBES.append((label, filename, source))


probe(
    "syntax error does not crash the run",
    "guardrail_probe_syntax.py",
    "def broken(:\n    pass\n",
)

probe(
    "third-party import",
    "guardrail_probe_thirdparty.py",
    "from __future__ import annotations\nimport requests\n",
)

probe(
    "test helper imported from a shipped module",
    "guardrail_probe_testscope.py",
    "from __future__ import annotations\nimport support_stub_transport\n",
)

probe(
    "missing __future__ annotations",
    "guardrail_probe_future.py",
    "x: int = 1\n",
)

probe(
    "unknown stdlib name is not silently allowed",
    "guardrail_probe_totally_unknown.py",
    "from __future__ import annotations\nimport not_a_real_module\n",
)


def run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def main() -> int:
    failures: list[str] = []

    clean = run()
    if clean.returncode != 0:
        print(f"[FAIL] baseline is not clean:\n{clean.stdout}{clean.stderr}")
        return 1
    print(f"[PASS] baseline: {clean.stdout.strip()}")

    for label, filename, source in PROBES:
        path = ROOT / filename
        path.write_text(source, encoding="utf-8")
        try:
            result = run()
        finally:
            path.unlink()
        output = result.stdout + result.stderr
        crash = "Traceback (most recent call last)" in output
        if result.returncode == 0:
            failures.append(f"{label}: guardrail passed a file it should reject")
            print(f"[FAIL] {label}: exit 0 (not caught)")
        elif crash:
            failures.append(f"{label}: raised a traceback instead of reporting")
            print(f"[FAIL] {label}: traceback instead of [FAIL] line")
        else:
            print(f"[PASS] {label}: {output.strip().splitlines()[0]}")

    # A module listed as local but absent from the Dockerfile COPY must fail.
    dockerfile = ROOT / "Dockerfile"
    original = dockerfile.read_text(encoding="utf-8")
    stripped = original.replace(" probe.py", "")
    if stripped == original:
        failures.append("dockerfile probe: could not remove probe.py from COPY")
    else:
        dockerfile.write_text(stripped, encoding="utf-8")
        try:
            result = run()
        finally:
            dockerfile.write_text(original, encoding="utf-8")
        if result.returncode == 0:
            failures.append("dockerfile drop: module missing from image not caught")
            print("[FAIL] dockerfile drop: exit 0 (not caught)")
        else:
            print(f"[PASS] dockerfile drop: {(result.stdout + result.stderr).strip().splitlines()[0]}")

    # A COPY'd module that is not in LOCAL_MODULES must fail.
    with_extra = original.replace("COPY config.py", "COPY config.py extra_module.py")
    dockerfile.write_text(with_extra, encoding="utf-8")
    try:
        result = run()
    finally:
        dockerfile.write_text(original, encoding="utf-8")
    if result.returncode == 0:
        failures.append("dockerfile ghost: COPY of an unlisted module not caught")
        print("[FAIL] dockerfile ghost: exit 0 (not caught)")
    else:
        print(f"[PASS] dockerfile ghost: {(result.stdout + result.stderr).strip().splitlines()[0]}")

    final = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    if final.strip():
        print(f"\n[NOTE] working tree changed by the probes:\n{final}")

    if failures:
        print(f"\n{len(failures)} probe(s) failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nAll {len(PROBES) + 3} probes behaved as advertised.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
