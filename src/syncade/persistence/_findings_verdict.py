"""findings.md headline-verdict derivation.

:func:`_compute_findings_md_verdict` derives the
findings.md headline (SHIP / NO-SHIP / ABORT) from the synth's consolidated
findings + the test-leg outcome; the matrix mirrors
:func:`syncade.orchestrator._compute_exit_code`'s test-leg branches so the two
surfaces always agree about the overall run outcome.
"""

from __future__ import annotations

from syncade.dispatcher import DispatchResult
from syncade.synthesis import SynthesizerOutput, has_active_blocker
from syncade.test_runner import TestRunResult


def _compute_findings_md_verdict(
    synth_output: SynthesizerOutput,
    test_result: TestRunResult | None,
    test_skip_reason: str | None = None,
    blocking_check_results: list[TestRunResult] | None = None,
    dispatch_result: DispatchResult | None = None,
) -> tuple[str, str]:
    """Derive the findings.md headline verdict from the synth's findings.

    The result also depends on the test-leg
    outcome, (when the leg was skipped) the skip reason, AND the
    BLOCKING mechanical-check results.

    Returns ``(label, qualifier)`` where ``label`` is the headline
    ("SHIP", "NO-SHIP", "ABORT") and ``qualifier`` is the
    parenthetical explaining where the verdict came from.

    The matrix mirrors
    :func:`syncade.orchestrator._compute_exit_code`'s test-leg +
    blocking-check branches exactly so the two surfaces always agree
    about the overall run outcome. The test leg and the blocking
    checks share one "mechanical gate" bucket; ``subprocess_error``
    (ABORT, exit 40) outranks BOTH a synth blocker and a ``failed``
    gate (NO-SHIP, exit 30), matching ``_compute_exit_code``'s
    ordering — a gate that couldn't run leaves the round
    indeterminate regardless of what the synth found:

    +-----------------+--------------------------+-----------+
    | synth blocker?  | mechanical gate outcome  | verdict   |
    +=================+==========================+===========+
    | (any)           | any gate subprocess_err  | ABORT     |
    | yes             | no gate subprocess_err   | NO-SHIP   |
    | no              | any gate failed          | NO-SHIP   |
    | no              | all gates passed/skipped | SHIP      |
    +-----------------+--------------------------+-----------+

    A "mechanical gate" is the test leg OR a ``blocking`` check.
    ``advisory`` checks are never passed in (``blocking_check_results``
    holds only blocking ones), so they are structurally unable to move
    the verdict — exactly as they cannot reach ``_compute_exit_code``.
    With no blocking checks the bucket is just the test leg:

    +-----------------+----------------+----------------------+-----------+
    | synth blocker?  | test outcome   | test_skip_reason     | verdict   |
    +=================+================+======================+===========+
    | yes (any)       | (skipped)      | (any)                | NO-SHIP   |
    | no              | passed         | None                 | SHIP      |
    | no              | failed         | None                 | NO-SHIP   |
    | no              | subprocess_err | None                 | ABORT     |
    | no              | None (skipped) | test_command_unset   | SHIP      |
    | no              | None (skipped) | reviewer_failed *    | NO-SHIP   |
    | no              | None (skipped) | synth_failed *       | NO-SHIP   |
    | no              | None (skipped) | test_worktree_error  | ABORT     |
    +-----------------+----------------+----------------------+-----------+

    \\* These rows are theoretical — persist_findings_md is only
    called on the synth-success-and-all-reviewers-succeeded path,
    so test_skip_reason can practically only be
    ``test_command_unset`` or ``test_worktree_error`` here. The
    matrix rows are listed for completeness / future-proofing.

    The "ABORT" wording for subprocess_error AND
    ``test_worktree_error`` is intentional — both are
    leg-couldn't-run states operationally distinct from "ship it"
    AND from "don't ship it because of a real defect". The
    operator's fix in both cases is environmental (PATH / binary
    / config / git state), not code.
    """
    # Fold the test leg AND the blocking mechanical checks into one gate
    # bucket, mirroring _compute_exit_code. subprocess_error (ABORT, exit 40)
    # is the highest-precedence signal — a gate that couldn't run leaves the
    # round indeterminate, so the operator fixes the environment first. With
    # no blocking checks the bucket is just the test leg.
    blocking = blocking_check_results or []
    errored_checks = [c for c in blocking if c.outcome == "subprocess_error"]
    failed_checks = [c for c in blocking if c.outcome == "failed"]

    # ABORT — a mechanical gate couldn't run (its harness/binary broke). This
    # is evaluated BEFORE the synth-blocker branch: checks run on synth-blocker
    # rounds (the test leg does not), so a broken check harness can coincide
    # with an active blocker, and an indeterminate gate outranks even NO-SHIP —
    # exactly as _compute_exit_code maps a blocking subprocess_error to exit 40
    # regardless of synth state. (test_result is None on blocker rounds, so its
    # branch never fires there; the zero-checks ordering is unchanged.)
    if test_result is not None and test_result.outcome == "subprocess_error":
        return (
            "ABORT",
            "test re-run subprocess failed; verdict indeterminate until "
            "the environment problem is fixed",
        )
    if errored_checks:
        return (
            "ABORT",
            f"blocking check '{errored_checks[0].name}' subprocess failed; "
            "verdict indeterminate until the environment problem is fixed",
        )

    # Synth blocker wins over a gate that ran (failed/passed) — the test leg
    # is skipped on synth-blocker paths anyway (see _compute_exit_code), so
    # test_result is None here and only checks can be present.
    if has_active_blocker(synth_output):
        return ("NO-SHIP", "mechanical, from consolidated_findings")

    # test-worktree-error is operationally an ABORT,
    # not a SHIP. The leg couldn't be provisioned, so the test
    # signal is indeterminate. exit_code on the orchestrator side
    # is 60 (WORKTREE_ERROR), not 0 — findings.md saying SHIP
    # would directly contradict the exit code.
    if test_skip_reason == "test_worktree_error":
        return (
            "ABORT",
            "test re-run worktree could not be provisioned; verdict "
            "indeterminate until the worktree provisioning failure "
            "is fixed (exit 60)",
        )

    # NO-SHIP — a mechanical gate ran and failed.
    if test_result is not None and test_result.outcome == "failed":
        return (
            "NO-SHIP",
            f"mechanical, from test re-run exit {test_result.exit_code} (synthesizer was clean)",
        )
    if failed_checks:
        return (
            "NO-SHIP",
            f"mechanical, from blocking check '{failed_checks[0].name}' exit "
            f"{failed_checks[0].exit_code} (synthesizer was clean)",
        )

    # DECISION NEEDED — every gate is clean, but two or more distinct reviewers
    # each raised a blocker and the synthesizer deactivated all of them
    # (PR-h-01 increment D → exit 10). Printing SHIP here would contradict both
    # the exit code and decision-needed.md, which says in as many words that the
    # loop refused to report a SHIP it cannot justify. findings.md is the
    # most-read artifact, so this is the surface where that contradiction does
    # the most damage.
    #
    # This bug class has now regressed three times (T1.6 failed test leg, then
    # failed blocking check, then this) for one reason: a new exit code was
    # added without a matching branch here. If you add a fourth, add it here.
    if _multi_reviewer_blockers_all_deactivated(dispatch_result):
        return (
            "DECISION NEEDED",
            "2+ reviewers each raised a blocker and all were deactivated; "
            "see decision-needed.md (exit 10)",
        )

    # SHIP — synth clean and every mechanical gate passed or was skipped.
    if test_result is None:
        # Test leg skipped — configured off (test_command_unset) OR a
        # hypothetical reviewer/synth-failed path (which doesn't reach here in
        # production but is listed in the matrix for completeness).
        return ("SHIP", "mechanical, from consolidated_findings; test leg skipped")
    # test_result.outcome == "passed".
    return (
        "SHIP",
        "mechanical, from consolidated_findings + test re-run passed",
    )


def _multi_reviewer_blockers_all_deactivated(dispatch_result: DispatchResult | None) -> bool:
    """Mirror of :func:`syncade.orchestrator.verdict._multi_reviewer_blockers_all_deactivated`
    for the render surface.

    Deliberately NOT imported from the orchestrator: ``persistence`` must not
    depend on ``orchestrator`` (which imports persistence), and the predicate is
    one set comprehension. The behavioural tie is enforced by test, not by a
    shared import — see ``tests/persistence/test_findings_md_verdict.py``.

    Callers reach this only after every gate is clean and
    ``has_active_blocker`` is False, so "all deactivated" needs no separate
    check, exactly as on the orchestrator side.
    """
    if dispatch_result is None:
        return False
    return (
        len(
            {
                r.reviewer_name
                for r in dispatch_result.successes
                if r.output is not None and any(f.severity == "blocker" for f in r.output.findings)
            }
        )
        >= 2
    )
