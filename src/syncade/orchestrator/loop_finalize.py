"""Loop finalization.

``_finalize_run`` is the post-loop tail of ``run_review``: it writes the
loop-level artifacts (loop-summary.md / loop-manifest.json / handoff.md),
records the per-branch last-reviewed SHA, emits the working-tree-not-synced
note, performs the terminal worktree cleanup-vs-preserve decision, builds the
aggregate :class:`RunResult`, and returns it.

``completed_at`` is computed by the caller and passed in.
"""

from __future__ import annotations

import sys
from pathlib import Path

from syncade import run_status as _run_status
from syncade.exit_codes import (
    CLARIFICATION_NEEDED,
    FINDINGS_PRESENT,
    MAX_ROUNDS_REACHED,
    SUCCESS,
)
from syncade.git_object_id import is_full_git_object_id
from syncade.persistence import (
    ProducerArtifactPaths,
    persist_handoff,
    persist_loop_manifest,
    persist_loop_summary,
)

from .results import RunArtifacts, RunResult


def _warn_branch_advanced(*, branch: str, repo_root, logger) -> None:
    """Emit the working-tree-not-synced warning after a branch advance.

    Shared by the normal finalize tail and the early ``test_worktree_error``
    re-raise path so the operator hears the identical message on both (N3).

    Uses ``logger.safety``, not ``logger.warning``: under ``--quiet`` this was suppressed
    entirely (PR-h-13), and it is the ONLY thing standing between an operator and a
    ``git commit`` that silently reverts the producer's work — the tree still holds the
    pre-advance content, so `git status` shows a fully staged revert.
    """
    logger.safety(
        f"orchestrator: branch refs/heads/{branch} "
        f"has been advanced to the latest producer commit, "
        f"but your working tree at {repo_root} is NOT "
        f"automatically synced. To pick up the producer's fix in your "
        f"working tree: `git reset --hard HEAD` (discards "
        f"any local edits) or `git stash && git reset "
        f"--hard HEAD && git stash pop` (preserves them). "
        f"See loop-summary.md for the commit series and "
        f"per-round artifacts."
    )


def _cleanup_prior_round_managers(managers, *, current_round_dir) -> None:
    """Best-effort ``cleanup_all`` of every manager whose worktrees are NOT
    under ``current_round_dir`` (N4).

    The current round's worktrees are deliberately preserved for diagnostics
    on the ``test_worktree_error`` early-raise path; only the now-superseded
    prior-round reviewer/producer worktrees are reclaimed.
    """
    for manager in managers:
        run_dir = manager.run_dir
        if run_dir == current_round_dir or current_round_dir in run_dir.parents:
            continue
        try:
            manager.cleanup_all()
        except Exception:
            # Best-effort: one manager's failure must not block the others
            # or the re-raise of the captured WorktreeError.
            pass


def _finalize_run(
    *,
    run_dir,
    run_id,
    repo_root,
    snapshot,
    config,
    pr_doc_path,
    round_results,
    round_artifacts_list,
    final_exit_code,
    termination_reason,
    started_at,
    completed_at,
    branch_advanced_during_run,
    managers_to_cleanup,
    effective_worktree_base,
    logger,
    run_usages=None,
    budget_ceiling=None,
) -> RunResult:
    """Write loop-level artifacts, build the aggregate :class:`RunResult`,
    handle last-reviewed recording + worktree cleanup, and return the result.

    ``completed_at`` is supplied by the caller (``run_review``) so the patched
    ``datetime.now`` lookup stays in the rebound caller body.
    """
    # --- Loop terminated --------------------------------------------
    final_round_idx = len(round_results) - 1
    last_round = round_results[-1]

    # write the loop-level artifacts (top-level
    # loop-summary.md + loop-manifest.json). Always written —
    # operators inspecting a multi-round run want the aggregate
    # view, and even single-pass runs benefit from a top-level
    # summary that doesn't require traversing into round-0/.
    #
    # ``termination_reason`` is always set by here — every break
    # path above assigns it. Defensive default just in case a
    # future code path forgets to.
    safe_termination_reason: str = termination_reason or "unknown"
    try:
        loop_summary_path = persist_loop_summary(
            run_dir,
            final_exit_code=final_exit_code,
            final_round=final_round_idx,
            termination_reason=safe_termination_reason,
            rounds=round_results,
            max_rounds=config.loop.max_rounds,
            started_at=started_at,
            completed_at=completed_at,
            # pass repo_root so the loop-summary
            # commit series can look up producer commit subjects via
            # ``git log -1 --pretty=format:%s <ending_sha>``.
            repo_root=repo_root,
            # PR-v2-11: the configured ceiling(s), so the Budget section on a
            # budget_exceeded run can name them alongside the tally.
            budget_tokens=config.loop.budget_tokens,
            budget_usd=config.loop.budget_usd,
            # The ENFORCEMENT tally itself — fresh on --resume, so the reported Budget number
            # is exactly what tripped, not original+resume re-summed from rehydrated rounds.
            budget_usages=run_usages,
            # Which ceiling crossed, so the Budget section names it (both set → first-to-trip).
            budget_ceiling=budget_ceiling,
        )
        persist_loop_manifest(
            run_dir,
            final_exit_code=final_exit_code,
            final_round=final_round_idx,
            termination_reason=safe_termination_reason,
            rounds=round_results,
            max_rounds=config.loop.max_rounds,
            started_at=started_at,
            producer_provider=config.producer.provider,
            producer_model=config.producer.model,
        )
        # structured operator handoff on exit 20 / exit 30
        # paths. persist_handoff gates internally on exit code +
        # active-blocker count so the call is safe to make
        # unconditionally; returns None when the gate skips.
        persist_handoff(
            run_dir,
            final_exit_code=final_exit_code,
            final_round=final_round_idx,
            termination_reason=safe_termination_reason,
            rounds=round_results,
            max_rounds=config.loop.max_rounds,
            pr_doc_path=pr_doc_path,
            repo_root=repo_root,
        )
    except Exception:
        # Loop-level persistence failures shouldn't mask the
        # actual exit code — the per-round artifacts are already
        # on disk. Log via the existing channel and continue.
        loop_summary_path = None
        logger.warning(
            "orchestrator: loop-level persistence failed (per-round "
            "artifacts are still on disk; the top-level "
            "loop-summary.md / loop-manifest.json / handoff.md may be "
            "missing or partial)"
        )

    # Build the aggregate RunArtifacts pointing flat fields at the
    # LAST round (back-compat with single-pass RunArtifacts shape).
    # producer_paths is the brief-documented flat list
    # parallel to rounds[] — one entry per round, None when the
    # round didn't run a producer (SHIP rounds + the final round
    # under max_rounds_reached).
    producer_paths_list: list[ProducerArtifactPaths | None] = [
        r.producer_paths for r in round_artifacts_list
    ]
    # the run-root findings.md (if it was written) lives
    # at <run_dir>/findings.md regardless of round count.
    run_root_findings = run_dir / "findings.md"
    run_root_findings_path: Path | None = run_root_findings if run_root_findings.is_file() else None
    artifacts = RunArtifacts(
        run_dir=run_dir,
        round_dir=last_round.artifacts.round_dir,
        manifest_path=last_round.artifacts.manifest_path,
        summary_path=last_round.artifacts.summary_path,
        findings_md_path=last_round.artifacts.findings_md_path,
        synthesizer_paths=last_round.artifacts.synthesizer_paths,
        test_run_paths=last_round.artifacts.test_run_paths,
        rounds=round_artifacts_list,
        loop_summary_path=loop_summary_path,
        producer_paths=producer_paths_list,
        run_root_findings_md_path=run_root_findings_path,
    )

    result = RunResult(
        artifacts=artifacts,
        snapshot=last_round.snapshot,
        dispatch_result=last_round.dispatch_result,
        exit_code=final_exit_code,
        synth_result=last_round.synth_result,
        test_result=last_round.test_result,
        test_skip_reason=last_round.test_skip_reason,
        test_worktree_error=last_round.test_worktree_error,
        rounds=round_results,
        final_round=final_round_idx,
        termination_reason=termination_reason,
    )

    # When the final round had a test-leg worktree provisioning failure, that
    # WorktreeError must be re-raised so the CLI maps to exit 60. The per-round
    # persistence already wrote everything; this is just the bubble-up.
    if last_round.test_worktree_error is not None:
        # The branch-advance warning and the prior-round cleanup both live
        # BELOW this raise, so replay the parts that still apply here:
        # N3 — an earlier round's producer may have advanced the branch; the
        # operator must hear that even on this exit-60 path.
        if snapshot.branch is not None and branch_advanced_during_run:
            _warn_branch_advanced(branch=snapshot.branch, repo_root=repo_root, logger=logger)
        # N4 — the terminal cleanup below would otherwise reclaim every prior
        # round's reviewer/producer worktrees. Do that here, preserving only
        # the current (failed) round's worktrees for diagnostics.
        _cleanup_prior_round_managers(
            managers_to_cleanup,
            current_round_dir=effective_worktree_base / run_id / f"round-{final_round_idx}",
        )
        logger.summary(result)
        # Finalize with the mechanical reason before re-raising so the CLI's
        # typed WorktreeError handler sees _active=None and is a no-op.
        # Without this, status.json would be overwritten as
        # exception:WorktreeError even though loop-manifest already recorded
        # the mechanical termination_reason (worktree_error).
        _run_status.finalize_active(safe_termination_reason, final_exit_code)
        raise last_round.test_worktree_error

    # --- record the per-branch last-reviewed SHA --------------
    # On a COMPLETED verdict where reviewers actually ran (exits 0/20/30, but NOT
    # no_changes_to_review or producer_emptied_diff which are exit-0 with zero reviewers
    # dispatched), record the SHA a reviewer saw so `--scope since-last-review` bounds its
    # diff to new work. Skipped on phase failures (40/50/60/70), decision_needed (10,
    # resume-pending), detached HEAD (no branch), and empty-diff terminals where no
    # reviewer actually ran (recording an unreviewed anchor would silently skip real work
    # on the next since-last-review diff).
    _skip_reasons = ("no_changes_to_review", "producer_emptied_diff")
    if (
        snapshot.branch is not None
        and final_exit_code in (SUCCESS, MAX_ROUNDS_REACHED, FINDINGS_PRESENT)
        and termination_reason not in _skip_reasons
    ):
        # Record the SHA a reviewer ACTUALLY saw, not a re-read of the branch (R6).
        # `last_round.snapshot` is the final round's — NOT the `snapshot` parameter, which
        # is round 0's and differs on any multi-round run.
        #
        # Re-reading `refs/heads/<branch>` returned whatever the ref pointed at when the
        # run ended, which is the reviewed SHA only because the TERMINAL round never runs a
        # producer (loop_round_step.py special-cases `round_idx == max_rounds - 1`). When
        # the ref moved anyway — a `producer_stalled` run whose fast-forward was refused, or
        # an external commit landing mid-run — it recorded a SHA nobody reviewed, and
        # `--scope since-last-review` then SKIPS that unreviewed work. Reading the value
        # already in hand cannot drift.
        _reviewed_sha = last_round.snapshot.commit_sha
        if is_full_git_object_id(_reviewed_sha):
            from syncade.persistence import persist_last_reviewed as _persist_last_reviewed

            try:
                _persist_last_reviewed(
                    repo_root,
                    branch=snapshot.branch,
                    sha=_reviewed_sha,
                    run_id=run_id,
                    recorded_at_utc=completed_at.isoformat(),
                )
            except OSError as exc:
                # Narrowed from `except Exception: pass` (audit rank 17). Only a real I/O
                # failure is tolerable here — losing last-reviewed costs a wider next diff,
                # never a wrong verdict, so it must not abort a completed run. Anything
                # else (a bug in the writer, a bad SHA) is a defect and must surface.
                #
                # Printed directly to stderr, NOT via logger.warning, which is suppressed
                # in --quiet mode. A persistence failure must be visible regardless of
                # verbosity — the operator needs to know the anchor was not updated.
                print(
                    f"[syncade] warning: could not record last-reviewed SHA for "
                    f"{snapshot.branch}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    # --- Do NOT touch the operator's working tree -----
    # The design is explicit ("Notes for the implementing agent"):
    #
    #   > do not use `git checkout` in the user's working tree
    #   > (don't touch their checkout). The worktree's shared
    #   > `.git` is the contract surface.
    #
    # Branch advance moves the ref; the operator syncs their working tree
    # themselves using the documented command in the loop summary.
    #
    # The remaining UX gap (operator runs ``cat foo.py`` post-loop
    # and sees old content) is addressed via documentation:
    # loop-summary.md's "Next steps" block names the sync command
    # explicitly on every SHIP termination. Operators handle the
    # checkout themselves — same contract as a remote ``git push``
    # to their branch from elsewhere.
    if snapshot.branch is not None and branch_advanced_during_run:
        _warn_branch_advanced(branch=snapshot.branch, repo_root=repo_root, logger=logger)

    # terminal cleanup decision. Per the PRD,
    # exits 10/20/30 keep worktrees for inspection; exit 0 +
    # environmental failures (40/50/60/70) clean up. Each
    # WorktreeManager was constructed with defer_cleanup=True so
    # the per-round + per-producer ``with`` blocks didn't
    # auto-clean; the orchestrator now decides based on
    # final_exit_code.
    preserve_worktrees = final_exit_code in (
        CLARIFICATION_NEEDED,
        MAX_ROUNDS_REACHED,
        FINDINGS_PRESENT,
    )
    if not preserve_worktrees:
        for manager in managers_to_cleanup:
            try:
                manager.cleanup_all()
            except Exception:
                # Best-effort: a cleanup failure on one manager
                # doesn't block the others or block the final
                # ``rmdir(<base>/<run_id>/)``. Surface via a
                # generic warning rather than the per-manager
                # stderr the WorktreeManager.cleanup_all already
                # writes; we don't want to double-warn.
                pass
    else:
        # Surface the preservation in the operator's terminal
        # log so they know where to look.
        if managers_to_cleanup:
            logger.warning(
                f"orchestrator: worktrees PRESERVED on disk for "
                f"inspection (exit {final_exit_code} keeps them per "
                f"PRD). Inspect at {effective_worktree_base / run_id}/."
            )

    # final cleanup of the per-run worktree-base parent. The
    # per-round WorktreeManagers each cleaned up their round-N
    # subdirs, but the shared ``<worktree_base>/<run_id>/`` parent
    # they were nested under is still on disk.
    #
    # the per-round managers' cleanup_all rmdirs
    # their own ``round-N/`` subdirs, BUT on a NO-SHIP round where
    # the producer also ran, the reviewer-manager's cleanup_all
    # tries to rmdir ``round-N/`` BEFORE the producer-manager
    # cleans up its own ``round-N/producer-worktree/`` — the rmdir
    # fails silently (dir not empty), the producer phase runs and
    # cleans its subdir, but nothing goes back to retry the
    # ``round-N/`` rmdir. Result: empty ``round-N/`` directories
    # accumulate under ``<base>/<run_id>/`` even on successful
    # runs, and the post-loop ``rmdir(<base>/<run_id>/)`` fails
    # because of the empty children.
    #
    # Fix: walk the run_dir bottom-up and rmdir every empty subdir
    # before the final ``rmdir(run_dir)``. Best-effort: any OSError
    # is swallowed (caller may have left intentional state).
    #
    # skipped when ``preserve_worktrees`` is True — the
    # whole point is to leave the dirs on disk for inspection.
    shared_run_dir = effective_worktree_base / run_id
    if not preserve_worktrees and shared_run_dir.exists():
        # Walk bottom-up so deeper empty dirs get rmdir'd first,
        # making their parents potentially empty for the next step.
        for child_dir in sorted(
            (p for p in shared_run_dir.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                child_dir.rmdir()
            except OSError:
                # Not empty (operator left something) — skip.
                pass
        try:
            shared_run_dir.rmdir()
        except OSError:
            # Not empty; leave it for inspection.
            pass

    logger.summary(result)
    return result
