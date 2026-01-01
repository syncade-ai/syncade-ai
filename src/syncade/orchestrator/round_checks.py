"""Mechanical-checks leg of the per-round pipeline."""

from __future__ import annotations

from syncade.persistence import persist_check_result
from syncade.test_runner import TestRunResult, run_tests
from syncade.worktree import WorktreeError


def _run_checks_leg(
    *,
    manager,
    config,
    snapshot,
    dispatch_result,
    synth_result,
    resolved_timeout,
    round_dir,
    logger,
) -> list[TestRunResult]:
    """Run the configured mechanical checks in stripped worktrees and return
    their results (``[]`` when the gate isn't met / no checks configured).

    Gate: reviewers succeeded AND the synth produced output — INCLUDING
    synth-blocker rounds, so advisory drift surfaces on every reviewable round
    (the check scenario). Skipped entirely when no checks are configured
    (zero-config → byte-identical) or when reviewers failed / synth errored.
    """
    check_results: list[TestRunResult] = []
    if not (
        config.checks
        and dispatch_result.all_succeeded
        and synth_result is not None
        and synth_result.output is not None
    ):
        return check_results

    check_timeout = (
        config.loop.test_timeout_seconds
        if config.loop.test_timeout_seconds is not None
        else resolved_timeout
    )
    logger.event(f"running {len(config.checks)} mechanical check(s)")
    for check in config.checks:
        try:
            check_worktree = manager.create(
                reviewer_name=check.name,
                commit_sha=snapshot.commit_sha,
                strip_files=config.review.strip_repo_context_files,
            )
        except WorktreeError as exc:
            # A check whose worktree can't be provisioned counts as a
            # subprocess_error (the check couldn't run): a blocking
            # check then → REVIEWER_FAILURE, advisory → surfaced
            # only. Name collisions are rejected at config-load, so
            # this is a disk/path failure, not a name clash.
            check_results.append(
                TestRunResult(
                    name=check.name,
                    severity=check.severity,
                    exit_code=-1,
                    outcome="subprocess_error",
                    duration_seconds=0.0,
                    stdout="",
                    stderr="",
                    error=exc,
                    command=check.command,
                )
            )
            continue
        logger.event(f"  worktree ready: {check.name} -> {check_worktree.path}")
        check_result = run_tests(
            worktree_path=check_worktree.path,
            test_command=check.command,
            timeout_seconds=check_timeout,
            name=check.name,
            severity=check.severity,
        )
        prefix = (
            f"  check {check_result.name!r} ({check_result.severity}) finished in "
            f"{check_result.duration_seconds:.1f}s — "
        )
        if check_result.outcome == "subprocess_error":
            err_cls = type(check_result.error).__name__ if check_result.error else "Unknown"
            logger.event(f"{prefix}SUBPROCESS_ERROR ({err_cls})", error=True)
        else:
            logger.event(f"{prefix}{check_result.outcome.upper()} (exit {check_result.exit_code})")
        check_results.append(check_result)
    for cr in check_results:
        persist_check_result(round_dir, cr)
    return check_results
