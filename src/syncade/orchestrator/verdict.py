"""Mechanical per-round verdict and phase-failure classification."""

from __future__ import annotations

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.registry import UnknownProviderError
from syncade.dispatcher import DispatchResult
from syncade.exit_codes import (
    CLARIFICATION_NEEDED,
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
from syncade.synthesis import SynthesizerOutput, SynthesizerOutputError, has_active_blocker
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
        if round_result.fail_closed_headers is not None:
            return "diff_malformed"
        if round_result.oversize_diff_bytes is not None:
            return "diff_too_large"
        if round_result.oversize_prompt_chars is not None:
            return "prompt_too_large"
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

    if _multi_reviewer_blockers_all_deactivated(dispatch_result):
        return CLARIFICATION_NEEDED

    return SUCCESS


def _multi_reviewer_blockers_all_deactivated(dispatch_result: DispatchResult) -> bool:
    """True when two or more DISTINCT reviewers each raised at least one
    blocker-severity finding and none survived as an active blocker.

    PR-h-01 increment D — the paraphrase guard, and the reason it is shaped
    this way. The unanimous-blocker rule and the exact-duplicate split guard
    both need to know that two reviewers named the SAME concern. Two reviewers
    describing one bug in different words is the normal case — consolidating
    that is the synthesizer's whole job — so the synthesizer can emit them as
    two single-reviewer findings, dismiss each on its own merits, and neither
    guard fires. ``has_active_blocker`` then sees nothing and the round is a
    clean SHIP.

    Semantic identity has no cheap correct implementation, and a similarity
    heuristic manufactures FALSE locks — the worst possible failure for a tool
    whose value is that its refusals mean something. So this does not attempt
    to decide whether two findings are the same concern. It asks a question
    with a mechanical answer: did multiple independent reviewers each say
    "blocker", and did every one of those get deactivated?

    That shape is not necessarily wrong — but it is not a verdict a machine
    should render silently, so it escalates to exit 10 (decision needed) rather
    than either shipping or blocking. It cannot false-lock: the worst case is a
    human being asked to look. Callers reach this only when
    ``has_active_blocker`` is already False, so "none survived" needs no
    separate check.

    Deliberately silent when: only one reviewer raised a blocker (the
    synthesizer's dismissal authority over a single-source blocker is
    intentional), no reviewer raised one, or any blocker is still active (that
    round is already exit 30).
    """
    reviewers_with_blockers = {
        r.reviewer_name
        for r in dispatch_result.successes
        if r.output is not None and any(f.severity == "blocker" for f in r.output.findings)
    }
    return len(reviewers_with_blockers) >= 2


def deactivated_blocker_details(
    dispatch_result: DispatchResult,
    synth_output: SynthesizerOutput,
) -> list[tuple[str, str, str]]:
    """``(reviewer_name, verbatim_finding_text, disposition)`` for every source
    blocker, for the exit-10 operator document.

    The text is the REVIEWER's own, read from the parsed reviewer output rather
    than from synthesizer-supplied provenance — the operator is being asked to
    judge the synthesizer, so quoting the synthesizer back at them would beg the
    question. (Increment C makes the two provably equal anyway; reading the
    source directly means this stays true if that check is ever relaxed.)

    ``disposition`` reports what the synthesizer did with it, resolved by
    matching provenance on ``(reviewer_name, original_index)``. A source blocker
    with no consolidated finding at all is reported as dropped, which is its own
    kind of answer.
    """
    details: list[tuple[str, str, str]] = []
    for r in dispatch_result.successes:
        if r.output is None:
            continue
        for idx, source in enumerate(r.output.findings):
            if source.severity != "blocker":
                continue
            details.append((r.reviewer_name, source.finding, _disposition(synth_output, r, idx)))
    return details


def _disposition(synth_output: SynthesizerOutput, reviewer, idx: int) -> str:
    for consolidated in synth_output.consolidated_findings:
        if not any(
            p.reviewer_name == reviewer.reviewer_name and p.original_index == idx
            for p in consolidated.provenance
        ):
            continue
        if consolidated.dismissed:
            rationale = (consolidated.dismissal_rationale or "").strip()
            return f"dismissed — {rationale}" if rationale else "dismissed"
        if consolidated.severity != "blocker":
            rationale = (consolidated.severity_change_rationale or "").strip()
            downgrade = f"downgraded to {consolidated.severity}"
            return f"{downgrade} — {rationale}" if rationale else downgrade
        return f"kept at {consolidated.severity}"
    return "dropped — no consolidated finding carries provenance for it"
