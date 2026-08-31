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
from contextlib import suppress
from pathlib import Path

from syncade import run_status as _run_status
from syncade.exit_codes import (
    FINDINGS_PRESENT,
    MAX_ROUNDS_REACHED,
    SUCCESS,
    WORKSPACE_PRESERVING_EXITS,
)
from syncade.git_object_id import is_full_git_object_id
from syncade.persistence import (
    ProducerArtifactPaths,
    persist_handoff,
    persist_loop_manifest,
    persist_loop_summary,
)
from syncade.producer_result import candidate_disposition
from syncade.workspace_owner import OWNER_RECORD_NAME, remove_workspace_claim, run_id_parts

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
    """Best-effort ``cleanup_all`` of every manager whose workspaces are NOT
    under ``current_round_dir`` (N4).

    The current round's workspaces are deliberately preserved for diagnostics
    on the ``test_worktree_error`` early-raise path; only the now-superseded
    prior-round reviewer/producer workspaces are reclaimed.
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
            # Pass repo_root for best-effort subject lookup of imported candidates.
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
        # Same values persist_loop_summary renders, so the terminal notice and the artifact
        # cannot disagree about what was spent (PR-h-field-06).
        budget_usages=list(run_usages or []),
        budget_ceiling=budget_ceiling,
        budget_tokens=config.loop.budget_tokens,
        budget_usd=config.loop.budget_usd,
    )

    # When the final round had a test-leg worktree provisioning failure, that
    # WorktreeError must be re-raised so the CLI maps to exit 60. The per-round
    # persistence already wrote everything; this is just the bubble-up.
    if last_round.test_worktree_error is not None:
        # The branch-advance warning and the prior-round cleanup both live
        # BELOW this raise, so replay the parts that still apply here:
        # N3 — an earlier imported candidate may have advanced the branch; the
        # operator must hear that even on this exit-60 path.
        if snapshot.branch is not None and branch_advanced_during_run:
            _warn_branch_advanced(branch=snapshot.branch, repo_root=repo_root, logger=logger)
        # N4 — the terminal cleanup below would otherwise reclaim every prior
        # round's reviewer/producer workspaces. Do that here, preserving only
        # the current (failed) round's workspaces for diagnostics.
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
        # the ref moved anyway — an imported candidate whose fast-forward was refused, or
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
    # Producer repositories have independent Git storage and cannot move the
    # operator ref through ordinary actor Git. The warning below is emitted only
    # when trusted import was followed by a successful operator-branch CAS.
    if snapshot.branch is not None and branch_advanced_during_run:
        _warn_branch_advanced(branch=snapshot.branch, repo_root=repo_root, logger=logger)

    # terminal cleanup decision. Per the PRD,
    # exits 10/20/30 keep actor workspaces for inspection; exit 0 +
    # environmental failures (40/50/60/70) clean up. Each
    # WorktreeManager was constructed with defer_cleanup=True so
    # the per-round + per-producer ``with`` blocks didn't
    # auto-clean; the orchestrator now decides based on
    # final_exit_code.
    # ONE authority, shared with every renderer — see `producer_result.candidate_disposition`.
    unlanded_candidate = candidate_disposition(round_results).workspace_is_only_copy
    preserve_worktrees = final_exit_code in WORKSPACE_PRESERVING_EXITS or unlanded_candidate
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
            if unlanded_candidate:
                # Not a verbosity preference: this names the ONLY surviving copy of committed
                # producer work, which is the `Logger.safety` class exactly.
                logger.safety(
                    f"orchestrator: a producer candidate was committed but did NOT reach this "
                    f"repository, so its standalone producer repository is PRESERVED as the only "
                    f"copy of that work. Inspect at {effective_worktree_base / run_id}/."
                )
            else:
                logger.warning(
                    f"orchestrator: actor workspaces PRESERVED on disk for "
                    f"inspection (exit {final_exit_code} keeps them per "
                    f"PRD). Inspect at {effective_worktree_base / run_id}/."
                )

    # Final cleanup of the shared ``<worktree_base>/<run_id>/`` parent the
    # per-round managers nested under. Skipped when ``preserve_worktrees``
    # is set — the point of those exit codes is to leave the tree readable.
    shared_run_dir = effective_worktree_base / run_id
    if not preserve_worktrees:
        _reclaim_shared_run_dir(shared_run_dir)
        if not shared_run_dir.exists():
            remove_workspace_claim(repo_root, run_id_parts(run_id)[0])

    logger.summary(result)
    return result


def _reclaim_shared_run_dir(shared_run_dir: Path) -> None:
    """Remove a run's workspace tree once nothing in it is worth keeping.

    Reviewer and producer managers each rmdir their own ``round-N/`` subdir, but
    on a NO-SHIP round where the producer also ran, the reviewer manager's
    cleanup tries to rmdir ``round-N/`` BEFORE the producer manager removes
    ``round-N/producer-worktree/``. That rmdir fails silently and nothing
    retries it, so empty ``round-N/`` dirs accumulate and the final rmdir fails
    on them. Walking bottom-up first is the fix.

    Best effort throughout: an ``OSError`` means the caller left state behind on
    purpose, and inspection beats tidiness.
    """
    # Never follow a symlinked run root: doing so would delete the ownership
    # record inside the symlink target, an uncontrolled location outside the
    # run tree. Let GC handle it — it applies the same no-follow discipline.
    if shared_run_dir.is_symlink():
        return
    if not shared_run_dir.exists():
        return
    # Deeper dirs first, so emptying a child can empty its parent in turn.
    for child_dir in sorted(
        (p for p in shared_run_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        with suppress(OSError):
            child_dir.rmdir()
    # The ownership record is the last thing standing in a fully cleaned tree,
    # and rmdir would fail on it — turning every clean run into a permanent
    # one-file directory, the exact accumulation the record exists to let GC
    # reclaim. Remove it ONLY when nothing else remains: a tree that survives
    # cleanup must keep its record, or GC can no longer prove the tree is ours
    # and would leave it on disk forever.
    with suppress(OSError):
        if [p.name for p in shared_run_dir.iterdir()] == [OWNER_RECORD_NAME]:
            (shared_run_dir / OWNER_RECORD_NAME).unlink()
    with suppress(OSError):
        shared_run_dir.rmdir()
