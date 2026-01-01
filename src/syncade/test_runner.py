# SIZE_OK: test rerun and mechanical checks share one artifact contract.
# Retained to keep the result shape used by persistence centralized.
# Future split: move classification/artifact writes to test_runner_artifacts.py.
"""Test re-run leg.

The third convergence leg. After every reviewer succeeds AND the cold
cold synthesizer produces a clean ``consolidated_findings``, the
orchestrator runs ONE additional subprocess: the operator-configured
``[loop] test_command`` from ``.syncade/config.toml``. The subprocess
runs in a fresh worktree provisioned the same way reviewer worktrees
are (``WorktreeManager``, CLAUDE.md / AGENTS.md stripped, fresh
checkout from the snapshot ``commit_sha``) so what runs is what an
external CI would see — not the producer's working tree, not the
reviewers' worktrees.

This module is intentionally narrow. There is no prompt, no LLM, no
JSONL parsing — just shell subprocess execution + result capture.
:class:`TestRunResult` mirrors the
:class:`~syncade.synthesizer.SynthesizerResult` and
:class:`~syncade.dispatcher.ReviewerRunResult` shapes so persistence
can treat reviewer outcomes, synthesizer outcomes, and test outcomes
with the same vocabulary.

**Architectural invariants (vs. relies on):**

- *Opt-in.* The orchestrator calls :func:`run_tests` only when
  ``config.loop.test_command is not None``. The unconfigured-test-leg
  path skips this module entirely; exit 0 then reflects synth-clean
  only.
- *Shell string execution is intentional.* The operator's ``test_command`` will
  plausibly want pipes, env vars, multi-command sequences ("``npm
  test && playwright test``"). The string comes from the operator's
  own ``.syncade/config.toml``, which lives in their repo and is
  controlled by them — same threat model as a ``Makefile`` or
  ``package.json`` ``"scripts"`` entry. We are not interpolating
  untrusted input into the shell string. The command runs under bash with
  ``pipefail`` enabled so failed pipeline components cannot be masked by a
  successful tail command.
- *Test failure is NOT a ConsolidatedFinding.* A non-zero test
  exit doesn't manufacture a synthetic
  :class:`~syncade.synthesis.ConsolidatedFinding` with empty
  provenance — that would violate the cannot-invent-findings
  invariant (every ``ConsolidatedFinding`` requires
  ``provenance: min_length=1``). The test result lives in its own
  section everywhere it surfaces (manifest, summary.md, findings.md).
- *Mechanical verdict stays mechanical.* The orchestrator OR's
  :func:`syncade.synthesis.has_active_blocker` with
  ``test_run.outcome == "failed"`` in
  :func:`syncade.orchestrator._compute_exit_code`. No LLM judgment
  on the test result.
- *Partial output preservation.* On timeout the SIGKILL kills the
  whole process group (inherited from
  :func:`syncade.process.run_subprocess`'s discipline) and any
  output captured before the kill is preserved on the
  :class:`TestRunResult`. Same pattern as the reviewer dispatcher's
  timeout handling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessTimeoutError,
    run_subprocess,
)
from syncade.test_runner_classify import (
    _extract_missing_binary,
)
from syncade.worktree_env import worktree_scoped_env

TestOutcome = Literal["passed", "failed", "subprocess_error"]
"""The three terminal states of a test run.

- ``"passed"`` — the test command exited 0. Tests ran and reported
  no failures (by the command's own definition of "no failures";
  the operator's free-form command decides what counts).
- ``"failed"`` — the test command exited non-zero (> 0). Tests ran
  and reported failures; the project's own test runner is the
  authority. The orchestrator folds this into the mechanical
  verdict as a blocker → exit 30.
- ``"subprocess_error"`` — the subprocess itself failed: binary
  missing, timeout, OS-level launch failure. Distinct from
  ``"failed"`` because the operator's fix path differs (fix the
  environment vs. fix the code).
"""

CheckSeverity = Literal["blocking", "advisory"]


@dataclass(frozen=True)  # SLOTS_OK: result shape is persisted and kept stable.
class TestRunResult:
    __test__: ClassVar[bool] = False

    """Outcome of one test command execution.

    Mirrors the :class:`~syncade.synthesizer.SynthesizerResult` shape
    so persistence can treat reviewer outcomes, synthesizer outcomes,
    and test outcomes with the same vocabulary.

    Attributes:
        exit_code: The test command's exit code. ``0`` means tests
            passed; ``> 0`` means tests failed. Sentinel ``-1`` when
            the subprocess was killed (timeout) or otherwise didn't
            produce a real exit code (binary missing, OS launch
            failure).
        outcome: One of ``"passed"`` | ``"failed"`` |
            ``"subprocess_error"``. ``"passed"`` iff
            ``exit_code == 0``. ``"failed"`` iff ``exit_code > 0``
            (tests ran and reported failures). ``"subprocess_error"``
            iff the subprocess itself failed (binary not found,
            timeout, launch error) — ``exit_code`` is ``-1`` in that
            case.
        duration_seconds: Wall-clock duration, measured via
            :func:`time.monotonic`.
        stdout: Captured stdout text (whatever made it before kill on
            timeout — see :func:`syncade.process.run_subprocess`'s
            partial-output preservation).
        stderr: Captured stderr text.
        error: The exception that fired on ``subprocess_error``.
            ``None`` on ``passed`` / ``failed``. Preserved so
            persistence can name the exception class in
            ``manifest.json``'s ``error_type`` field — same pattern
            as :class:`~syncade.synthesizer.SynthesizerResult`'s
            ``error`` attribute.
        command: The operator-configured ``test_command`` string,
            preserved verbatim so persistence can echo it back in
            manifest.json and summary.md without re-fetching from
            config. Empty string is allowed only on construction
            from synthetic test data; production code passes the
            real config value.
        name: Optional mechanical-check name. ``None`` for the single
            test leg; populated for ``[[checks]]`` so the check
            artifacts keep their existing names.
        severity: Optional mechanical-check severity. ``None`` for
            the single test leg; ``"blocking"`` or ``"advisory"`` for
            ``[[checks]]``.
    """

    exit_code: int
    outcome: TestOutcome
    duration_seconds: float
    stdout: str
    stderr: str
    error: Exception | None = None
    command: str = field(default="")
    name: str | None = None
    severity: CheckSeverity | None = None

    def __post_init__(self) -> None:
        """Enforce outcome ↔ exit_code consistency before the result
        reaches persistence.

        The contract is intentionally tight — a downstream consumer
        (manifest renderer, summary.md renderer, ``_compute_exit_code``)
        keys on ``outcome`` and never needs to second-guess whether
        ``exit_code == 0`` and ``outcome == "failed"`` could co-occur.

        Runtime validation rules:
        - Reject unknown ``outcome`` values up-front (the Literal
          type hint is advisory; runtime construction with
          ``outcome=None`` or ``outcome="bogus"`` would silently
          slip past all the rules below since none of them match).
        - ``subprocess_error`` requires BOTH non-None error AND
          ``exit_code == -1`` (the sentinel). Without this, a
          caller could construct
          ``TestRunResult(outcome="subprocess_error", exit_code=42,
          error=Exception())`` and persistence would echo 42 into
          manifest's ``exit_code`` field while the orchestrator's
          mechanical verdict treats the result as a subprocess
          error — two consumers disagreeing on what the run
          actually did.
        """
        # Reject unknown outcomes BEFORE the per-outcome rules
        # below — otherwise an out-of-vocabulary outcome would
        # silently slip past every rule (none of them would match).
        if self.outcome not in ("passed", "failed", "subprocess_error"):
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                f"TestRunResult: outcome={self.outcome!r} is not one of "
                f"the three valid TestOutcome values "
                f"('passed', 'failed', 'subprocess_error')"
            )
        if self.outcome == "passed" and self.exit_code != 0:
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                f"TestRunResult: outcome='passed' requires exit_code=0, "
                f"got exit_code={self.exit_code}"
            )
        if self.outcome == "failed" and self.exit_code <= 0:
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                f"TestRunResult: outcome='failed' requires exit_code > 0, "
                f"got exit_code={self.exit_code}"
            )
        if self.outcome == "subprocess_error":
            if self.error is None:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    "TestRunResult: outcome='subprocess_error' requires non-None error"
                )
            if self.exit_code != -1:
                raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                    f"TestRunResult: outcome='subprocess_error' requires "
                    f"exit_code=-1 (the no-real-exit-code sentinel); got "
                    f"exit_code={self.exit_code}"
                )
        if self.outcome != "subprocess_error" and self.error is not None:
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                f"TestRunResult: error must be None when outcome != "
                f"'subprocess_error' (outcome={self.outcome!r}, "
                f"error={self.error!r})"
            )
        if (self.name is None) != (self.severity is None):
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                "TestRunResult: check metadata requires both name and severity"
            )
        if self.severity is not None and self.severity not in ("blocking", "advisory"):
            raise ValueError(  # GENERIC_ERR_OK: dataclass invariant preserves existing API.
                "TestRunResult: severity must be 'blocking' or 'advisory'"
            )


def is_blocking_check_subprocess_error(severity: str, outcome: str) -> bool:
    return severity == "blocking" and outcome == "subprocess_error"


def run_tests(
    *,
    worktree_path: Path,
    test_command: str,
    timeout_seconds: float,
    name: str | None = None,
    severity: CheckSeverity | None = None,
) -> TestRunResult:
    """Execute ``test_command`` in ``worktree_path``, capture
    stdout/stderr/rc, return a typed :class:`TestRunResult`.

    The command is run via ``bash -o pipefail -c <test_command>`` so operator
    pipelines fail when any component fails. The same SIGKILL-on-timeout
    discipline as reviewer subprocesses applies — inherited from
    :func:`syncade.process.run_subprocess`, which kills the whole process group
    on timeout so descendants don't orphan.

    **Note on shell=True security:** the command comes from the
    operator's own ``.syncade/config.toml``, which is in their repo
    and controlled by them. We are not interpolating untrusted input
    into the shell string. The threat model is "operator
    misconfigures their own test_command and shoots their own foot"
    — not "attacker injects via test_command." Same threat model as
    a Makefile or ``package.json`` ``"scripts"`` entry.

    Args:
        worktree_path: Path of the fresh worktree to run the test
            command in. The orchestrator provisions this via
            :class:`~syncade.worktree.WorktreeManager` with the
            same ``strip_files`` treatment as reviewer worktrees,
            so what runs sees what an external CI would see.
        test_command: The operator-configured ``[loop] test_command``
            string. Passed verbatim to ``bash -o pipefail -c``. Must
            be a non-empty string; whitespace-only is rejected at
            config-load via
            :meth:`syncade.config.LoopConfig._test_command_not_whitespace`,
            so callers can assume it's substantive.
        timeout_seconds: Per-test-run wall-clock cap. The
            orchestrator resolves this as
            ``config.loop.test_timeout_seconds or
            config.loop.timeout_seconds`` and passes the resolved
            value here.
        name: Optional mechanical-check name. The ordinary test leg
            leaves this unset.
        severity: Optional mechanical-check severity. Must be paired
            with ``name``.

    Returns:
        :class:`TestRunResult` regardless of outcome. The caller
        (orchestrator) inspects ``outcome`` to fold the result into
        the mechanical verdict:

        - ``"passed"`` → no contribution to exit code; still 0 if
          synth was also clean.
        - ``"failed"`` → exit 30 (treated as a blocker; the
          orchestrator OR's this with
          :func:`syncade.synthesis.has_active_blocker`).
        - ``"subprocess_error"`` → exit 40 (same bucket as a
          reviewer or synthesizer subprocess failure).
    """
    argv = ["bash", "-o", "pipefail", "-c", test_command]
    run_start = time.monotonic()

    try:
        result = run_subprocess(
            argv,
            cwd=worktree_path,
            # scope the child's Python env to the worktree so the
            # authoritative test leg imports the worktree's syncade / runs the
            # snapshot's pytest, NOT MAIN's via the editable-install .pth.
            # The mechanical-check leg uses this same path with check metadata.
            env=worktree_scoped_env(worktree_path),
            timeout=timeout_seconds,
        )
    except SubprocessTimeoutError as exc:
        # Same partial-output preservation pattern as the reviewer
        # dispatcher and the synthesizer module. The partial
        # stdout/stderr captured by run_subprocess before the
        # SIGKILL is on the exception; surface it on the
        # TestRunResult so persistence still writes the
        # test-run.stdout / test-run.stderr files. Sentinel
        # exit_code=-1 marks "killed before exit".
        return TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=time.monotonic() - run_start,
            stdout=exc.stdout,
            stderr=exc.stderr,
            error=exc,
            command=test_command,
            name=name,
            severity=severity,
        )
    except SubprocessNotFoundError as exc:
        # ``bash`` itself missing — uncommon on supported operator systems
        # but the orchestrator's error surface still needs a defined
        # mapping. No partial output to preserve; the subprocess
        # never started.
        return TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=time.monotonic() - run_start,
            stdout="",
            stderr="",
            error=exc,
            command=test_command,
            name=name,
            severity=severity,
        )
    except SubprocessError as exc:
        # Other launch failures (bad cwd, OS error). Same shape as
        # SubprocessNotFoundError — no subprocess output to
        # preserve.
        return TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=time.monotonic() - run_start,
            stdout="",
            stderr="",
            error=exc,
            command=test_command,
            name=name,
            severity=severity,
        )

    # The subprocess ran to completion. Classify by exit code.
    #
    # The verdict architecture rests on exit codes carrying
    # unambiguous operator intent:
    # - Exit 30 (``outcome="failed"``) means "your code has a real
    #   defect, go fix the code." The multi-round loop will re-invoke the
    #   producer to fix code on exit 30.
    # - Exit 40 (``outcome="subprocess_error"``) means "the harness
    #   couldn't run, go fix your environment." The loop will not continue
    #   on this — the operator's PATH / config / binary install is
    #   the fix.
    #
    # Three specific return-code patterns get reclassified from
    # "non-zero = failed" to "subprocess_error" because they
    # unambiguously signal harness/environment problems rather
    # than test failures:
    #
    # - **127** (POSIX "command not found"): bash pipefail execution
    #   returned this because the operator's command references a binary
    #   that isn't on PATH. This is a subprocess_error, and the classifier
    #   parses the actual missing binary out of stderr for the error message.
    # - **126** (POSIX "command found but not executable"): the
    #   shell located the binary but the OS refused to exec it
    #   (permission denied, wrong arch, broken shebang). Same
    #   class of environmental problem as 127.
    # - **Negative return code** (signal-killed): the subprocess
    #   was terminated by a signal before it could exit normally.
    #   This isn't the test reporting failure — the test process
    #   never got to decide. Examples: ``kill -TERM $$`` from
    #   inside the script, OOM-killer, parent-shell death.
    #
    # All three preserve stdout/stderr so the operator inspecting
    # test-run.stderr sees what the shell / OS said about the
    # failure. exit_code on the TestRunResult is -1 (the
    # subprocess_error sentinel) — the real return code lives in
    # the synthesized exception's message and in the captured
    # streams.
    #
    # Risk of false positive: a hypothetical test framework that
    # exits 126/127 on genuine test failure would be
    # reclassified. No major framework does (pytest 0-5, npm 1,
    # jest 1). Documented escape hatch: wrap the command to
    # remap, e.g. ``bash -o pipefail -c 'runner; rc=$?; case $rc in 126|127)
    # exit 1;; *) exit $rc;; esac'``.
    rc = result.returncode
    if rc == 127:
        # Prefer to name the actual missing binary (extracted from stderr) over
        # the full command string. Fallback to the full command if parse fails.
        missing = _extract_missing_binary(result.stderr) or test_command
        synthesized_error: Exception = SubprocessNotFoundError(missing)
        return TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            error=synthesized_error,
            command=test_command,
            name=name,
            severity=severity,
        )
    if rc == 126:
        synthesized_error = SubprocessError(
            f"test_command found but not executable (bash/shell exit 126): "
            f"{test_command!r}. The OS refused to exec the named "
            f"binary — common causes: permission denied (chmod +x), "
            f"wrong architecture, or a broken shebang. Captured "
            f"shell stderr: {result.stderr.strip()[:200]!r}"
        )
        return TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            error=synthesized_error,
            command=test_command,
            name=name,
            severity=severity,
        )
    if rc < 0:
        # subprocess.Popen returns negative rc when the child was
        # terminated by a signal. ``-N`` means signal N (so -15 =
        # SIGTERM, -9 = SIGKILL — though SIGKILL from the orch's
        # own timeout takes the SubprocessTimeoutError path
        # above, not this one). Signal-killed isn't a test
        # verdict; it's the test process never getting to
        # exit.
        synthesized_error = SubprocessError(
            f"test_command terminated by signal {-rc} before it "
            f"could exit normally (raw subprocess returncode={rc}). "
            f"This is a harness/environment problem, not a test "
            f"verdict. Captured shell stderr: "
            f"{result.stderr.strip()[:200]!r}"
        )
        return TestRunResult(
            exit_code=-1,
            outcome="subprocess_error",
            duration_seconds=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            error=synthesized_error,
            command=test_command,
            name=name,
            severity=severity,
        )

    if rc == 0:
        outcome: TestOutcome = "passed"
    else:
        outcome = "failed"

    return TestRunResult(
        exit_code=rc,
        outcome=outcome,
        duration_seconds=result.duration_seconds,
        stdout=result.stdout,
        stderr=result.stderr,
        error=None,
        command=test_command,
        name=name,
        severity=severity,
    )
