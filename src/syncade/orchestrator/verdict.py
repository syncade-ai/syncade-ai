"""Mechanical per-round verdict and phase-failure classification."""

from __future__ import annotations

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.registry import UnknownProviderError
from syncade.dispatcher import DispatchResult
from syncade.exit_codes import (
    CONFIG_ERROR,
    FINDINGS_PRESENT,
    REVIEWER_FAILURE,
    REVIEWER_OUTPUT_UNPARSEABLE,
    SUCCESS,
    WORKTREE_ERROR,
)
from syncade.findings import ReviewerOutputError
from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessTimeoutError,
)
from syncade.synthesis import SynthesizerOutputError, has_active_blocker
from syncade.synthesizer import SynthesizerResult
from syncade.test_runner import TestRunResult, is_blocking_check_subprocess_error

from .results import RoundResult, TerminationReason


def _classify_phase_failure(round_result: RoundResult) -> TerminationReason:
    """Map a phase-failure round_exit_code to a termination_reason.

    Called from the loop terminator when ``round_exit_code`` is
    one of {40, 50, 60, 70}. Inspects the round's per-phase
    outcomes to distinguish which phase actually failed (e.g.
    exit 40 could be reviewer / synth / test-subprocess; exit 70
    could be reviewer-parse / synth-parse).
    """
    exit_code = round_result.round_exit_code
    if exit_code == CONFIG_ERROR:
        return "config_error"
    if exit_code == WORKTREE_ERROR:
        return "worktree_error"
    if exit_code == REVIEWER_OUTPUT_UNPARSEABLE:
        return "parse_failure"
    if exit_code == REVIEWER_FAILURE:
        # Three flavors of 40: reviewer subprocess, synth subprocess,
        # test subprocess. Distinguish by which phase has an error.
        if not round_result.dispatch_result.all_succeeded:
            return "reviewer_failure"
        if round_result.synth_result is not None and round_result.synth_result.error is not None:
            return "synth_failure"
        if (
            round_result.test_result is not None
            and round_result.test_result.outcome == "subprocess_error"
        ):
            return "test_subprocess_error"
        # A blocking mechanical check whose subprocess could not run is its
        # own phase, distinct from a reviewer subprocess failure.
        if any(
            is_blocking_check_subprocess_error(c.severity, c.outcome)
            for c in round_result.check_results
        ):
            return "check_subprocess_error"
        # Fall-through default: treat as reviewer failure.
        return "reviewer_failure"
    # Shouldn't happen — caller filters to {40,50,60,70} before
    # calling. Defensive fallback.
    return "reviewer_failure"


def _compute_exit_code(
    dispatch_result: DispatchResult,
    synth_result: SynthesizerResult | None,
    test_result: TestRunResult | None = None,
    blocking_check_results: list[TestRunResult] | None = None,
) -> int:
    """Compute the per-round mechanical exit code."""
    failures = dispatch_result.failures

    for r in failures:
        if isinstance(r.error, UnknownProviderError):
            return CONFIG_ERROR

    for r in failures:
        if isinstance(r.error, ReviewerOutputError):
            return REVIEWER_OUTPUT_UNPARSEABLE

    for r in failures:
        if isinstance(
            r.error,
            (
                ReviewerInvocationError,
                SubprocessNotFoundError,
                SubprocessTimeoutError,
                SubprocessError,
            ),
        ):
            return REVIEWER_FAILURE

    if failures:
        return REVIEWER_FAILURE

    successes = dispatch_result.successes
    if not successes:
        return CONFIG_ERROR

    if synth_result is None:
        raise RuntimeError(
            "_compute_exit_code: synth_result is None but every reviewer "
            "succeeded. The orchestrator runs the synthesizer phase "
            "whenever dispatch_result.all_succeeded is True; receiving "
            "synth_result=None on this path means a caller bypassed "
            "run_review's lifecycle."
        )

    if isinstance(synth_result.error, SynthesizerOutputError):
        return REVIEWER_OUTPUT_UNPARSEABLE
    if isinstance(
        synth_result.error,
        (
            ReviewerInvocationError,
            SubprocessNotFoundError,
            SubprocessTimeoutError,
            SubprocessError,
        ),
    ):
        return REVIEWER_FAILURE
    if synth_result.error is not None:
        return REVIEWER_FAILURE

    if synth_result.output is None:
        raise RuntimeError(
            "_compute_exit_code: synth_result has neither output nor "
            "error — SynthesizerResult contract violated"
        )
    # Fold blocking mechanical-check results in alongside the test leg. Advisory
    # results are never passed in by the caller and cannot gate the verdict.
    mechanical = ([test_result] if test_result is not None else []) + list(
        blocking_check_results or []
    )
    # A subprocess_error means the harness could not run, so it outranks every
    # determinate verdict, including an active synth blocker.
    if any(m.outcome == "subprocess_error" for m in mechanical):
        return REVIEWER_FAILURE

    if has_active_blocker(synth_result.output):
        return FINDINGS_PRESENT

    # A gate that ran and FAILED (a real defect → exit 30) is the same
    # determinate NO-SHIP signal as a synth blocker; checked after it.
    if any(m.outcome == "failed" for m in mechanical):
        return FINDINGS_PRESENT

    return SUCCESS
