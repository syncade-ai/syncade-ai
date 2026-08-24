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


# --- The gate must refuse an enumeration it could not make (PR-h-15 item 1) -------------------
#
# `git ls-files` used to live in the loop's process substitution, where its failure is invisible
# to `set -euo pipefail`: the loop read nothing, found nothing over the limit, and printed the
# success line. Same class as the non-numeric limit above — a gate that cannot see what it is
# checking must fail, not pass — so these live beside it rather than in a new file.


def _fixture(tmp_path: Path, *, git: bool, track: bool) -> Path:
    """A tree containing a 900-line violator, in one of three enumeration states."""
    root = tmp_path / "fixture"
    (root / "src" / "syncade").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "check-loc.sh").write_bytes(_SCRIPT.read_bytes())
    (root / "src" / "syncade" / "huge.py").write_text(
        "\n".join(f"x{i} = {i}" for i in range(900)), encoding="utf-8"
    )
    if git:
        subprocess.run(["git", "init", "-q", "."], cwd=root, check=True, capture_output=True)
        if track:
            subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    return root


def _run_in(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/check-loc.sh", "500"], cwd=root, capture_output=True, text=True
    )


def test_a_tracked_violation_is_caught(tmp_path):
    """The CONTROL. Without it the two refusal tests below could pass because the fixture's
    violator is undetectable, rather than because the gate refused."""
    result = _run_in(_fixture(tmp_path, git=True, track=True))
    assert result.returncode == 1, f"the fixture must plant a detectable violation: {result}"
    assert "900 code LOC > 500" in result.stdout


def test_no_git_repository_is_refused_not_passed(tmp_path):
    result = _run_in(_fixture(tmp_path, git=False, track=False))
    assert result.returncode == 2, f"expected the could-not-check code: {result}"
    assert _SUCCESS_PREFIX not in result.stdout, "reported success over an unreadable tree"


def test_an_empty_tracked_set_is_refused_not_passed(tmp_path):
    """A real repository whose tracked set is empty: git succeeds and yields nothing, which is
    indistinguishable from a clean tree by exit code alone."""
    result = _run_in(_fixture(tmp_path, git=True, track=False))
    assert result.returncode == 2, f"expected the could-not-check code: {result}"
    assert _SUCCESS_PREFIX not in result.stdout, "reported success over an empty enumeration"
