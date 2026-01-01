"""Smoke tests for :mod:`syncade.test_runner` against real ``pytest``.

PR-7.5: the test re-run leg is the third convergence leg. These
smoke tests run a real ``pytest`` invocation through
:func:`syncade.test_runner.run_tests` against the syncade repo
itself — the smoke target lives in this same repo so the test
doesn't need an external project to drive.

Gated behind ``@pytest.mark.smoke``. The default ``pytest`` run
deselects it via ``addopts = "-m 'not smoke'"``. The smokes skip
cleanly if ``pytest`` itself isn't on PATH (unlikely in the dev
environment, but defensive).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from syncade.test_runner import run_tests


def _skip_if_pytest_missing() -> None:
    """Skip the smoke unless ``pytest`` is on PATH — the smokes
    invoke it via ``sh -c``, so ``which`` is the right check."""
    if shutil.which("pytest") is None:
        pytest.skip("pytest not on PATH")


def _repo_root() -> Path:
    """Repo root for this smoke run. The smoke executes a
    ``pytest`` against tests inside this same repo (cheap, fast,
    self-contained — no external project required)."""
    return Path(__file__).resolve().parents[2]


@pytest.mark.smoke
def test_run_tests_against_real_passing_pytest(tmp_path):
    """A real ``pytest`` invocation against ``tests/findings/test_parser_and_fields.py``
    (a fast, no-network test file) should return
    ``outcome="passed"``, ``exit_code=0``, and meaningful stdout.

    The test runs in the syncade repo itself as the worktree path —
    so ``pytest`` discovers the test file at its real location. The
    cheap ``-q`` flag keeps output bounded; the smoke is intended to
    complete in under 5 seconds against the cached interpreter.
    """
    _skip_if_pytest_missing()
    repo = _repo_root()

    result = run_tests(
        worktree_path=repo,
        test_command="pytest tests/findings/test_parser_and_fields.py -q",
        timeout_seconds=120.0,
    )

    assert result.outcome == "passed", (
        f"unexpected outcome={result.outcome!r}; exit_code={result.exit_code}; "
        f"stderr={result.stderr[:500]!r}"
    )
    assert result.exit_code == 0
    # pytest's own summary line shape is stable.
    assert " passed" in result.stdout
    assert result.duration_seconds > 0.0
    assert result.error is None
    assert result.command == "pytest tests/findings/test_parser_and_fields.py -q"


@pytest.mark.smoke
def test_run_tests_against_real_failing_pytest(tmp_path):
    """A real ``pytest`` invocation pointed at a non-existent test
    file should return ``outcome="failed"``, ``exit_code > 0``,
    and capture pytest's own error message in stderr or stdout.

    pytest's exit codes (per its docs):
    - 0: all tests passed.
    - 1: tests failed.
    - 2: interrupted by user.
    - 3: internal error.
    - 4: pytest command-line usage error.
    - 5: no tests collected.

    "No tests collected" (exit 5) is the cleanest synthetic
    failure shape — pytest reports it without timing out and
    without depending on a specific failing test inside the
    repo. The orchestrator's mechanical verdict treats any
    non-zero exit as ``outcome="failed"``, so this still folds
    correctly into the exit-30 path.
    """
    _skip_if_pytest_missing()
    repo = _repo_root()

    result = run_tests(
        worktree_path=repo,
        test_command="pytest tests/this_file_definitely_does_not_exist.py -q",
        timeout_seconds=30.0,
    )

    assert result.outcome == "failed", (
        f"unexpected outcome={result.outcome!r}; exit_code={result.exit_code}; "
        f"stdout={result.stdout[:500]!r}; stderr={result.stderr[:500]!r}"
    )
    assert result.exit_code > 0
    # pytest "no tests collected" lands in stderr or stdout —
    # accept either as long as the failure shape is visible.
    combined = result.stdout + result.stderr
    assert "no tests" in combined.lower() or "error" in combined.lower()
    assert result.error is None  # NOT a subprocess_error; tests-failed is a clean run
