"""One iteration of the multi-round review loop.

``_run_round_step`` runs a single round — snapshot refresh → ``_run_one_round`` →
terminator decision → (if NO-SHIP and rounds remain) producer phase → escalation
honoring → branch advance — and returns a :class:`_RoundStep` signal telling
``run_review`` whether to ``continue`` to the next round or ``break`` out, with
the final exit code / termination reason set on a break.

This is a faithful extraction of the original ``for``-loop body: every ``break``
became a ``return _RoundStep(action="break", ...)`` and the fall-through-to-next-
round path became ``return _RoundStep(action="continue", ...)``. The mutable
``round_results`` / ``round_artifacts_list`` / ``managers_to_cleanup`` lists are
passed in and mutated in place exactly as before; ``current_snapshot`` and the
per-round ``branch_advanced`` flag are returned so the caller can thread them.
"""

from __future__ import annotations

from dataclasses import dataclass

from syncade import run_status
from syncade.adapters.base import ReviewerAdapter
from syncade.exit_codes import (
    BUDGET_EXCEEDED,
    CLARIFICATION_NEEDED,
    CONFIG_ERROR,
    FINDINGS_PRESENT,
    MAX_ROUNDS_REACHED,
    REVIEWER_FAILURE,
    REVIEWER_OUTPUT_UNPARSEABLE,
    SUCCESS,
    WORKTREE_ERROR,
)
from syncade.persistence import (
    persist_producer_result,
    persist_round_manifest,
    persist_run_summary,
)
from syncade.snapshot import take_snapshot

from .branch_advance import _advance_branch_ref
from .budget import approaching_budget, over_budget, review_usages
from .escalation_coverage import escalation_covers_active_blockers
from .producer_phase import _run_producer_phase
from .results import RoundArtifacts, RoundResult
from .round import _run_one_round
from .verdict import (
    _classify_phase_failure,
    deactivated_blocker_details,
    error_is_provider_usage_limit,
    round_hit_provider_usage_limit,
)


@dataclass(frozen=True)
class _RoundStep:
    """The outcome of one loop iteration: whether to continue or break, the
    final exit code / termination reason (set on break), the (possibly
    refreshed) snapshot, and whether this round advanced the branch."""

    action: str  # "continue" | "break"
    current_snapshot: object
    branch_advanced: bool
    final_exit_code: int | None = None
    termination_reason: str | None = None
    budget_ceiling: str | None = None  # "budget_tokens" | "budget_usd" on a budget abort
    budget_warned: bool = False  # True when the 80%-of-ceiling advisory fired this step


def _run_round_step(
    *,
    round_idx,
    current_snapshot,
    resumed_round_start,
    repo_root,
    pr_doc_path,
    run_id,
    run_dir,
    config,
    base_ref,
    resolved_timeout,
    resolved_producer_timeout,
    adapter_factory,
    synthesizer_adapter: ReviewerAdapter | None,
    producer_adapter,
    effective_worktree_base,
    logger,
    started_at,
    managers_to_cleanup,
    round_results,
    round_artifacts_list,
    resume_plan,
    operator_decision,
    force_drift,
    prior_usages,
    branch_advanced_during_run,
    budget_warned,
) -> _RoundStep:
    """Run one round of the loop and return a continue/break signal.

    ``prior_usages`` is the loop's running budget tally from PRIOR rounds (PR-v2-11). It is
    read only for the pre-producer check below — the caller accumulates this round's usage
    after the step returns — so a round whose reviewers already crossed the ceiling skips the
    expensive producer leg instead of spending it and aborting one round later.

    ``branch_advanced_during_run`` is the CUMULATIVE flag from all prior rounds — True when any
    earlier round fast-forwarded the branch. The local ``branch_advanced`` variable tracks only
    THIS round; the cumulative flag is what exit-10 documents must report.
    """
    branch_advanced = False
    round_dir = run_dir / f"round-{round_idx}"

    # Budget-abort-before-producer resume: the review bundle for this round
    # was completed in a prior process and is already rehydrated into
    # round_results[round_idx]. Skip mkdir (dir exists), skip _run_one_round,
    # and proceed directly to the terminator + producer dispatch.
    _budget_abort_resume = (
        resume_plan is not None and resume_plan.budget_aborted_before_producer_round == round_idx
    )

    if _budget_abort_resume:
        # round_results was pre-populated by _rehydrate_resume_state
        round_result = round_results[round_idx]
        run_status.update_phase(f"round-{round_idx}: producer", round_idx)
    else:
        round_dir.mkdir(parents=True, exist_ok=False)
        run_status.update_phase(f"round-{round_idx}: reviewing", round_idx)

        # First iteration: use the initial snapshot (the dirty-
        # tree messaging already fired). For resume, the initial
        # snapshot reflects the resumed round's expected SHA
        # (validated by check_tree_drift unless --force-drift).
        # Subsequent iterations: take a fresh snapshot (HEAD has
        # moved thanks to the previous round's producer commits +
        # branch advance). No new dirty-tree warnings — the new
        # commits are intentional and produce a clean tree.
        if round_idx > resumed_round_start:
            run_status.update_phase(f"round-{round_idx}: snapshotting", round_idx)
            # Use the immutable OID from the round-0 snapshot rather than
            # re-resolving the symbolic base_ref, which can move if the base
            # branch receives new commits between rounds.
            _stable_base = current_snapshot.base_oid or base_ref
            # `base_oid` is ALREADY the effective diff base — round 0 resolved
            # the branch point once. Re-deriving a merge base here would be a
            # no-op for three-dot runs (the merge base of an ancestor and its
            # descendant is the ancestor) but would silently CONVERT a
            # `--two-dot` run to three-dot semantics mid-loop.
            current_snapshot = take_snapshot(repo_root, base_ref=_stable_base, three_dot=False)
            run_status.update_phase(f"round-{round_idx}: reviewing", round_idx)

        round_result = _run_one_round(
            round_idx=round_idx,
            snapshot=current_snapshot,
            repo_root=repo_root,
            pr_doc_path=pr_doc_path,
            run_id=run_id,
            run_dir=run_dir,
            round_dir=round_dir,
            config=config,
            resolved_timeout=resolved_timeout,
            resolved_producer_timeout=resolved_producer_timeout,
            adapter_factory=adapter_factory,
            synthesizer_adapter=synthesizer_adapter,
            producer_adapter=producer_adapter,
            worktree_base=effective_worktree_base,
            logger=logger,
            started_at=started_at,
            managers_to_cleanup=managers_to_cleanup,
            resumed_under_drift=(
                force_drift and resume_plan is not None and round_idx == resumed_round_start
            ),
        )
        round_results.append(round_result)
        round_artifacts_list.append(round_result.artifacts)

    # --- Terminator decision ------------------------------------
    # The round itself produced an exit code. The loop terminator
    # then folds in: producer outcome (if it ran), max-rounds
    # cap, and the per-round exit code's category (success vs
    # findings vs phase-failure).

    # Phase-failure terminators take precedence — they're
    # exit codes 40 / 50 / 60 / 70 indicating subprocess /
    # config / worktree / parse failures. Looping wouldn't
    # help, so terminate immediately.
    # A provider quota refusal reaches here as a reviewer failure (exit 40), which is the
    # wrong verdict: the run is not unreviewable, the operator just has to wait. Route it to
    # exit 25's existing "stopped cleanly, resume later" contract BEFORE the generic branch.
    if round_result.round_exit_code == REVIEWER_FAILURE and round_hit_provider_usage_limit(
        round_result
    ):
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=BUDGET_EXCEEDED,
            termination_reason="provider_usage_limit",
        )

    if round_result.round_exit_code in (
        REVIEWER_FAILURE,
        CONFIG_ERROR,
        WORKTREE_ERROR,
        REVIEWER_OUTPUT_UNPARSEABLE,
    ):
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=round_result.round_exit_code,
            termination_reason=_classify_phase_failure(round_result),
        )

    # No-changes (PR-h-02d D1/D3): pre-dispatch check found an empty diff
    # with a resolved base_oid — no reviewers or subprocesses were dispatched
    # THIS round. Map to exit 0 with a reason that reflects prior-round spend:
    # round 0 → no_changes_to_review (zero total spend); round 1+ → producer_emptied_diff
    # (prior rounds dispatched reviewers/producer and the producer cleaned up all changes).
    if round_result.no_changes_to_review:
        _empty_reason = "no_changes_to_review" if round_idx == 0 else "producer_emptied_diff"
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=SUCCESS,
            termination_reason=_empty_reason,
        )

    # Round SHIPped (clean synth + tests passed if configured) →
    # terminate with SUCCESS.
    if round_result.round_exit_code == SUCCESS:
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=SUCCESS,
            termination_reason="ship",
        )

    # Round escalated (exit 10): two or more distinct reviewers each raised a
    # blocker and every one was deactivated (PR-h-01 increment D). Terminal by
    # construction — the question is whether the synthesizer was RIGHT to rule
    # them all out, which only the operator can answer. Running the producer
    # would be incoherent: there is no active blocker for it to fix.
    if round_result.round_exit_code == CLARIFICATION_NEEDED:
        # The operator gets exit 10 either way; without this document they would
        # have no idea WHY, since nothing is listed as an active blocker.
        if round_result.synth_result is not None and round_result.synth_result.output is not None:
            from syncade.persistence import persist_deactivated_blockers_decision_needed

            persist_deactivated_blockers_decision_needed(
                run_dir,
                round_idx=round_idx,
                run_id=run_id,
                deactivated=deactivated_blocker_details(
                    round_result.dispatch_result, round_result.synth_result.output
                ),
                branch_advanced=branch_advanced_during_run,
            )
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=CLARIFICATION_NEEDED,
            termination_reason="blockers_all_deactivated",
        )

    # Round was NO-SHIP (exit 30): non-dismissed blocker or test
    # failed. If we're at max_rounds-1, terminate. The exit code
    # depends on max_rounds:
    #
    # - max_rounds == 1: single-pass back-compat. The
    #   operator didn't ask for retries; NO-SHIP stays at exit 30.
    #   Exit 20 ("max rounds reached") is semantically a
    #   multi-round-loop-exhausted signal — it doesn't apply when
    #   there was never a producer phase to potentially fix things.
    # - max_rounds > 1: the operator asked for retries AND we
    #   used them all up without converging. Exit 20 +
    #   termination_reason="max_rounds_reached".
    if round_result.round_exit_code != FINDINGS_PRESENT:
        raise RuntimeError(
            "loop round step reached NO-SHIP branch with unexpected "
            f"round_exit_code={round_result.round_exit_code}"
        )

    if round_idx == config.loop.max_rounds - 1:
        if config.loop.max_rounds == 1:
            # Single-pass back-compat: don't remap exit code.
            final_exit_code = FINDINGS_PRESENT
            termination_reason = "findings_present"
        else:
            # Multi-round loop exhausted without convergence.
            final_exit_code = MAX_ROUNDS_REACHED
            termination_reason = "max_rounds_reached"
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=final_exit_code,
            termination_reason=termination_reason,
        )

    # PR-v2-11: pre-producer budget check. Reaching here means NO-SHIP with rounds remaining,
    # i.e. the producer — the round's one expensive conditional leg — is about to run. If this
    # round's reviewers + judge, added to the prior-rounds tally, already crossed the ceiling,
    # abort BEFORE the producer so overshoot is bounded to the review phase, not a whole round.
    # Skip this check for a budget-abort-before-producer resume: the review costs were
    # incurred in the prior process and must not count against the fresh budget tally.
    if not _budget_abort_resume:
        _pre_producer_usages = prior_usages + review_usages(round_result)
        budget_ceiling = over_budget(_pre_producer_usages, config.loop)
        if budget_ceiling is not None:
            return _RoundStep(
                action="break",
                current_snapshot=current_snapshot,
                branch_advanced=branch_advanced,
                final_exit_code=BUDGET_EXCEEDED,
                termination_reason="budget_exceeded",
                budget_ceiling=budget_ceiling,
            )
        # PR-h-field-06 item 3: the 80% advisory fires at BOTH dispatch boundaries.
        # The start-of-round check in loop.py covers the case where prior rounds already
        # entered the band; this catches the case where this round's reviews push the total
        # into the band so the operator is warned before the producer spends more.
        if not budget_warned:
            _approaching = approaching_budget(_pre_producer_usages, config.loop)
            if _approaching is not None:
                from syncade.logging import _approaching_budget_line

                logger.warning(
                    _approaching_budget_line(_approaching, _pre_producer_usages, config.loop)
                )
                budget_warned = True

    # NO-SHIP and rounds remaining → run the producer. on the
    # round being RESUMED after an escalation, feed the operator's
    # recorded decision (read by the CLI from decision.txt) to the
    # producer; every other round gets the sentinel (None here).
    round_operator_decision = (
        operator_decision
        if (resume_plan is not None and round_idx == resumed_round_start)
        else None
    )
    run_status.update_phase(f"round-{round_idx}: producer", round_idx)
    producer_result = _run_producer_phase(
        round_idx=round_idx,
        round_result=round_result,
        run_id=run_id,
        run_dir=run_dir,
        round_dir=round_dir,
        snapshot=current_snapshot,
        repo_root=repo_root,
        pr_doc_path=pr_doc_path,
        config=config,
        resolved_producer_timeout=resolved_producer_timeout,
        producer_adapter=producer_adapter,
        worktree_base=effective_worktree_base,
        logger=logger,
        managers_to_cleanup=managers_to_cleanup,
        operator_decision=round_operator_decision,
    )

    # the escalation honor decision is mechanical set-containment
    # over the synth's own active-blocker set (escalation_coverage). Compute
    # it ONCE here so (a) the per-round summary renders the escalation's TRUE
    # disposition — honored checkpoint vs. uncovered-blocker stall — rather
    # than pointing the operator at a decision-needed.md the rejected path
    # never writes, and (b) the terminator below reuses the same answer. The
    # two surfaces can then never disagree.
    escalation_honored = producer_result.outcome == "escalated" and (
        escalation_covers_active_blockers(
            producer_result.escalation,
            round_result.synth_result.output if round_result.synth_result is not None else None,
        )
    )

    # Attach producer artifacts to the round's bundle.
    producer_paths = persist_producer_result(round_dir, producer_result)
    new_round_artifacts = RoundArtifacts(
        round_idx=round_idx,
        round_dir=round_result.artifacts.round_dir,
        manifest_path=round_result.artifacts.manifest_path,
        summary_path=round_result.artifacts.summary_path,
        findings_md_path=round_result.artifacts.findings_md_path,
        synthesizer_paths=round_result.artifacts.synthesizer_paths,
        test_run_paths=round_result.artifacts.test_run_paths,
        producer_paths=producer_paths,
    )
    # Replace the last round's artifacts/result with the
    # producer-aware version so the rounds list reflects the
    # producer phase.
    round_artifacts_list[-1] = new_round_artifacts
    round_results[-1] = RoundResult(
        round_idx=round_result.round_idx,
        snapshot=round_result.snapshot,
        dispatch_result=round_result.dispatch_result,
        synth_result=round_result.synth_result,
        test_result=round_result.test_result,
        test_skip_reason=round_result.test_skip_reason,
        test_worktree_error=round_result.test_worktree_error,
        producer_result=producer_result,
        round_exit_code=round_result.round_exit_code,
        artifacts=new_round_artifacts,
        # carry the mechanical-check results through the
        # producer-aware rebuild. Dropping them here would empty
        # RoundResult.check_results AND erase the '## Mechanical checks'
        # section / 'checks' manifest array when the manifest + summary
        # are re-written below — findings.md and the raw *.check.* files
        # would silently disagree with summary.md / manifest.json.
        check_results=round_result.check_results,
        filtered_diff_bytes=round_result.filtered_diff_bytes,
        raw_diff_bytes=round_result.raw_diff_bytes,
    )

    # Re-write the manifest with the producer section now populated.
    # Use round_result.snapshot (the snapshot the reviewers actually saw), not
    # current_snapshot: on a force-drift budget-abort resume, current_snapshot
    # reflects the drifted HEAD while round_result.snapshot was rehydrated from
    # the completed round's persisted state. In the normal (non-resume) path the
    # two are identical so this change is safe for both paths.
    persist_round_manifest(
        round_dir,
        round_result.snapshot,
        round_result.dispatch_result,
        round_result.round_exit_code,
        started_at,
        round_result.synth_result,
        round_result.test_result,
        round_result.test_skip_reason,
        round_idx=round_idx,
        producer_result=producer_result,
        producer_provider=config.producer.provider,
        producer_model=config.producer.model,
        check_results=round_result.check_results,
        filtered_diff_bytes=round_result.filtered_diff_bytes,
        raw_diff_bytes=round_result.raw_diff_bytes,
    )
    # Re-render summary.md after the producer phase so the per-round summary
    # includes the producer section and producer-aware next-step guidance.
    persist_run_summary(
        round_dir,
        round_result.snapshot,
        round_result.dispatch_result,
        round_result.round_exit_code,
        started_at,
        round_result.synth_result,
        round_result.test_result,
        round_result.test_skip_reason,
        producer_result=producer_result,
        producer_provider=config.producer.provider,
        producer_model=config.producer.model,
        resumed_under_drift=(
            force_drift and resume_plan is not None and round_idx == resumed_round_start
        ),
        check_results=round_result.check_results,
        escalation_honored=escalation_honored,
        branch_already_advanced=branch_advanced_during_run,
    )

    # --- Producer outcome → loop continuation decision ----------
    if producer_result.outcome == "stalled":
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=FINDINGS_PRESENT,
            termination_reason="producer_stalled",
        )
    if producer_result.outcome == "subprocess_error":
        # Same reasoning as the reviewer/judge branch above: a quota refusal is resumable, not
        # a permanent failure. The producer is the likeliest actor to hit one — it burned 6.6M
        # tokens in a single field round — and routing it to exit 40 would throw away every
        # committed round behind it.
        if error_is_provider_usage_limit(producer_result.error):
            return _RoundStep(
                action="break",
                current_snapshot=current_snapshot,
                branch_advanced=branch_advanced,
                final_exit_code=BUDGET_EXCEEDED,
                termination_reason="provider_usage_limit",
            )
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=REVIEWER_FAILURE,
            termination_reason="producer_subprocess_error",
        )
    if producer_result.outcome == "escalated":
        # the producer flagged a finding as an operator
        # decision. Honor it (checkpoint → exit 10 + decision_needed) ONLY
        # when its finding_indices cover EVERY active blocker in the round
        # — the mechanical set-containment computed above
        # (escalation_covers_active_blockers over the synth's own
        # consolidated_findings, NOT a count and NOT the producer prompt).
        # AC5 ("checkpoint only when the remaining active blockers are all
        # escalated") lives here.
        #
        # An escalation that leaves an active blocker uncovered — or
        # references a non-blocker / out-of-range index (escalated ⟺
        # no-commit, so the producer committed nothing) — did NOT finish
        # the round's work. Treat it as an ordinary stall (exit 30): the
        # operator sees NO-SHIP + the findings + the producer narrative,
        # and the uncovered blocker comes back next run. This closes the
        # codex repro (escalate one, leave one → stall) without false-
        # downgrading a legitimate multi-blocker decision (cover all → honor).
        if escalation_honored:
            # Checkpoint-and-terminate (NOT a blocking wait). The mechanical
            # verdict is untouched (round_exit_code stays 30); the escalation
            # rides alongside as the termination reason. ``escalation_honored``
            # is True only on the escalated outcome, so ``escalation`` is the
            # non-None value ProducerResult enforces for it.
            from syncade.persistence import persist_decision_needed

            persist_decision_needed(
                run_dir,
                round_idx=round_idx,
                escalation=producer_result.escalation,
                run_id=run_id,
                branch_advanced=branch_advanced_during_run,
                check_results=round_result.check_results,
            )
            return _RoundStep(
                action="break",
                current_snapshot=current_snapshot,
                branch_advanced=branch_advanced,
                final_exit_code=CLARIFICATION_NEEDED,
                termination_reason="decision_needed",
            )
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=FINDINGS_PRESENT,
            termination_reason="producer_stalled",
        )

    if producer_result.outcome != "committed":
        raise RuntimeError(
            "loop round step reached branch-advance path with unexpected "
            f"producer outcome={producer_result.outcome!r}"
        )
    run_status.update_phase(f"round-{round_idx}: branch advancing", round_idx)
    advance_status = _advance_branch_ref(
        repo_root=repo_root,
        snapshot=current_snapshot,
        producer_result=producer_result,
        logger=logger,
    )
    # branch-advance failure is a non-SHIP terminal
    # state. Per the brief, a non-descendant ending SHA is
    # "treated as a stall"; update-ref failure (ref moved
    # under us, permissions) has the same stale-SHIP risk
    # if we kept going, so it also terminates. Detached-HEAD
    # cannot advance a named ref either; continuing would make
    # the next reviewers inspect the same stale SHA and could
    # falsely SHIP.
    if advance_status == "advanced":
        branch_advanced = True
        run_status.update_phase(f"round-{round_idx}: branch advanced", round_idx)
        # Clear the escalation checkpoint only after the resumed producer
        # durably applied the operator's decision: committed and branch
        # advanced. Cleaning at resume start would lose the decision if
        # the round aborted before applying it.
        if (
            resume_plan is not None
            and round_idx == resumed_round_start
            and operator_decision is not None
        ):
            from syncade.persistence import (
                DECISION_NEEDED_FILENAME as _DN,
            )
            from syncade.persistence import (
                OPERATOR_DECISION_FILENAME as _OD,
            )

            (run_dir / _DN).unlink(missing_ok=True)
            (run_dir / _OD).unlink(missing_ok=True)
    elif advance_status in (
        "skipped_detached_head",
        "non_descendant",
        "update_ref_failed",
    ):
        return _RoundStep(
            action="break",
            current_snapshot=current_snapshot,
            branch_advanced=branch_advanced,
            final_exit_code=FINDINGS_PRESENT,
            termination_reason="producer_stalled",
        )
    # advance_status == "advanced" — continue to next round. Every
    # non-advanced status terminates above so the next reviewers never
    # inspect stale input from an unadvanced ref.
    return _RoundStep(
        action="continue",
        current_snapshot=current_snapshot,
        branch_advanced=branch_advanced,
        budget_warned=budget_warned,
    )
