"""Unit tests for :mod:`syncade.test_runner` — PR-7.5 (continued).

Subprocess-error paths, the frozen-result guard, the pipeline
false-pass reclassification, and the missing-binary shell-variant
extraction. Companion to ``test_test_runner.py``.
"""

from __future__ import annotations

import sys

import pytest

from syncade.process import (
    SubprocessNotFoundError,
    SubprocessTimeoutError,
)
from syncade.test_runner import run_tests

# ---------------------------------------------------------------------------
# run_tests subprocess-error paths — timeout, command not found
# ---------------------------------------------------------------------------


class TestRunTestsSubprocessError:
    """The three subprocess-failure cases: binary missing, timeout,
    other launch errors. All map to ``outcome="subprocess_error"``
    with the exception preserved."""

    def test_command_not_found_returns_subprocess_error(self, tmp_path):
        """``bash -o pipefail -c`` returns exit 127 when the binary
        the operator named isn't on PATH. PR-7.5 fix #2 (post-QA): the runner
        reclassifies this as ``outcome="subprocess_error"`` with a
        synthesized :class:`SubprocessNotFoundError` per the Task 2
        acceptance criterion.

        Why this matters: the orchestrator maps ``failed`` to
        exit 30 (real defect, PR-8 loop will re-invoke producer to
        fix code) and ``subprocess_error`` to exit 40 (environment
        problem, operator fix path). A misconfigured ``test_command``
        is environmental; routing it to exit 30 would send a future
        PR-8 producer to debug a phantom test failure.

        T3.12 refinement: the synthesized
        :class:`SubprocessNotFoundError.binary` carries the actual
        missing binary name (parsed from shell stderr), not the
        whole pipeline string — so manifest.json's ``error_type``
        field gives the operator an actionable identity.
        """
        result = run_tests(
            worktree_path=tmp_path,
            test_command="thisbinaryreallydoesnotexistanywhere",
            timeout_seconds=10.0,
        )
        assert result.outcome == "subprocess_error", (
            f"exit 127 from bash pipefail execution must reclassify to "
            f"subprocess_error per Task 2 acceptance; got {result.outcome!r}"
        )
        assert result.exit_code == -1
        assert isinstance(result.error, SubprocessNotFoundError)
        # T3.12: .binary is the actual missing binary name parsed
        # from the shell's "command not found" line, not the full
        # operator command. For a single-word command they're
        # identical; the difference shows in pipeline commands.
        assert result.error.binary == "thisbinaryreallydoesnotexistanywhere"
        # Shell's "command not found" message preserved in stderr
        # verbatim — the operator inspecting test-run.stderr sees
        # what the shell actually said.
        assert "command not found" in result.stderr.lower()

    def test_command_not_found_in_pipeline_extracts_actual_missing_binary(self, tmp_path):
        """T3.12: when the operator's test_command is a pipeline like
        ``echo x | thisbinarymissing | wc -l``, the synthesized
        SubprocessNotFoundError's .binary should be
        ``thisbinarymissing`` (parsed from shell stderr), not the
        full pipeline string."""
        result = run_tests(
            worktree_path=tmp_path,
            test_command="echo hi | thisbinarymissing | wc -l",
            timeout_seconds=10.0,
        )
        assert isinstance(result.error, SubprocessNotFoundError)
        assert result.error.binary == "thisbinarymissing", (
            f"expected the missing-binary name extracted from stderr; "
            f"got .binary={result.error.binary!r}"
        )

    def test_command_not_executable_returns_subprocess_error(self, tmp_path):
        """T1.1: ``bash -o pipefail -c`` returns exit 126 when the
        binary exists but the OS refuses to exec it (permission denied, wrong
        arch, broken shebang). This is environmental, not a test
        verdict — must classify as subprocess_error → exit 40, not
        as failed → exit 30."""
        # Plant a script that exists but isn't chmod +x. Bash invoking
        # it returns 126.
        script = tmp_path / "noexec.sh"
        script.write_text("#!/bin/sh\necho should not run\n")
        # Explicitly remove +x just in case the umask granted it.
        script.chmod(0o644)
        result = run_tests(
            worktree_path=tmp_path,
            test_command=f"{script}",
            timeout_seconds=10.0,
        )
        assert result.outcome == "subprocess_error", (
            f"exit 126 (permission denied) must reclassify to "
            f"subprocess_error; got {result.outcome!r} exit_code={result.exit_code}"
        )
        assert result.exit_code == -1
        # SubprocessError (base) — distinct from
        # SubprocessNotFoundError (127). Different error class
        # signals different operator-fix paths in manifest.error_type.
        from syncade.process import SubprocessError

        assert isinstance(result.error, SubprocessError)
        # NOT a SubprocessNotFoundError — 126 is a different category.
        assert not isinstance(result.error, SubprocessNotFoundError)

    def test_signal_killed_command_returns_subprocess_error(self, tmp_path):
        """T1.1: ``bash -o pipefail -c 'kill -TERM $$'`` causes the
        shell itself to die from SIGTERM. ``subprocess.Popen`` reports the
        return code as -15 (negative signal number). The test
        process never got to exit normally — must classify as
        subprocess_error, NOT failed.

        Without this reclassification, the original implementation
        would have raised ``ValueError`` on construction (exit -15
        is not > 0, so outcome="failed" would have failed
        __post_init__). The fix prevents both the silent
        misclassification AND the construction-time crash.
        """
        result = run_tests(
            worktree_path=tmp_path,
            test_command="kill -TERM $$",
            timeout_seconds=10.0,
        )
        assert result.outcome == "subprocess_error", (
            f"negative rc (signal-killed) must reclassify to "
            f"subprocess_error; got {result.outcome!r} exit_code={result.exit_code}"
        )
        assert result.exit_code == -1
        from syncade.process import SubprocessError

        assert isinstance(result.error, SubprocessError)
        # The synthesized message should mention the signal so the
        # operator can read manifest.error_type and understand what
        # killed the test process.
        assert "signal" in str(result.error).lower()

    def test_timeout_returns_subprocess_error_with_partial_output(self, tmp_path):
        """A long-sleeping command with a short timeout should
        SIGKILL on the partial-output preservation pattern: the
        captured stdout/stderr from before the kill is on the
        result; the exception is :class:`SubprocessTimeoutError`."""
        # Write some output, then sleep long enough to exceed the
        # half-second timeout. The output before the sleep must
        # reach the result.
        result = run_tests(
            worktree_path=tmp_path,
            test_command="echo partial-before-kill && sleep 30",
            timeout_seconds=0.5,
        )
        assert result.outcome == "subprocess_error"
        assert result.exit_code == -1
        assert isinstance(result.error, SubprocessTimeoutError)
        assert "partial-before-kill" in result.stdout

    def test_invalid_cwd_returns_subprocess_error(self, tmp_path):
        """A non-existent worktree path surfaces as a
        :class:`syncade.process.SubprocessError`. The orchestrator
        provisions the worktree before calling, so this is
        defensive — but the contract must be defined."""
        missing = tmp_path / "does-not-exist"
        result = run_tests(
            worktree_path=missing,
            test_command="echo unreachable",
            timeout_seconds=10.0,
        )
        assert result.outcome == "subprocess_error"
        assert result.exit_code == -1
        assert result.error is not None
        # SubprocessError class hierarchy — `cwd does not exist` is a
        # SubprocessError (the base class).
        from syncade.process import SubprocessError

        assert isinstance(result.error, SubprocessError)


# ---------------------------------------------------------------------------
# Defensive sanity: the result type is frozen so downstream code can't
# accidentally mutate it before persistence reads it.
# ---------------------------------------------------------------------------


class TestTestRunResultIsFrozen:
    def test_frozen_dataclass(self, tmp_path):
        from dataclasses import FrozenInstanceError

        result = run_tests(
            worktree_path=tmp_path,
            test_command="echo ok",
            timeout_seconds=10.0,
        )
        with pytest.raises(FrozenInstanceError):
            result.outcome = "failed"  # type: ignore[misc]


class TestReturnCodeClassification:
    def test_pipeline_with_missing_lhs_returns_subprocess_error(self, tmp_path):
        # Given: a pipeline whose left side cannot launch.
        # When: run_tests executes it.
        result = run_tests(
            worktree_path=tmp_path,
            test_command="thisbinarymissingxyz | wc -l",
            timeout_seconds=10.0,
        )
        # Then: the shell failure is a subprocess error, not a pass.
        assert result.outcome == "subprocess_error"
        assert result.exit_code == -1
        assert isinstance(result.error, SubprocessNotFoundError)
        assert result.error.binary == "thisbinarymissingxyz"

    def test_chain_with_missing_first_command_trusts_successful_shell_return_code(self, tmp_path):
        result = run_tests(
            worktree_path=tmp_path,
            test_command="thisbinarymissingxyz || true",
            timeout_seconds=10.0,
        )
        assert result.outcome == "passed"
        assert result.exit_code == 0
        assert result.error is None

    def test_clean_passing_command_with_no_diagnostic_is_still_passed(self, tmp_path):
        """Sanity: the rc==0 reclassification check must NOT
        false-positive on normal passing commands. Plain ``true``
        with empty stderr → outcome="passed"."""
        result = run_tests(
            worktree_path=tmp_path,
            test_command="true",
            timeout_seconds=10.0,
        )
        assert result.outcome == "passed"

    def test_successful_command_with_failure_looking_stderr_stays_passed(self, tmp_path):
        result = run_tests(
            worktree_path=tmp_path,
            test_command="printf 'FAILED: command not found in docs\\n' >&2; exit 0",
            timeout_seconds=10.0,
        )
        assert result.outcome == "passed"
        assert result.exit_code == 0
        assert result.error is None
        assert "FAILED" in result.stderr

    def test_pipeline_with_nonzero_shell_return_code_is_not_ship(self, tmp_path):
        result = run_tests(
            worktree_path=tmp_path,
            test_command="printf 'x\\n' | grep 'missing'",
            timeout_seconds=10.0,
        )
        assert result.outcome == "failed"
        assert result.exit_code == 1
        assert result.error is None

    def test_false_piped_to_true_is_failed(self, tmp_path):
        # Given: a failing command masked by a successful pipeline tail.
        # When: run_tests executes the pipeline.
        result = run_tests(
            worktree_path=tmp_path,
            test_command="false | true",
            timeout_seconds=10.0,
        )
        # Then: the failed pipeline component prevents a passed result.
        assert result.outcome == "failed"
        assert result.exit_code == 1
        assert result.error is None

    def test_python_failure_piped_to_cat_is_failed(self, tmp_path):
        # Given: Python exits non-zero before a successful cat.
        # When: run_tests executes the pipeline.
        result = run_tests(
            worktree_path=tmp_path,
            test_command=f"{sys.executable} -c 'import sys; sys.exit(7)' | cat",
            timeout_seconds=10.0,
        )
        # Then: the Python failure prevents a passed result.
        assert result.outcome == "failed"
        assert result.exit_code == 7
        assert result.error is None

    def test_python_failure_piped_to_tee_is_failed(self, tmp_path):
        # Given: Python exits non-zero before a successful tee.
        log_path = tmp_path / "syncade-pipe.log"
        # When: run_tests executes the pipeline.
        result = run_tests(
            worktree_path=tmp_path,
            test_command=(f"{sys.executable} -c 'import sys; sys.exit(7)' | tee {log_path}"),
            timeout_seconds=10.0,
        )
        # Then: the Python failure prevents a passed result.
        assert result.outcome == "failed"
        assert result.exit_code == 7
        assert result.error is None


class TestExtractMissingBinaryShellVariants:
    """R2.T2.10: _extract_missing_binary handles bash / sh / dash /
    zsh / busybox diagnostic formats. Each variant prints the
    "command not found" line in a slightly different shape."""

    def _extract(self, stderr: str) -> str | None:
        from syncade.test_runner import _extract_missing_binary

        return _extract_missing_binary(stderr)

    def test_bash_with_line_number(self):
        assert self._extract("bash: line 1: missingbinary: command not found\n") == "missingbinary"

    def test_sh_without_line_number(self):
        assert self._extract("sh: missingbinary: command not found\n") == "missingbinary"

    def test_dash_format_not_found(self):
        """dash uses ``"not found"`` (no leading "command ")."""
        assert self._extract("sh: 1: missingbinary: not found\n") == "missingbinary"

    def test_dash_full_name(self):
        assert self._extract("dash: 1: missingbinary: not found\n") == "missingbinary"

    def test_zsh_reversed_form(self):
        """zsh: ``zsh: command not found: missingbinary``."""
        assert self._extract("zsh: command not found: missingbinary\n") == "missingbinary"

    def test_busybox_sh_path_prefix(self):
        assert self._extract("/bin/sh: missingbinary: not found\n") == "missingbinary"

    def test_returns_last_match_when_multiple_diagnostics(self):
        """Pipeline ``missing1 | missing2`` produces two diagnostics.
        Return the LAST one (the most-recent failure)."""
        stderr = "sh: missing1: command not found\nsh: missing2: command not found\n"
        assert self._extract(stderr) == "missing2"

    def test_returns_none_for_unrelated_stderr(self):
        """Test runner output that happens to contain the substring
        'command not found' but no shell prefix → no match. The
        strict shell-name prefix prevents false positives."""
        assert self._extract("FAILED: the user said command not found in the docs\n") is None

    def test_returns_none_for_empty(self):
        assert self._extract("") is None
        assert self._extract("\n\n\n") is None
