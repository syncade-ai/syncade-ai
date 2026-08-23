"""Resume-state rehydration for the multi-round loop.

Split out of ``loop.py`` to keep that module under the LOC gate. The
rebound ``run_review`` calls :func:`_rehydrate_resume_state` exactly once,
before the round loop, when resuming an aborted/interrupted run.
"""

from __future__ import annotations

from pathlib import Path

from syncade.config import SyncadeConfig
from syncade.worktree import WorktreeError

from .results import RoundArtifacts, RoundResult
from .resume import ResumePlan, load_completed_round


def _rehydrate_resume_state(
    resume_plan: ResumePlan,
    run_dir: Path,
    run_id: str,
    config: SyncadeConfig,
) -> tuple[list[RoundResult], list[RoundArtifacts], int, bool, SyncadeConfig]:
    """Rehydrate a resumed run's completed rounds and derive its loop bounds.

    Returns ``(round_results, round_artifacts_list, resumed_round_start,
    branch_advanced_during_run, config)``.

    When resuming, pre-populate ``round_results`` with each completed
    round's RoundResult so downstream consumers (loop-summary rendering,
    handoff rendering, cross-round context disk reads) see a
    consistent picture. ``branch_advanced_during_run`` reflects a committed,
    imported prior candidate whose completed round advanced the branch.
    """
    round_results: list[RoundResult] = []
    round_artifacts_list: list[RoundArtifacts] = []
    branch_advanced_during_run = False
    for r_idx in resume_plan.completed_rounds:
        rehydrated = load_completed_round(run_dir / f"round-{r_idx}")
        if rehydrated is None:
            # plan_resume admitted this round; its manifest
            # disappearing between plan_resume and here is a
            # caller-induced race. Surface as WorktreeError so
            # the CLI exits 60 with a useful message.
            raise WorktreeError(
                f"resumed run {run_id}: completed round {r_idx} "
                f"lost its manifest.json between plan_resume and "
                f"orchestrator entry — race condition or operator "
                f"interference. Cannot proceed."
            )
        round_results.append(rehydrated)
        round_artifacts_list.append(rehydrated.artifacts)
        if (
            rehydrated.producer_result is not None
            and rehydrated.producer_result.outcome == "committed"
        ):
            branch_advanced_during_run = True
    resumed_round_start = resume_plan.resumed_round

    # --- Loop-bound consistency with the resume planner ---------
    # INVARIANT: a resume must run >= 1 round (never zero rounds +
    # a silent exit-0 SHIP). ``plan_resume`` derives ``resumed_round``
    # against ``effective_max_rounds = max(original_max_rounds,
    # --max-rounds override)``, so ``resumed_round`` is always < that
    # effective cap. But the round loop AND the terminator's "final
    # round" check (``round_idx == config.loop.max_rounds - 1`` in
    # ``_run_round_step``) BOTH read ``config.loop.max_rounds`` — the
    # RAW override. When the operator resumes with ``--max-rounds N`` <=
    # ``resumed_round`` (e.g. a 3-round run aborted at round 2 resumed
    # with ``--max-rounds 1``), ``range(resumed_round, N)`` is empty: no
    # round runs, ``final_exit_code`` stays SUCCESS, ``termination_reason``
    # stays None, and ``_finalize_run`` returns a false exit-0 SHIP
    # pointing at a rehydrated NO-SHIP round. Bump ``max_rounds`` to the
    # SAME effective cap the planner used so both the loop bound and the
    # terminator cover ``resumed_round`` (model_copy leaves the caller's
    # config object untouched).
    effective_max_rounds = max(config.loop.max_rounds, resume_plan.max_rounds)
    if effective_max_rounds != config.loop.max_rounds:
        config = config.model_copy(
            update={"loop": config.loop.model_copy(update={"max_rounds": effective_max_rounds})}
        )

    return (
        round_results,
        round_artifacts_list,
        resumed_round_start,
        branch_advanced_during_run,
        config,
    )
