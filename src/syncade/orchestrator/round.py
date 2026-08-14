"""Per-round reviewer, synthesizer, test, and check orchestration."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from syncade import run_status
from syncade.adapters.base import ReviewerAdapter
from syncade.adapters.producer import ProducerAdapter
from syncade.config import SyncadeConfig
from syncade.diff_filter import (
    elide_binary_hunks,
    filter_diff_for_reviewer,
)
from syncade.dispatcher import DispatchResult, dispatch_reviewers
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.findings import get_findings_schema_string
from syncade.logging import Logger
from syncade.persistence import (
    SynthesizerArtifactPaths,
    TestRunArtifactPaths,
    persist_current_findings_md,
    persist_dispatch_record,
    persist_findings_md,
    persist_reviewer_result,
    persist_round_manifest,
    persist_run_summary,
    persist_synthesizer_result,
    persist_test_run_result,
)
from syncade.prompts import load_reviewer_template_for, render_reviewer_prompt
from syncade.snapshot import Snapshot
from syncade.synthesis import has_active_blocker
from syncade.synthesizer import SynthesizerResult, run_synthesizer
from syncade.test_runner import TestRunResult, run_tests
from syncade.worktree import TEST_WORKTREE_NAME, WorktreeError, WorktreeManager

from .prior_round import load_prior_reviewer_response_text
from .results import RoundArtifacts, RoundResult, TestSkipReason
from .round_checks import _run_checks_leg
from .round_predispatch import run_predispatch_gates
from .verdict import _compute_exit_code

_NO_DIFF_SENTINEL: str = "(diff not provided; review against the full repo state at HEAD)"

# Test-leg basename exported from :mod:`syncade.worktree` as a single source.
_TEST_WORKTREE_NAME = TEST_WORKTREE_NAME


def _build_reviewer_prompt(
    *,
    repo_root: Path,
    snapshot: Snapshot,
    config: SyncadeConfig,
    round_idx: int,
    run_dir: Path,
    pr_doc_path: Path,
) -> tuple[str | dict[str, str], str, bool]:
    """Load + render the reviewer prompt(s) for one round.

    Each reviewer renders its OWN provider-specific template (anthropic →
    ``reviewer_adversarial.md``, openai → ``reviewer_codex.md``, any other provider
    → the generic ``reviewer.md``), so the prompt differs per reviewer even on
    round 0 — the result is always a per-reviewer ``{name: prompt}`` dict.
    Round N>0 additionally folds in that reviewer's own prior-round response
    (per-reviewer isolation).

    Returns ``(prompt_arg, pr_doc_ref, pr_doc_is_out_of_repo)``: ``prompt_arg``
    is a per-reviewer ``{name: prompt}`` dict;
    ``pr_doc_ref`` is the worktree-LOCAL doc reference handed to reviewers;
    ``pr_doc_is_out_of_repo`` flags a doc whose copy into each worktree must
    never be skipped (see the caller's copy site).

    Raises :class:`KeyError` / :class:`ValueError` when the (operator-
    overridden) template references an unknown placeholder or a malformed
    format spec — the caller maps that to a phase-failure round, mirroring the
    producer/synth template-render contract.
    """
    # Strip context-file hunks from the reviewer-facing diff so reviewers do
    # not see edits to files absent from their worktrees. Uses the same strip
    # list as worktree creation in the caller.
    filtered_diff = filter_diff_for_reviewer(
        snapshot.diff_text, config.review.strip_repo_context_files
    )
    # Binary content never reaches the prompt (PR-h-field-01 item 2). `snapshot` renders with
    # `--text`, so a committed PNG is emitted as raw bytes — measured, 12 screenshot
    # baselines turned 66 KB of real diff into 3.1 MB. That is unreadable to a reviewer,
    # displaces the diff it should be judging, and exceeds both providers' prompt ceilings
    # (codex 1,048,576 chars; claude 1,000,000 tokens). Headers and a byte-count notice
    # survive, so the change is disclosed rather than silently dropped.
    filtered_diff, _ = elide_binary_hunks(filtered_diff)
    diff_text = filtered_diff if filtered_diff else _NO_DIFF_SENTINEL
    # Worktree-escape fix: reviewers run with cwd = their own worktree. Hand
    # them a worktree-LOCAL reference to the PR doc (relative to the worktree
    # root) — NOT the operator's absolute MAIN path. An absolute MAIN path lures
    # the reviewer OUT of its isolated, CLAUDE.md/AGENTS.md-stripped worktree to
    # review the live repo, defeating the isolation + blindness invariants
    # (empirically: run 2026-05-30T21-22-19 round 0, claude-reviewer cd'd to
    # MAIN for 25/32 bash commands + every Read). The doc is copied into each
    # worktree by the caller. Same relative ref for every reviewer (each
    # resolves it against its own cwd), so the round-0 shared prompt and the
    # round-N per-reviewer prompts both work.
    pr_doc_is_out_of_repo = False
    try:
        pr_doc_ref = str(pr_doc_path.relative_to(repo_root))
    except ValueError:
        # Out-of-repo doc: its basename may collide with a tracked file (e.g.
        # README.md). A bare-basename ref would resolve to the repo's tracked
        # file and the caller's copy would be skipped because the path already
        # exists — reviewers would silently read the WRONG spec. Render a
        # reserved, collision-free worktree-local ref under `.syncade-inputs/`
        # (hashed on the resolved source path) so the supplied doc can ALWAYS
        # be copied there without shadowing a tracked file.
        pr_doc_is_out_of_repo = True
        digest = hashlib.sha256(str(pr_doc_path).encode("utf-8")).hexdigest()[:16]
        pr_doc_ref = f".syncade-inputs/pr-doc-{digest}-{pr_doc_path.name}"
    # Each reviewer renders its own provider-specific template, so we always
    # build a per-reviewer dict — there is no shared-str fast path now that the
    # claude and codex prompts differ. Round 0 omits prior_round_output → the
    # renderer's "(no prior round)" sentinel; round N>0 passes THIS reviewer's
    # own prior-round response (per-reviewer isolation preserved).
    prior_round_dir = run_dir / f"round-{round_idx - 1}" if round_idx > 0 else None
    per_reviewer_prompts: dict[str, str] = {}
    for reviewer in config.reviewers:
        template = load_reviewer_template_for(
            repo_root, provider=reviewer.provider, template=reviewer.template
        )
        render_kwargs: dict[str, object] = {
            "pr_doc_path": pr_doc_ref,
            "diff": diff_text,
            "master_plan_path": None,
            "json_schema": get_findings_schema_string(),
            "adversarial_lens": reviewer.adversarial_lens,
            "bug_class_sweep": reviewer.bug_class_sweep,
        }
        if prior_round_dir is not None:
            render_kwargs["prior_round_output"] = load_prior_reviewer_response_text(
                prior_round_dir=prior_round_dir,
                reviewer_name=reviewer.name,
                reviewer_provider=reviewer.provider,
            )
        per_reviewer_prompts[reviewer.name] = render_reviewer_prompt(template, **render_kwargs)
    prompt_arg: str | dict[str, str] = per_reviewer_prompts
    return prompt_arg, pr_doc_ref, pr_doc_is_out_of_repo


def _run_one_round(
    *,
    round_idx: int,
    snapshot: Snapshot,
    repo_root: Path,
    pr_doc_path: Path,
    run_id: str,
    run_dir: Path,
    round_dir: Path,
    config: SyncadeConfig,
    resolved_timeout: float,
    resolved_producer_timeout: float,
    adapter_factory: Callable[[str], ReviewerAdapter] | None,
    synthesizer_adapter: ReviewerAdapter | None,
    producer_adapter: ProducerAdapter | None,
    worktree_base: Path,
    logger: Logger,
    started_at: datetime,
    managers_to_cleanup: list[WorktreeManager],
    resumed_under_drift: bool = False,
) -> RoundResult:
    """Execute one round of the per-round pipeline.

    The round handles reviewer worktree provisioning, dispatch, synthesis,
    optional test rerun, checks, and persistence. Producer and branch-advance
    happen after this returns.

    Returns the round's :class:`RoundResult` regardless of
    outcome. The caller decides whether to terminate the loop or
    dispatch a producer based on ``round_exit_code``.

    """
    _gate = run_predispatch_gates(
        repo_root=repo_root,
        snapshot=snapshot,
        config=config,
        round_idx=round_idx,
        run_dir=run_dir,
        round_dir=round_dir,
        pr_doc_path=pr_doc_path,
        started_at=started_at,
        resumed_under_drift=resumed_under_drift,
        logger=logger,
        build_prompt=_build_reviewer_prompt,
    )
    if isinstance(_gate, RoundResult):
        return _gate
    prompt_arg = _gate.prompt_arg
    pr_doc_ref = _gate.pr_doc_ref
    pr_doc_is_out_of_repo = _gate.pr_doc_is_out_of_repo
    _filtered_for_check = _gate.filtered_for_check

    # --- Worktrees + dispatch ---------------------------------------
    # Scope the WorktreeManager at <run-id>/round-N/ so each round's
    # worktrees are isolated.
    #
    # When the test-leg worktree fails to provision, preserve reviewer
    # worktrees on disk for inspection. The ``with`` block's ``__exit__`` runs
    # ``cleanup_all`` only on clean exit; raising inside the block
    # skips it. We raise the test_worktree_error inside the block
    # to trigger preservation, then catch it here so
    # ``_run_one_round`` returns a :class:`RoundResult` rather than
    # bubbling the exception up to the loop (which would terminate
    # the loop without recording the round's artifacts).
    round_worktree_id = f"{run_id}/round-{round_idx}"
    # Forward-declare round-result variables so they're bound in
    # ``_run_one_round``'s scope across the with block (the
    # test_worktree_error raise-then-catch flow needs to read them
    # after the with block unwinds).
    dispatch_result: DispatchResult | None = None
    synth_result: SynthesizerResult | None = None
    synth_paths: SynthesizerArtifactPaths | None = None
    findings_md_path: Path | None = None
    test_result: TestRunResult | None = None
    test_skip_reason: TestSkipReason | None = None
    test_worktree_error: WorktreeError | None = None
    test_run_paths: TestRunArtifactPaths | None = None
    check_results: list[TestRunResult] = []
    round_exit_code: int = SUCCESS

    # Defer cleanup so the loop terminator decides cleanup-vs-preserve from
    # the final exit code.
    try:
        round_manager = WorktreeManager(
            repo_root,
            round_worktree_id,
            base_dir=worktree_base,
            defer_cleanup=True,
        )
        managers_to_cleanup.append(round_manager)
        with round_manager as manager:
            logger.event(
                f"provisioning {len(config.reviewers)} worktree(s) under {manager.run_dir}"
            )
            worktree_paths: dict[str, Path] = {}
            for reviewer in config.reviewers:
                worktree = manager.create(
                    reviewer_name=reviewer.name,
                    commit_sha=snapshot.commit_sha,
                    strip_files=config.review.strip_repo_context_files,
                )
                worktree_paths[reviewer.name] = worktree.path
                # Ensure the PR doc is readable from INSIDE the worktree so the
                # worktree-local `pr_doc_ref` resolves without the reviewer
                # leaving its sandbox. An out-of-repo doc ALWAYS copies to its
                # reserved collision-free ref (never skip — a same-basename
                # tracked file, e.g. README.md, must not shadow the supplied
                # doc and feed reviewers the WRONG spec). A tracked in-repo doc
                # is already present (no-op); an untracked in-repo doc copies in.
                pr_doc_in_worktree = worktree.path / pr_doc_ref
                if pr_doc_is_out_of_repo or not pr_doc_in_worktree.exists():
                    import shutil

                    pr_doc_in_worktree.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(pr_doc_path, pr_doc_in_worktree)
                logger.event(f"  worktree ready: {reviewer.name} -> {worktree.path}")

            # Report the timeout honestly: "each" only holds when every reviewer uses the fallback.
            # A per-reviewer ``timeout_seconds`` override makes "1800s each" a lie (dogfood R2-M4).
            if any(r.timeout_seconds is not None for r in config.reviewers):
                timeout_note = f"per-reviewer timeouts, {resolved_timeout:g}s fallback"
            else:
                timeout_note = f"timeout {resolved_timeout:g}s each"
            logger.event(
                f"dispatching {len(config.reviewers)} reviewer(s) in parallel ({timeout_note})"
            )
            for reviewer in config.reviewers:
                logger.event(f"  -> {reviewer.name} ({reviewer.provider}) started")

            # Written BEFORE the panel runs: every other artifact lands after it returns, so a
            # death mid-dispatch left an empty round dir and nothing to diagnose from.
            persist_dispatch_record(
                round_dir,
                round_index=round_idx,
                reviewers=config.reviewers,
                timeout_seconds=resolved_timeout,
            )
            dispatch_result = dispatch_reviewers(
                config.reviewers,
                worktree_paths=worktree_paths,
                prompt=prompt_arg,
                timeout_seconds=resolved_timeout,
                adapter_factory=adapter_factory,
                pricing=config.pricing,
                max_retries=config.retry.max_retries,
            )

            for run_result in dispatch_result.results:
                prefix = (
                    f"  <- {run_result.reviewer_name} ({run_result.provider}) "
                    f"finished in {run_result.duration_seconds:.1f}s — "
                )
                if run_result.output is None:
                    err_cls = type(run_result.error).__name__ if run_result.error else "Unknown"
                    logger.event(f"{prefix}FAILED ({err_cls})", error=True)
                else:
                    logger.event(
                        f"{prefix}{run_result.output.verdict}, "
                        f"{len(run_result.output.findings)} finding(s)"
                    )

            logger.event(f"persisting reviewer outputs to {round_dir}")
            for run_result in dispatch_result.results:
                persist_reviewer_result(
                    round_dir,
                    run_result,
                    run_result.raw_subprocess_result,
                )

            # --- Synthesizer phase --------------------------------------
            # PR-v2-11: the budget is deliberately NOT re-checked between the reviewer panel and
            # the judge. The judge is part of the atomic "review-bundle" — a cheap (~$0.05)
            # deterministic consolidation whose output IS this round's findings.md. Gating it out
            # on a reviewer-panel budget crossing would leave the crossing round with no
            # consolidated verdict, breaking the "round is whole" guarantee (C2). The budget's
            # two enforced boundaries (before-round in loop.py, before-producer in
            # loop_round_step.py) skip the EXPENSIVE producer instead; overshoot is bounded to
            # one review-bundle or one producer, never a running call. See the PR-v2-11 brief.
            if dispatch_result.all_succeeded:
                run_status.update_phase(f"round-{round_idx}: synthesizing", round_idx)
                logger.event(f"synthesizing reviewer outputs (cold {config.synthesizer.provider})")
                synth_result = run_synthesizer(
                    dispatch_result.results,
                    repo_root=repo_root,
                    pr_doc_path=pr_doc_path,
                    timeout_seconds=resolved_timeout,
                    config=config.synthesizer,
                    adapter=synthesizer_adapter,
                    pricing=config.pricing,
                    max_retries=config.retry.max_retries,
                )
                prefix = f"  synthesizer finished in {synth_result.duration_seconds:.1f}s — "
                if synth_result.output is None:
                    err_cls = type(synth_result.error).__name__ if synth_result.error else "Unknown"
                    logger.event(f"{prefix}FAILED ({err_cls})", error=True)
                else:
                    findings = len(synth_result.output.consolidated_findings)
                    dismissed = sum(
                        1 for f in synth_result.output.consolidated_findings if f.dismissed
                    )
                    active = findings - dismissed
                    logger.event(
                        f"{prefix}{active} active finding(s), {dismissed} dismissed "
                        f"(of {findings} consolidated)"
                    )
            else:
                logger.event("  synthesizer skipped (one or more reviewers failed)")

            if synth_result is not None:
                synth_paths = persist_synthesizer_result(round_dir, synth_result)

            # --- Test re-run leg ---------------------------------------
            if (
                dispatch_result.all_succeeded
                and synth_result is not None
                and synth_result.output is not None
                and not has_active_blocker(synth_result.output)
                and config.loop.test_command is not None
            ):
                test_timeout = (
                    config.loop.test_timeout_seconds
                    if config.loop.test_timeout_seconds is not None
                    else resolved_timeout
                )
                try:
                    test_worktree = manager.create(
                        reviewer_name=_TEST_WORKTREE_NAME,
                        commit_sha=snapshot.commit_sha,
                        strip_files=config.review.strip_repo_context_files,
                    )
                except WorktreeError as exc:
                    test_worktree_error = exc
                    test_skip_reason = "test_worktree_error"
                    logger.event(
                        f"  test re-run skipped (test worktree provisioning failed: {exc})"
                    )
                else:
                    run_status.update_phase(f"round-{round_idx}: testing", round_idx)
                    logger.event(f"  worktree ready: {_TEST_WORKTREE_NAME} -> {test_worktree.path}")
                    logger.event(
                        f"running test re-run leg: {config.loop.test_command!r} "
                        f"(timeout {test_timeout:g}s)"
                    )
                    test_result = run_tests(
                        worktree_path=test_worktree.path,
                        test_command=config.loop.test_command,
                        timeout_seconds=test_timeout,
                    )
                    prefix = f"  test re-run finished in {test_result.duration_seconds:.1f}s — "
                    if test_result.outcome == "subprocess_error":
                        err_cls = (
                            type(test_result.error).__name__ if test_result.error else "Unknown"
                        )
                        logger.event(f"{prefix}SUBPROCESS_ERROR ({err_cls})", error=True)
                    else:
                        logger.event(
                            f"{prefix}{test_result.outcome.upper()} (exit {test_result.exit_code})"
                        )
            elif not dispatch_result.all_succeeded:
                test_skip_reason = "reviewer_failed"
                logger.event("  test re-run skipped (a reviewer failed in dispatch)")
            elif synth_result is None or synth_result.error is not None:
                test_skip_reason = "synth_failed"
                logger.event("  test re-run skipped (the synthesizer phase failed)")
            elif (
                synth_result is not None
                and synth_result.output is not None
                and has_active_blocker(synth_result.output)
            ):
                test_skip_reason = "synth_blocker"
                logger.event(
                    "  test re-run skipped (the synthesizer surfaced an active blocker "
                    "with a NO-SHIP verdict)"
                )
            else:
                test_skip_reason = "test_command_unset"
                logger.event(
                    "  test re-run skipped (test_command is not configured in .syncade/config.toml)"
                )

            if test_result is not None:
                test_run_paths = persist_test_run_result(round_dir, test_result)

            # --- Mechanical checks leg ---------------------------------
            run_status.update_phase(f"round-{round_idx}: checking", round_idx)
            check_results = _run_checks_leg(
                manager=manager,
                config=config,
                snapshot=snapshot,
                dispatch_result=dispatch_result,
                synth_result=synth_result,
                resolved_timeout=resolved_timeout,
                round_dir=round_dir,
                logger=logger,
            )

            # --- Compute round verdict ----------------------------------
            if test_worktree_error is not None:
                round_exit_code = WORKTREE_ERROR
            else:
                # Only blocking check results reach the verdict; advisory
                # results are structurally never passed in, so they cannot gate.
                round_exit_code = _compute_exit_code(
                    dispatch_result,
                    synth_result,
                    test_result,
                    blocking_check_results=[c for c in check_results if c.severity == "blocking"],
                )

            # --- Persistence block --------------------------------------
            try:
                if synth_result is not None and synth_result.output is not None:
                    findings_md_path = persist_findings_md(
                        round_dir,
                        synth_result,
                        started_at,
                        test_result,
                        dispatch_result,
                        test_skip_reason=test_skip_reason,
                        snapshot_sha=snapshot.commit_sha,
                        check_results=check_results,
                    )
                    # Refresh the run-root findings.md convenience copy so the
                    # operator can address latest findings without knowing the
                    # round number. Best-effort: a copy failure
                    # doesn't block the per-round persistence —
                    # the round-N/findings.md is the source of
                    # truth and lives on disk regardless.
                    persist_current_findings_md(run_dir, findings_md_path)

                persist_round_manifest(
                    round_dir,
                    snapshot,
                    dispatch_result,
                    round_exit_code,
                    started_at,
                    synth_result,
                    test_result,
                    test_skip_reason,
                    round_idx=round_idx,
                    producer_result=None,
                    producer_provider=config.producer.provider,
                    producer_model=config.producer.model,
                    check_results=check_results,
                    filtered_diff_bytes=len(_filtered_for_check.encode("utf-8")),
                    raw_diff_bytes=len((snapshot.diff_text or "").encode("utf-8")),
                )
                persist_run_summary(
                    round_dir,
                    snapshot,
                    dispatch_result,
                    round_exit_code,
                    started_at,
                    synth_result,
                    test_result,
                    test_skip_reason,
                    resumed_under_drift=resumed_under_drift,
                    check_results=check_results,
                )
            except Exception as persist_exc:
                if test_worktree_error is not None:
                    raise persist_exc from test_worktree_error
                raise

            # Raise the captured test_worktree_error inside the with block so
            # __exit__ skips cleanup and reviewer worktrees survive on disk.
            if test_worktree_error is not None:
                raise test_worktree_error
    except WorktreeError:
        # Re-enter here only when the captured ``test_worktree_error`` was
        # raised inside the with block to trigger worktree preservation.
        # Distinguish this case from a *different* WorktreeError (e.g. reviewer-name
        # duplicate during reviewer-worktree provisioning, which
        # fires BEFORE we ever set test_worktree_error). The
        # pre-dispatch WorktreeError path must propagate to the
        # CLI which maps it to exit 60 with the original message;
        # falling through to RoundResult assembly here would lose
        # the exception AND populate the result with a None
        # dispatch_result that downstream rendering (logger.summary,
        # persist_*) can't handle.
        if test_worktree_error is None:
            raise
        # Otherwise: the captured test_worktree_error was the cause.
        # Fall through to RoundResult assembly. The variables
        # (dispatch_result, synth_result, etc.) are bound in
        # _run_one_round's scope, so they remain accessible after
        # the with block unwinds. The orchestrator treats
        # ``round_exit_code == WORKTREE_ERROR`` + non-None
        # ``test_worktree_error`` as the terminator for this round.

    # --- Assemble round artifacts + result ------------------------
    artifacts = RoundArtifacts(
        round_idx=round_idx,
        round_dir=round_dir,
        manifest_path=round_dir / "manifest.json",
        summary_path=round_dir / "summary.md",
        findings_md_path=findings_md_path,
        synthesizer_paths=synth_paths,
        test_run_paths=test_run_paths,
        producer_paths=None,  # filled in by run_review after producer phase
    )
    return RoundResult(
        round_idx=round_idx,
        snapshot=snapshot,
        dispatch_result=dispatch_result,
        synth_result=synth_result,
        test_result=test_result,
        test_skip_reason=test_skip_reason,
        test_worktree_error=test_worktree_error,
        producer_result=None,
        round_exit_code=round_exit_code,
        artifacts=artifacts,
        check_results=check_results,
        filtered_diff_bytes=len(_filtered_for_check.encode("utf-8")),
        raw_diff_bytes=len((snapshot.diff_text or "").encode("utf-8")),
    )
