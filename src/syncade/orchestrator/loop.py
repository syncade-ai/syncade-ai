"""Top-level multi-round review loop driver."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from syncade import __version__, run_status
from syncade.adapters.base import ReviewerAdapter
from syncade.adapters.producer import ProducerAdapter
from syncade.config import SyncadeConfig
from syncade.exit_codes import BUDGET_EXCEEDED, SUCCESS, WORKTREE_ERROR
from syncade.logging import Logger, _approaching_budget_line
from syncade.persistence import persist_run_init
from syncade.persistence._atomic import atomic_write_text
from syncade.run_inputs import validate_run_inputs
from syncade.snapshot import SnapshotError, discover_repo_root, take_snapshot
from syncade.worktree import WorktreeError, WorktreeManager, generate_run_id

from ._runs_dir import _ensure_runs_gitignore
from .budget import approaching_budget, over_budget, producer_only_usages, round_usages
from .loop_dispatch_check import _diff_will_dispatch as _diff_will_dispatch
from .loop_finalize import _finalize_run
from .loop_preflight import run_preflight
from .loop_resume import _rehydrate_resume_state
from .loop_rmtree import _safe_resume_rmtree
from .loop_round_step import _run_round_step
from .results import RoundArtifacts, RoundResult, RunResult, TerminationReason
from .resume import ResumePlan, check_tree_drift
from .resume_types import ResumeError

if TYPE_CHECKING:
    from syncade.usage import Usage


def _autoprune_old_transcripts(
    repo_root: Path, logger: Logger, *, keep: int, max_age_days: int
) -> None:
    """Keep ``.syncade/runs/`` bounded without anyone remembering ``syncade --gc``.

    **Never fails a review.** Auto-prune is housekeeping; a review is the product. Any
    failure here — unreadable dir, permissions, a corpus mid-write by a concurrent
    syncade — is swallowed and logged, never raised. The alternative (a disk-cleanup
    error aborting an expensive multi-round loop) is strictly worse than a fat
    ``.syncade/``.

    **Quiet unless it did something.** A no-op prune prints nothing; the operator's
    pane is for the review, not for housekeeping that freed zero bytes.

    The import is function-local on purpose — the same reason
    :func:`_safe_resume_rmtree` defers its GC imports. ``syncade.gc`` reaches
    ``gc_protection`` → ``orchestrator.resume`` → ``orchestrator/__init__`` → this
    module, so importing it at module scope makes ``import syncade.gc`` fail with a
    partially-initialized-module error. The CLI happened to survive that (it enters
    through a different module first) and the whole test suite happened to survive it
    (pytest imports ``orchestrator`` first) — which is exactly why it has to be
    deferred rather than trusted.
    """
    from syncade.gc import autoprune_transcripts

    try:
        report = autoprune_transcripts(repo_root, keep=keep, max_age_days=max_age_days)
    except Exception as exc:  # noqa: BLE001 — housekeeping must never abort a review
        logger.warning(f"auto-prune skipped ({type(exc).__name__}: {exc})")
        return
    # Report on runs SLIMMED, not bytes freed — the same distinction execute_gc keys
    # on. A run whose transcripts are all zero-byte is still mutated, and gating the
    # log on bytes would leave the loop silent about work it actually did.
    if report.runs_slimmed:
        mb = report.bytes_freed / (1024 * 1024)
        logger.event(
            f"auto-pruned {len(report.runs_slimmed)} old run(s) — "
            f"{mb:.1f} MB of transcripts freed (run history kept)"
        )


def run_review(
    *,
    repo_root: Path,
    pr_doc_path: Path,
    config: SyncadeConfig,
    base_ref: str | None = None,
    timeout_seconds: float | None = None,
    logger: Logger | None = None,
    adapter_factory: Callable[[str], ReviewerAdapter] | None = None,
    synthesizer_adapter: ReviewerAdapter | None = None,
    producer_adapter: ProducerAdapter | None = None,
    worktree_base: Path | None = None,
    force_dirty: bool = False,
    two_dot: bool = False,
    resume_plan: ResumePlan | None = None,
    force_drift: bool = False,
    operator_decision: str | None = None,
    pr_doc_artifact_name: str | None = None,
    allow_default_branch: bool = False,
) -> RunResult:
    """Execute a multi-round review loop against ``repo_root``.

    For ``config.loop.max_rounds == 1``, run one round without
    provisioning a producer subprocess.

    For ``max_rounds > 1``, wraps the per-round pipeline in a loop:
    each iteration runs snapshot → reviewers → synth → optional test
    → verdict → (if NO-SHIP and rounds remain) producer → branch
    advance → next round. The loop terminates as soon as:

    - **SHIP** at any round → exit 0 with
      ``termination_reason="ship"``.
    - **Max rounds reached** (last round was NO-SHIP) → exit 20
      with ``termination_reason="max_rounds_reached"``.
    - **Producer stall** (subprocess clean but no commit, OR an
      escalation that does not cover every active blocker) → exit 30
      with ``termination_reason="producer_stalled"``.
    - **Decision needed** (the producer escalated and its
      ``finding_indices`` covered every active blocker) → exit
      10 with ``termination_reason="decision_needed"`` +
      ``decision-needed.md``.
    - **Budget exceeded** (token or dollar ceiling hit mid-loop) →
      exit 25 with ``termination_reason="budget_exceeded"``.
    - **Provider usage limit** (the account's quota window is empty, so
      no actor can be dispatched) → also exit 25, but with
      ``termination_reason="provider_usage_limit"``: same resumable
      stop, a cause the operator did not configure and cannot raise.
    - **Subprocess / parse / worktree / config error** → exit
      40/50/60/70 with the appropriate categorical reason.

    Args:
        repo_root: Path inside the git repo. Resolved to the actual
            git root via
            :func:`~syncade.snapshot.discover_repo_root` before any
            side effects so ``.syncade/`` artifacts land at the repo
            root regardless of invocation cwd.
        pr_doc_path: Path to the PR doc; substituted into both the
            reviewer + producer prompts.
        config: The loaded :class:`~syncade.config.SyncadeConfig`.
            ``config.loop.max_rounds`` drives loop iteration count;
            ``config.loop.test_command`` (if set) drives the per-
            round test re-run leg; ``config.producer`` drives the
            producer adapter config (provider / model / thinking /
            permissions / timeout).
        base_ref: Optional git ref the diff is rendered against on
            every round. ``None`` produces the no-diff sentinel and
            the reviewers operate against the full HEAD state.
        timeout_seconds: Per-reviewer wall-clock timeout. Falls back
            to ``config.loop.timeout_seconds``. Same value is used
            for the synthesizer + (when unset)
            ``config.producer.timeout_seconds`` resolution.
        logger: Optional :class:`~syncade.logging.Logger`. Default
            constructs a normal-verbosity logger.
        adapter_factory: Reviewer adapter factory (default: the
            production registry). Tests inject fakes.
        synthesizer_adapter: Optional explicit synthesizer adapter
            (default: resolved from the registry per round via ``config.synthesizer.provider``).
            Tests pass :class:`FakeSynthesizerAdapter`. NOTE: when
            a single adapter instance is supplied for a multi-round
            run, it's reused across rounds — if the adapter has
            per-call state (e.g. canned outputs that should vary
            per round), the caller is responsible for that.
        producer_adapter: Optional explicit producer adapter
            (default: registry lookup using
            ``config.producer.provider``). Tests pass
            :class:`FakeProducerAdapter`.
        worktree_base: Override for the per-run worktree base
            directory. When ``None``, falls back to
            ``config.worktree_base`` (which itself defaults to
            :data:`DEFAULT_WORKTREE_BASE` = ``/tmp/syncade/``).
            Tests use ``tmp_path`` to avoid cross-run collisions.
        two_dot: When ``True``, diff the literal ``base..HEAD`` range instead
            of from the branch point. The escape hatch for "show me everything
            between these two commits"; it re-introduces phantom deletions for
            a branch that is behind its base, which is why it is opt-in.
        force_dirty: When ``True``, bypasses the loop-mode dirty-
            tree refusal. Plumbed from the CLI's ``--force-dirty``
            flag. Only relevant when ``max_rounds > 1`` AND
            ``snapshot.dirty_state in {"tracked", "both"}``.
        pr_doc_artifact_name: Fresh-run only. When set, copy the
            input PR doc into ``<run_dir>/<name>`` before writing
            ``run-init.json`` and use that persisted artifact as the
            PR doc for every round. This keeps generated inputs such
            as ``--openspec`` resumable after their staging tempfile
            is removed.

    Returns:
        :class:`RunResult` carrying the loop's final exit code,
        the per-round results, and the aggregate artifacts.

    Raises:
        FileNotFoundError / NotADirectoryError: For invalid inputs.
        :class:`SnapshotError`: When ``repo_root`` isn't in a git
            repo or the snapshot fails.
        :class:`WorktreeError`: When a worktree can't be provisioned.
            Bubbles up to the CLI which maps to exit 60.
    """
    logger = logger if logger is not None else Logger()

    # Run-start timestamp captured once and threaded into every per-
    # round manifest / summary so they all agree on when the RUN
    # began (not when each file happened to be written).
    started_at = datetime.now(tz=UTC)

    # --- Input validation -------------------------------------------
    validate_run_inputs(repo_root, pr_doc_path)

    repo_root = repo_root.resolve()
    pr_doc_path = pr_doc_path.resolve()
    repo_root = discover_repo_root(repo_root)

    # --- Round-0 snapshot (the starting point for the whole loop) ---
    # On resume, use the immutable OID from the plan rather than the caller-supplied
    # symbolic ref — the symbolic ref may have moved since the original run.
    logger.event(f"snapshotting repo at {repo_root}")
    _resumed_base = resume_plan.base_oid if resume_plan is not None else None
    _snapshot_base_ref = _resumed_base if _resumed_base is not None else base_ref
    # A resumed base_oid is ALREADY the effective diff base the original run
    # resolved, so re-deriving a branch point from it would move the review
    # target — and would convert a `--two-dot` run to three-dot on resume.
    snapshot = take_snapshot(
        repo_root,
        base_ref=_snapshot_base_ref,
        three_dot=not two_dot and _resumed_base is None,
    )
    branch = snapshot.branch or "(detached HEAD)"
    logger.event(f"snapshot taken — {snapshot.commit_sha[:12]} on {branch}")

    state = snapshot.dirty_state

    run_preflight(
        config=config,
        repo_root=repo_root,
        pr_doc_path=pr_doc_path,
        snapshot=snapshot,
        state=state,
        branch=branch,
        resume_plan=resume_plan,
        logger=logger,
        force_dirty=force_dirty,
        allow_default_branch=allow_default_branch,
    )
    # CLI passes config.worktree_base explicitly; direct API callers that omit the kwarg
    # still get the configured base (not always DEFAULT_WORKTREE_BASE).
    effective_worktree_base = worktree_base if worktree_base is not None else config.worktree_base
    runs_root = repo_root / ".syncade" / "runs"
    if resume_plan is None:
        # Auto-prune BEFORE this run's directory exists, so the run we are about to
        # start cannot be a candidate for its own pruning. Fresh runs only: a resume
        # reuses an existing run dir and may still need its transcripts, so pruning
        # there is risk without benefit.
        _autoprune_old_transcripts(
            repo_root, logger, keep=config.gc.keep, max_age_days=config.gc.max_age_days
        )

        # FRESH RUN: generate run-id with collision resolution and
        # mkdir the run directory.
        base_run_id = generate_run_id()
        run_id = base_run_id
        attempt = 1
        while True:
            run_dir = runs_root / run_id
            tmp_run_dir = effective_worktree_base / run_id
            # Reserve the global /tmp worktree dir atomically rather than
            # check-then-act on .exists(): mkdir(exist_ok=False) wins or
            # raises, so a concurrent run racing on the same timestamp
            # run-id cannot also claim this tmp subtree.
            try:
                tmp_run_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                attempt += 1
                run_id = f"{base_run_id}-{attempt}"
                if attempt > 100:
                    raise RuntimeError(
                        f"could not find a free run_id after {attempt} attempts "
                        f"(base={base_run_id!r}); both <repo>/.syncade/runs/ "
                        f"and {effective_worktree_base}/ have stale entries — clean one up"
                    ) from None
                continue
            except OSError as exc:
                raise WorktreeError(
                    f"cannot create run directory under worktree_base "
                    f"{effective_worktree_base!r}: {exc}"
                ) from exc
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                # Release the tmp reservation we just took for this id so a
                # run-dir collision does not strand an empty /tmp subtree.
                tmp_run_dir.rmdir()
                attempt += 1
                run_id = f"{base_run_id}-{attempt}"
                if attempt > 100:
                    raise
        _ensure_runs_gitignore(runs_root)
        # Begin the breadcrumb immediately after the run dir exists. The
        # post-begin setup (PR-doc artifact copy, persist_run_init) is guarded
        # below so any signal or exception in that window finalizes status.json
        # rather than leaving it stuck as "running".
        run_status.begin(run_dir, started_at)

        try:
            if pr_doc_artifact_name is not None:
                artifact_name = Path(pr_doc_artifact_name).name
                if not artifact_name:
                    raise ValueError("pr_doc_artifact_name must include a filename")
                persisted_pr_doc_path = (run_dir / artifact_name).resolve()
                atomic_write_text(
                    persisted_pr_doc_path,
                    pr_doc_path.read_text(encoding="utf-8"),
                )
                pr_doc_path = persisted_pr_doc_path

            # Capture the run's initial state before any round directory is
            # created. Resume reads this artifact but does not rewrite it.
            persist_run_init(
                run_dir,
                syncade_version=__version__,
                started_at=started_at,
                pr_doc_path=pr_doc_path,
                base_ref=base_ref,
                base_oid=snapshot.base_oid,
                starting_sha=snapshot.commit_sha,
                operator_branch=snapshot.branch,
                max_rounds=config.loop.max_rounds,
                config=config,
            )
        except KeyboardInterrupt:
            if not run_status.received_signal():
                run_status.finalize_active("exception:KeyboardInterrupt", None)
            raise
        except BaseException as _exc:
            run_status.finalize_active(f"exception:{type(_exc).__name__}", None)
            raise
    else:
        # Resume reuses the original run-id and run directory and leaves
        # run-init.json unchanged.
        run_id = resume_plan.run_id
        run_dir = resume_plan.run_dir
        _ensure_runs_gitignore(runs_root)
        # Register the breadcrumb immediately for resume: the reused run dir already
        # holds a (possibly stale `running`) status.json from the original run, and
        # the drift-check / rehydration below can raise or be signalled. Beginning
        # here closes the pre-begin gap where a signal would leave the OLD status.json
        # stale while stderr claims the signal was recorded.
        run_status.begin(run_dir, started_at)

        try:
            # Tree-drift check: refuse unless the operator's current HEAD
            # matches the resumed round's expected SHA. --force-drift
            # bypasses the check (the resumed round then snapshots from
            # current HEAD; see the "Resumed under tree drift" log line
            # in the resumed round's summary.md).
            if not force_drift:
                try:
                    check_tree_drift(
                        repo_root,
                        expected_sha=resume_plan.expected_sha,
                        expected_branch=resume_plan.expected_branch,
                    )
                except Exception as exc:
                    # TreeDriftError + ResumeError both bubble up here.
                    # The CLI maps both to exit 60. Reraise without
                    # additional context.
                    raise WorktreeError(str(exc)) from exc

            # Drop the resumed round's stale state if any. The on-disk
            # directory and the worktree subtree at /tmp/syncade/<run-id>/
            # round-N/ are remnants of the aborted attempt; we re-run from
            # snapshot. Both removals go through _safe_resume_rmtree, which
            # applies the same containment + identity guards GC uses so a
            # swapped symlink or out-of-base path can never redirect the
            # delete; missing targets safely no-op (idempotent). Only the
            # EXTERNAL worktree subtree is reaped (reap=True), matching GC's
            # worktree-tree removal; the persisted .syncade/runs artifact dir
            # uses a plain guarded rmtree (reap=False) — an operator may be
            # inspecting it, and SIGKILL would over-reach inside .syncade/runs/ (M2).
            #
            # Exception: for a budget-abort-before-producer resume the round
            # directory holds a complete review bundle that we will rehydrate —
            # do NOT delete it. No external worktree was created (the producer
            # never ran), so there is nothing to prune.
            _is_budget_abort_resume = resume_plan.budget_aborted_before_producer_round is not None
            if not _is_budget_abort_resume:
                resumed_round_dir = run_dir / f"round-{resume_plan.resumed_round}"
                _safe_resume_rmtree(resumed_round_dir, runs_root, repo_root, reap=False)
                resumed_worktree_dir = (
                    effective_worktree_base / run_id / f"round-{resume_plan.resumed_round}"
                )
                _safe_resume_rmtree(
                    resumed_worktree_dir, effective_worktree_base, repo_root, reap=True
                )
                # Terminal rounds that preserve worktrees also leave git worktree
                # registry entries. The rmtree above removed the dirs, but stale registry
                from syncade.process import run_subprocess as _run_subprocess

                try:
                    _run_subprocess(["git", "worktree", "prune"], cwd=repo_root, timeout=10.0)
                except Exception:
                    pass

            # Warn on syncade-version drift. This is informational only.
            if resume_plan.syncade_version and resume_plan.syncade_version != __version__:
                logger.warning(
                    f"orchestrator: resuming run {run_id} started under "
                    f"syncade {resume_plan.syncade_version}; current "
                    f"syncade is {__version__}. Behavior may differ; "
                    f"inspect the resumed round's artifacts if surprised."
                )

            # When force-drift is used, surface the drift now and in the resumed
            # round's durable summary.md annotation.
            if force_drift:
                logger.warning(
                    f"orchestrator: resuming run {run_id} under tree drift "
                    f"(--force-drift). The resumed round-{resume_plan.resumed_round} "
                    f"will snapshot from current HEAD; the original run's "
                    f"expected SHA was {resume_plan.expected_sha[:12]}. "
                    f"Cross-round context from prior rounds references findings "
                    f"against the ORIGINAL tree state, not the current one — "
                    f"reviewers may surface inconsistencies."
                )
        except KeyboardInterrupt:
            if not run_status.received_signal():
                run_status.finalize_active("exception:KeyboardInterrupt", None)
            raise
        except WorktreeError as _exc:
            run_status.finalize_active(f"exception:{type(_exc).__name__}", WORKTREE_ERROR)
            raise
        except BaseException as _exc:
            run_status.finalize_active(f"exception:{type(_exc).__name__}", None)
            raise

    try:
        # --- Timeout resolution -----------------------------------------
        resolved_timeout = (
            timeout_seconds if timeout_seconds is not None else config.loop.timeout_seconds
        )
        # Producer timeout: explicit > shared reviewer timeout (per
        # config.producer.timeout_seconds docs).
        resolved_producer_timeout = (
            config.producer.timeout_seconds
            if config.producer.timeout_seconds is not None
            else resolved_timeout
        )

        # --- Loop state -------------------------------------------------
        round_results: list[RoundResult] = []
        round_artifacts_list: list[RoundArtifacts] = []
        # Track every WorktreeManager opened during the loop. They defer cleanup so
        # finalization can preserve diagnostic worktrees on operator-action exits
        # and clean them on success/error exits.
        managers_to_cleanup: list[WorktreeManager] = []
        termination_reason: TerminationReason | None = None
        final_exit_code: int = SUCCESS
        branch_advanced_during_run = False
        # PR-v2-11: running per-actor usage tally for the budget checks. Starts empty even on
        # --resume (a fresh tally: the prior process already spent that; this run bounds only
        # what IT spends), so a resumed loop is never aborted for a predecessor's cost.
        run_usages: list[Usage] = []
        budget_warned = False  # the 80% heads-up fires once per run, not once per round
        # Which ceiling tripped ("budget_tokens" | "budget_usd"), so the Budget section can
        # name it (both set → first-to-trip, tokens checked first). None unless a budget abort.
        budget_ceiling: str | None = None

        # The current snapshot — refreshed each round when the previous
        # round's producer advanced the branch.
        current_snapshot = snapshot

        # --- Resume rehydration -----------------------------------------
        # Rehydrate each completed round and derive the resumed loop bounds.
        resumed_round_start: int = 0
        if resume_plan is not None:
            (
                round_results,
                round_artifacts_list,
                resumed_round_start,
                branch_advanced_during_run,
                config,
            ) = _rehydrate_resume_state(resume_plan, run_dir, run_id, config)

        # --- The round loop ---------------------------------------------
        # Each iteration mutates the round/result/cleanup lists in place and
        # returns a continue/break signal.
        for round_idx in range(resumed_round_start, config.loop.max_rounds):
            # PR-v2-11: refuse to START a round once prior rounds' accumulated spend already
            # crossed a budget (round 0 always runs — the tally is empty). A no-op when no
            # ceiling is active (the 0 sentinel), so an opted-out run's control flow is unchanged.
            budget_ceiling = over_budget(run_usages, config.loop)
            if budget_ceiling is not None:
                final_exit_code = BUDGET_EXCEEDED
                termination_reason = "budget_exceeded"
                break
            # Advisory heads-up at the SAME boundary the ceiling is enforced at, so the
            # operator sees the stop coming with a round left to react in (PR-h-field-06).
            # Checked only when over_budget stayed silent, so the two never both speak. Fired
            # ONCE per run: a warning repeated every round is a warning nobody reads, and it
            # would be loudest exactly when the operator is already watching a long run.
            if not budget_warned:
                approaching = approaching_budget(run_usages, config.loop)
                if approaching is not None:
                    budget_warned = True
                    logger.warning(_approaching_budget_line(approaching, run_usages, config.loop))
            step = _run_round_step(
                round_idx=round_idx,
                current_snapshot=current_snapshot,
                resumed_round_start=resumed_round_start,
                repo_root=repo_root,
                pr_doc_path=pr_doc_path,
                run_id=run_id,
                run_dir=run_dir,
                config=config,
                base_ref=base_ref,
                resolved_timeout=resolved_timeout,
                resolved_producer_timeout=resolved_producer_timeout,
                adapter_factory=adapter_factory,
                synthesizer_adapter=synthesizer_adapter,
                producer_adapter=producer_adapter,
                effective_worktree_base=effective_worktree_base,
                logger=logger,
                started_at=started_at,
                managers_to_cleanup=managers_to_cleanup,
                round_results=round_results,
                round_artifacts_list=round_artifacts_list,
                resume_plan=resume_plan,
                operator_decision=operator_decision,
                force_drift=force_drift,
                prior_usages=run_usages,
                branch_advanced_during_run=branch_advanced_during_run,
                budget_warned=budget_warned,
            )
            current_snapshot = step.current_snapshot
            if step.branch_advanced:
                branch_advanced_during_run = True
            if step.budget_warned:
                budget_warned = True
            # Accumulate the round just run (reviewers + judge + any producer) BEFORE the
            # break check, so the final tally the summary/metrics report includes the round
            # that crossed — whether it broke here or the step's pre-producer check broke it.
            # For a budget-abort-before-producer resume the review bundle was paid for in the
            # prior process; only the producer cost counts against the fresh tally.
            _is_budget_abort_round = (
                resume_plan is not None
                and resume_plan.budget_aborted_before_producer_round == round_idx
            )
            run_usages.extend(
                producer_only_usages(round_results[-1])
                if _is_budget_abort_round
                else round_usages(round_results[-1])
            )
            if step.action == "break":
                final_exit_code = step.final_exit_code
                termination_reason = step.termination_reason
                budget_ceiling = step.budget_ceiling  # None unless a pre-producer budget abort
                break

        # --- Loop terminated --------------------------------------------
        # completed_at captured here (in the rebound body) so the patched
        # ``datetime`` lookup stays inside run_review; passed into
        # _finalize_run, which therefore references no monkeypatched name.
        completed_at = datetime.now(tz=UTC)
        result = _finalize_run(
            run_dir=run_dir,
            run_id=run_id,
            repo_root=repo_root,
            snapshot=snapshot,
            config=config,
            pr_doc_path=pr_doc_path,
            round_results=round_results,
            round_artifacts_list=round_artifacts_list,
            final_exit_code=final_exit_code,
            termination_reason=termination_reason,
            started_at=started_at,
            completed_at=completed_at,
            branch_advanced_during_run=branch_advanced_during_run,
            managers_to_cleanup=managers_to_cleanup,
            effective_worktree_base=effective_worktree_base,
            logger=logger,
            run_usages=run_usages,
            budget_ceiling=budget_ceiling,
        )
        # Normal exit: _finalize_run already finalized status on the
        # test_worktree_error re-raise path (mechanical reason recorded
        # before raise). For every other normal return, finalize here.
        run_status.finalize_active(result.termination_reason or "unknown", result.exit_code)
        return result
    except KeyboardInterrupt:
        # Signal-induced KI: run_status._handler recorded the signum and
        # raised KI. Preserve the active breadcrumb so the CLI's
        # finalize_signal() can write signal:<NAME> + 128+signum. For a
        # pure (non-signal) KI there is no CLI handler waiting — finalize here.
        if not run_status.received_signal():
            run_status.finalize_active("exception:KeyboardInterrupt", None)
        raise
    except (WorktreeError, SnapshotError, ResumeError) as exc:
        # These all map to exit 60 in the CLI. Finalize with that code so status.json
        # matches the process exit for both direct API callers and CLI callers (whose
        # typed handler is a no-op after this clears _active) — not a null exit_code
        # that would disagree with the mapped exit.
        run_status.finalize_active(f"exception:{type(exc).__name__}", WORKTREE_ERROR)
        raise
    except Exception as exc:
        # Unexpected exception: finalize for direct API callers (no CLI
        # wrapper). If _finalize_run already finalized (mechanical re-raise
        # path), _active is None and this is a no-op.
        run_status.finalize_active(f"exception:{type(exc).__name__}", None)
        raise
    except BaseException as exc:
        # A parent-side SystemExit (or any non-Exception BaseException that is not the
        # KeyboardInterrupt handled above) must not leave the breadcrumb `running`.
        run_status.finalize_active(f"exception:{type(exc).__name__}", None)
        raise
