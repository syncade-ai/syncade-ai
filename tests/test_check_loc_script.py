"""Behavioral tests for ``scripts/check-loc.sh`` — the shipped LOC gate.

The script is a mechanical check wired into syncade's own ``.syncade/config.toml``;
its only contract is "exit non-zero iff a tracked .py file exceeds <limit>". A
mistyped (non-numeric) limit must FAIL loudly rather than silently report success
— a gate that can't evaluate its threshold must not pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check-loc.sh"
_SUCCESS_PREFIX = "all tracked src/ + tests/ .py files are within"


def _run(limit: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), limit],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_non_numeric_limit_does_not_silently_pass():
    """A mistyped limit previously emitted integer-expression errors but still
    exited 0 with a success message — silently disabling the gate. It must now
    exit non-zero and print no success line."""
    result = _run("50O")  # letter O, not zero
    assert result.returncode != 0, (
        "a non-numeric limit must fail the gate, not silently pass:\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert _SUCCESS_PREFIX not in result.stdout


def test_valid_high_limit_passes():
    """The happy path is intact: a large numeric limit no file exceeds exits 0
    with the success message."""
    result = _run("100000")
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert _SUCCESS_PREFIX in result.stdout
    assert "code LOC" in result.stdout


def test_gate_counts_code_lines_not_physical_lines():
    files = subprocess.run(
        ["git", "ls-files", "src", "tests"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    py_files = [
        _REPO_ROOT / file
        for file in files
        if file.endswith(".py") and (_REPO_ROOT / file).is_file()
    ]
    physical_max = max(len(file.read_text(encoding="utf-8").splitlines()) for file in py_files)
    code_max = max(
        sum(
            1
            for line in file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        for file in py_files
    )

    assert code_max < physical_max
    result = _run(str((code_max + physical_max) // 2))
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "code LOC" in result.stdout


def test_resume_round_trip_fixture_is_the_only_targeted_exemption():
    exempt_file = _REPO_ROOT / "tests" / "resume" / "test_round_trip.py"
    exempt_loc = sum(
        1
        for line in exempt_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    result = _run("500")
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert (
        f"tests/resume/test_round_trip.py: {exempt_loc} code LOC > 500 "
        "(exempt: cohesive resume round-trip fixture)"
    ) in result.stdout
    assert "1 targeted exemption" in result.stdout
