"""Unit tests for mechanical checks carried by :mod:`syncade.test_runner` — PR-21.

``run_tests`` is the test leg generalized: the check path calls it directly
with ``name`` and ``severity`` metadata. These tests cover that metadata,
the consistency contract, and the inherited shell / timeout / exit-127 traps.
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError

import pytest

from syncade.process import SubprocessNotFoundError, SubprocessTimeoutError
from syncade.test_runner import TestRunResult, run_tests
from tests.test_worktree_env import make_worktree_src


def _run_check(
    tmp_path,
    *,
    name: str = "lint",
    command: str = "exit 0",
    severity: str = "advisory",
    timeout_seconds: float = 10.0,
) -> TestRunResult:
    return run_tests(
        worktree_path=tmp_path,
        test_command=command,
        timeout_seconds=timeout_seconds,
        name=name,
        severity=severity,
    )


class TestCheckMetadataDataclass:
    """The outcome ↔ exit_code contract, plus check metadata, so a
    downstream consumer (manifest / findings.md / verdict) never second-guesses
    whether ``exit_code == 0`` and ``outcome == "failed"`` could co-occur."""

    def test_passed_ok(self):
        r = TestRunResult(
            name="x",
            severity="advisory",
            exit_code=0,
            outcome="passed",
            duration_seconds=0.1,
            stdout="ok",
            stderr="",
        )
        assert r.outcome == "passed"
        assert r.name == "x"
        assert r.severity == "advisory"
        assert r.error is None

    def test_failed_ok(self):
        r = TestRunResult(
            name="x",
            severity="blocking",
            exit_code=1,
            outcome="failed",
            duration_seconds=0.1,
            stdout="",
            stderr="FAIL",
        )
        assert r.outcome == "failed"
        assert r.exit_code == 1

    def test_subprocess_error_ok(self):
        r = TestRunResult(
            name="x",
            severity="advisory",
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=0.1,
            stdout="",
            stderr="",
            error=SubprocessNotFoundError("missing"),
        )
        assert r.outcome == "subprocess_error"
        assert isinstance(r.error, SubprocessNotFoundError)

    def test_passed_with_nonzero_rejected(self):
        with pytest.raises(ValueError, match="outcome='passed' requires exit_code=0"):
            TestRunResult(
                name="x",
                severity="advisory",
                exit_code=1,
                outcome="passed",
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

    def test_failed_with_zero_rejected(self):
        with pytest.raises(ValueError, match="outcome='failed' requires exit_code > 0"):
            TestRunResult(
                name="x",
                severity="advisory",
                exit_code=0,
                outcome="failed",
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

    def test_subprocess_error_without_exception_rejected(self):
        with pytest.raises(ValueError, match="subprocess_error.*non-None error"):
            TestRunResult(
                name="x",
                severity="advisory",
                exit_code=-1,
                outcome="subprocess_error",
                duration_seconds=0.1,
                stdout="",
                stderr="",
                error=None,
            )

    def test_unknown_outcome_rejected(self):
        with pytest.raises(ValueError, match="not one of"):
            TestRunResult(
                name="x",
                severity="advisory",
                exit_code=0,
                outcome="bogus",
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )


class TestRunTestsCheckPassFail:
    """``passed`` (exit 0) and ``failed`` (exit > 0) against real ``sh -c``."""

    def test_passing_command_returns_passed(self, tmp_path):
        result = _run_check(tmp_path, command="echo ok && exit 0")
        assert result.outcome == "passed"
        assert result.exit_code == 0
        assert "ok" in result.stdout
        assert result.command == "echo ok && exit 0"

    def test_subprocess_imports_worktree_src_not_main(self, tmp_path):
        # PR-23: the checks leg (delegating to run_tests) must evaluate the
        # WORKTREE's src, not MAIN's editable-install .pth.
        worktree = make_worktree_src(tmp_path / "wt")
        worktree_pkg_dir = str((worktree / "src" / "syncade").resolve())
        cmd = f'{sys.executable} -c "import syncade, sys; sys.stdout.write(syncade.__file__)"'
        result = _run_check(worktree, command=cmd, timeout_seconds=60.0)
        assert result.outcome == "passed", result.stderr
        assert worktree_pkg_dir in result.stdout

    def test_failing_command_returns_failed(self, tmp_path):
        result = _run_check(tmp_path, command="echo broken && exit 1", severity="blocking")
        assert result.outcome == "failed"
        assert result.exit_code == 1

    def test_name_and_severity_carried_through(self, tmp_path):
        """The check path carries
        the check's identity + severity so the surfacing layer can tag
        advisory vs blocking."""
        result = _run_check(
            tmp_path,
            name="file-length",
            command="exit 0",
            severity="advisory",
        )
        assert result.name == "file-length"
        assert result.severity == "advisory"


class TestRunTestsCheckSubprocessError:
    """The inherited traps flow through delegation to run_tests."""

    def test_missing_binary_returns_subprocess_error(self, tmp_path):
        """exit 127 from ``sh -c`` reclassifies to subprocess_error — the
        trap is inherited from run_tests via delegation."""
        result = _run_check(tmp_path, command="thisbinaryreallydoesnotexistanywhere")
        assert result.outcome == "subprocess_error"
        assert result.exit_code == -1
        assert isinstance(result.error, SubprocessNotFoundError)

    def test_timeout_preserves_partial_output(self, tmp_path):
        """A command that emits then hangs past the timeout → SIGKILL,
        subprocess_error, partial stdout preserved — inherited from
        run_tests / run_subprocess."""
        result = _run_check(
            tmp_path,
            command="echo partial-before-kill && sleep 30",
            timeout_seconds=0.5,
        )
        assert result.outcome == "subprocess_error"
        assert result.exit_code == -1
        assert isinstance(result.error, SubprocessTimeoutError)
        assert "partial-before-kill" in result.stdout


class TestCheckMetadataResultIsFrozen:
    def test_frozen_dataclass(self, tmp_path):
        result = _run_check(tmp_path, command="exit 0")
        with pytest.raises(FrozenInstanceError):
            result.outcome = "failed"  # type: ignore[misc]


class TestPersistCheckResult:
    """PR-21 T3: ``persist_check_result`` writes the per-check raw artifacts,
    mirroring the test-leg's ``test-run.{stdout,stderr,exit-code.txt}``."""

    def test_writes_three_files_named_from_the_check(self, tmp_path):
        from syncade.persistence import persist_check_result

        cr = TestRunResult(
            name="file-length",
            severity="advisory",
            exit_code=1,
            outcome="failed",
            duration_seconds=0.5,
            stdout="src/x.py: 506 > 500\n",
            stderr="",
        )
        paths = persist_check_result(tmp_path, cr)
        assert paths.name == "file-length"
        assert paths.stdout.name == "file-length.check.stdout"
        assert paths.stdout.read_text() == "src/x.py: 506 > 500\n"
        assert paths.stderr.read_text() == ""
        assert paths.exit_code.read_text() == "1\n"

    def test_subprocess_error_writes_sentinel_exit_code(self, tmp_path):
        from syncade.persistence import persist_check_result

        cr = TestRunResult(
            name="lint",
            severity="blocking",
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=0.0,
            stdout="",
            stderr="",
            error=SubprocessNotFoundError("ruff"),
        )
        paths = persist_check_result(tmp_path, cr)
        assert paths.exit_code.read_text() == "-1\n"
