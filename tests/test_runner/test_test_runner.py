"""Unit tests for :mod:`syncade.test_runner` — PR-7.5.

The module is intentionally narrow: shell subprocess execution, the
three terminal :class:`TestRunResult` states, and the outcome ↔
exit_code consistency rule. Smoke tests against a real ``pytest``
invocation live in ``tests/smoke/test_test_runner_smoke.py``.
"""

from __future__ import annotations

import sys

import pytest

from syncade.process import (
    SubprocessNotFoundError,
)
from syncade.test_runner import TestRunResult as _TestRunResult
from syncade.test_runner import run_tests
from tests.test_worktree_env import make_worktree_src

# pytest collects classes whose name starts with `Test` — alias the
# imported dataclass so the symbol doesn't trigger
# PytestCollectionWarning ("cannot collect test class with __init__").
# Test classes below DO start with TestRunTest... which is fine
# (they're test classes by design).


# ---------------------------------------------------------------------------
# TestRunResult.__post_init__ — outcome ↔ exit_code consistency
# ---------------------------------------------------------------------------


class TestTestRunResultDataclass:
    """The outcome ↔ exit_code contract: a downstream consumer should
    never have to second-guess whether ``exit_code == 0`` and
    ``outcome == "failed"`` could co-occur. Construction enforces."""

    def test_passed_with_zero_exit_code_ok(self):
        r = _TestRunResult(
            exit_code=0,
            outcome="passed",
            duration_seconds=0.1,
            stdout="ok",
            stderr="",
        )
        assert r.outcome == "passed"
        assert r.exit_code == 0
        assert r.error is None

    def test_failed_with_positive_exit_code_ok(self):
        r = _TestRunResult(
            exit_code=1,
            outcome="failed",
            duration_seconds=0.1,
            stdout="",
            stderr="FAIL",
        )
        assert r.outcome == "failed"
        assert r.exit_code == 1

    def test_subprocess_error_with_exception_ok(self):
        r = _TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=0.1,
            stdout="",
            stderr="",
            error=SubprocessNotFoundError("missing"),
        )
        assert r.outcome == "subprocess_error"
        assert isinstance(r.error, SubprocessNotFoundError)

    def test_passed_with_nonzero_exit_code_rejected(self):
        with pytest.raises(ValueError, match="outcome='passed' requires exit_code=0"):
            _TestRunResult(
                exit_code=1,
                outcome="passed",
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

    def test_failed_with_zero_exit_code_rejected(self):
        with pytest.raises(ValueError, match="outcome='failed' requires exit_code > 0"):
            _TestRunResult(
                exit_code=0,
                outcome="failed",
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

    def test_failed_with_negative_exit_code_rejected(self):
        """Sentinel ``-1`` belongs to ``subprocess_error``, not
        ``failed``. The contract makes the two distinguishable."""
        with pytest.raises(ValueError, match="outcome='failed' requires exit_code > 0"):
            _TestRunResult(
                exit_code=-1,
                outcome="failed",
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

    def test_subprocess_error_without_exception_rejected(self):
        with pytest.raises(ValueError, match="subprocess_error.*non-None error"):
            _TestRunResult(
                exit_code=-1,
                outcome="subprocess_error",
                duration_seconds=0.1,
                stdout="",
                stderr="",
                error=None,
            )

    def test_passed_with_exception_rejected(self):
        """An exception on a successful result is a category error —
        either the subprocess ran (no exception) or it didn't (no
        outcome other than subprocess_error)."""
        with pytest.raises(ValueError, match="error must be None"):
            _TestRunResult(
                exit_code=0,
                outcome="passed",
                duration_seconds=0.1,
                stdout="",
                stderr="",
                error=SubprocessNotFoundError("x"),
            )

    def test_failed_with_exception_rejected(self):
        with pytest.raises(ValueError, match="error must be None"):
            _TestRunResult(
                exit_code=1,
                outcome="failed",
                duration_seconds=0.1,
                stdout="",
                stderr="",
                error=SubprocessNotFoundError("x"),
            )

    # T1.2: tightened post_init — new rejections.

    def test_subprocess_error_with_non_sentinel_exit_code_rejected(self):
        """T1.2: subprocess_error must use exit_code=-1 (the sentinel).
        Without this rule, a caller could construct a result whose
        exit_code disagrees with the subprocess_error outcome —
        manifest.json would echo the bogus exit_code into the
        ``test_run.exit_code`` field while the orchestrator's verdict
        treats it as a subprocess error. Two consumers disagreeing."""
        with pytest.raises(ValueError, match="requires exit_code=-1"):
            _TestRunResult(
                exit_code=42,
                outcome="subprocess_error",
                duration_seconds=0.1,
                stdout="",
                stderr="",
                error=SubprocessNotFoundError("x"),
            )

    def test_subprocess_error_with_zero_exit_code_rejected(self):
        with pytest.raises(ValueError, match="requires exit_code=-1"):
            _TestRunResult(
                exit_code=0,
                outcome="subprocess_error",
                duration_seconds=0.1,
                stdout="",
                stderr="",
                error=SubprocessNotFoundError("x"),
            )

    def test_unknown_outcome_string_rejected(self):
        """T1.2: the Literal type hint is advisory; runtime
        construction with a bogus string slips past type-checking.
        __post_init__ rejects it explicitly."""
        with pytest.raises(ValueError, match="is not one of the three valid"):
            _TestRunResult(
                exit_code=0,
                outcome="bogus",  # type: ignore[arg-type]
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

    def test_none_outcome_rejected(self):
        """T1.2: same as above, but None — which would type-check
        in some loose contexts."""
        with pytest.raises(ValueError, match="is not one of the three valid"):
            _TestRunResult(
                exit_code=0,
                outcome=None,  # type: ignore[arg-type]
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

    def test_empty_string_outcome_rejected(self):
        with pytest.raises(ValueError, match="is not one of the three valid"):
            _TestRunResult(
                exit_code=0,
                outcome="",  # type: ignore[arg-type]
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )


# ---------------------------------------------------------------------------
# run_tests against synthesized shell commands (no real test runner)
# ---------------------------------------------------------------------------


class TestRunTestsHappyPath:
    """``passed`` (exit 0) and ``failed`` (exit > 0) cases against
    cheap shell commands. No real pytest invocation here — that's
    the smoke test's job."""

    def test_passing_command_returns_passed(self, tmp_path):
        """``exit 0`` shell command → ``outcome="passed"``."""
        result = run_tests(
            worktree_path=tmp_path,
            test_command="echo ok && exit 0",
            timeout_seconds=10.0,
        )
        assert result.outcome == "passed"
        assert result.exit_code == 0
        assert "ok" in result.stdout
        assert result.error is None
        assert result.command == "echo ok && exit 0"
        assert result.duration_seconds >= 0.0

    def test_subprocess_imports_worktree_src_not_main(self, tmp_path):
        # PR-23: the authoritative test leg must evaluate the WORKTREE's src,
        # not MAIN's editable-install .pth. Real proof — the command literally
        # `import syncade` and prints where it resolved from.
        worktree = make_worktree_src(tmp_path / "wt")
        worktree_pkg_dir = str((worktree / "src" / "syncade").resolve())
        cmd = f'{sys.executable} -c "import syncade, sys; sys.stdout.write(syncade.__file__)"'
        result = run_tests(
            worktree_path=worktree,
            test_command=cmd,
            timeout_seconds=60.0,
        )
        assert result.outcome == "passed", result.stderr
        assert worktree_pkg_dir in result.stdout

    def test_failing_command_returns_failed(self, tmp_path):
        """``exit 1`` shell command → ``outcome="failed"``."""
        result = run_tests(
            worktree_path=tmp_path,
            test_command="echo broken && exit 1",
            timeout_seconds=10.0,
        )
        assert result.outcome == "failed"
        assert result.exit_code == 1
        assert "broken" in result.stdout
        assert result.error is None

    def test_failing_command_captures_stderr(self, tmp_path):
        """Stderr must reach the result so persistence can write
        ``test-run.stderr`` with the actual test runner's failure
        diagnostics."""
        result = run_tests(
            worktree_path=tmp_path,
            test_command="echo OOPS 1>&2 && exit 2",
            timeout_seconds=10.0,
        )
        assert result.outcome == "failed"
        assert result.exit_code == 2
        assert "OOPS" in result.stderr

    def test_pipe_chain_returns_pipeline_exit(self, tmp_path):
        """``sh -c`` interprets pipes — the operator's pipeline
        ("npm test && playwright test") works. The exit code of the
        last command in the chain is the chain's exit code."""
        result = run_tests(
            worktree_path=tmp_path,
            test_command="echo first && echo second && exit 0",
            timeout_seconds=10.0,
        )
        assert result.outcome == "passed"
        assert "first" in result.stdout
        assert "second" in result.stdout

    def test_runs_in_provided_worktree(self, tmp_path):
        """``cwd=worktree_path`` — the operator's test command sees
        the worktree's contents, not the orchestrator's cwd."""
        (tmp_path / "marker.txt").write_text("here\n")
        result = run_tests(
            worktree_path=tmp_path,
            test_command="cat marker.txt",
            timeout_seconds=10.0,
        )
        assert result.outcome == "passed"
        assert "here" in result.stdout
