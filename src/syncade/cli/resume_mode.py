"""The ``syncade --resume <run-id>`` handler.

Lives in its own module to keep :mod:`syncade.cli.modes` under the file-length
cap: ``--resume`` is the largest one-shot handler (it rebuilds a ResumePlan from
on-disk artifacts and re-enters the full review loop), and piling it alongside
the other mode dispatchers is exactly what makes an entry-point policy easy to
miss. ``modes`` re-exports :func:`_run_resume` so ``main()``'s dispatch is
unchanged.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from syncade.cli.auth_gate import auth_gate
from syncade.cli.config_overrides import apply_worktree_base_override
from syncade.config_auth import REVIEW_BLOCKS
from syncade.config_loader import ConfigError, load_config
from syncade.exit_codes import CONFIG_ERROR, WORKTREE_ERROR
from syncade.logging import Logger
from syncade.snapshot import SnapshotError
from syncade.worktree import WorktreeError


def _original_budget(config_snapshot_path) -> dict:
    """The ORIGINAL run's configured budget, read from its run-init.json config snapshot.

    Returns ``{'budget_tokens': ..., 'budget_usd': ...}`` with ONLY the dimensions the original
    run actually set (a CLI ``--budget-*`` was folded into that snapshot at launch). ``{}`` on a
    missing path, any read/parse failure, or a malformed value — budget inheritance is best-effort
    and must never block a resume or bypass LoopConfig validators."""
    if config_snapshot_path is None:
        return {}
    try:
        data = json.loads(Path(config_snapshot_path).read_text(encoding="utf-8"))
        loop = data.get("config_snapshot", {}).get("loop", {}) or {}
        result = {}
        for k in ("budget_tokens", "budget_usd"):
            v = loop.get(k)
            if v is None:
                continue
            if isinstance(v, bool):
                continue
            # budget_tokens requires a plain int (LoopConfig._strict_budget_tokens rejects
            # floats — including 0.0, NaN, and Infinity — so inheriting them via model_copy
            # would bypass the schema validator). budget_usd allows int/float but must be
            # finite and non-negative (LoopConfig._budget_usd_isfinite rejects NaN/Inf).
            if k == "budget_tokens":
                if not isinstance(v, int) or v < 0:
                    continue
            else:
                if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                    continue
            result[k] = v
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        # AttributeError covers a malformed config_snapshot/loop that isn't a dict.
        return {}


def _resolve_resume_budget(config, args, plan):
    """Resolve the resumed run's budget and announce it (PR-v2-11).

    Precedence PER DIMENSION: CLI ``--budget-*`` > current ``config.toml`` > the ORIGINAL run's
    ceiling > none. Inheriting the original ceiling is Finding 2's fix: a run launched with a
    CLI-only ``--budget-tokens`` must NOT silently resume with no guardrail just because resume
    reloads config from disk. Only the CEILING is inherited — the TALLY is still fresh (bounds
    only the resumed spend). The notice fires even under ``--quiet`` (a spend-authorization fact,
    like the resolved auth mode) so the operator is never surprised by the resumed budget."""
    cli = {
        field: value
        for field, value in (
            ("budget_tokens", args.budget_tokens),
            ("budget_usd", args.budget_usd),
        )
        if value is not None
    }
    if cli:
        config = config.model_copy(update={"loop": config.loop.model_copy(update=cli)})

    # Inherit when the reloaded config did not EXPLICITLY set this dimension. Testing for
    # None was equivalent until budget_tokens gained a default (PR-h-field-06) — after which
    # it is never None, so inheritance silently stopped firing and a run launched at 3,500
    # tokens resumed at the 50,000,000 default. Not unguarded, but guarded 14,000x looser,
    # which is the same surprise Finding 2 exists to prevent. `model_fields_set` is the
    # distinction the layered loader preserves: an omitted key is absent from it, an
    # explicitly-configured one is present, whatever its value.
    original = _original_budget(plan.config_snapshot_path)
    inherited = {
        dim: original[dim]
        for dim in ("budget_tokens", "budget_usd")
        if dim not in config.loop.model_fields_set and original.get(dim) is not None
    }
    if inherited:
        config = config.model_copy(update={"loop": config.loop.model_copy(update=inherited)})

    if config.loop.budget_tokens or config.loop.budget_usd:  # 0 sentinels = no active ceiling
        msg = (
            "[syncade] note: --resume applies a FRESH budget to this run's spend; the original "
            "run's tokens/cost are NOT carried over, so original + resume can exceed a single "
            "budget ceiling (resuming re-authorizes the spend)."
        )
        if inherited:
            names = ", ".join(f"{k}={v}" for k, v in inherited.items())
            msg += (
                f" No budget was set for this resume, so the original run's ceiling was "
                f"inherited ({names}); pass --budget-tokens/--budget-usd to override."
            )
        print(msg, file=sys.stderr)
    return config


def _report_hard_kill(run_dir: Path, run_status) -> None:
    """Say so when the run being resumed was HARD-KILLED (PR-h-field-02).

    ``run_status.is_stale_running`` has existed since the breadcrumb landed and, until now,
    nothing in the product called it — a ``running`` state against a dead pid was detectable and
    never reported. Three field runs died exactly that way, and the operator's only clue was a
    status file still claiming the run was in progress.

    Nothing in-process can finalize that file after SIGKILL, by definition. What CAN be fixed is
    the silence: resume is where someone goes after a run vanishes, so resume is where it should
    be told what it is looking at.
    """
    try:
        status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not run_status.is_stale_running(status):
        return
    phase = status.get("phase") or "an unrecorded phase"
    print(
        f"[syncade] the run you are resuming was HARD-KILLED during '{phase}' — its status file "
        f"still reads 'running' because nothing in the process survived to finalize it "
        f"(SIGKILL, OOM, or the machine going down). Completed rounds are intact and will be "
        f"reused; the interrupted round is dropped and retried.",
        file=sys.stderr,
    )


def _run_resume(args) -> int:
    """Dispatch ``syncade --resume <run-id>``.

    Resolves the target run-id (specific or 'latest'), builds a
    :class:`ResumePlan` from on-disk artifacts, and invokes
    :func:`run_review` with the resume_plan threaded in. Maps the
    resume-specific exceptions to exit codes:

    - Config load failure → 50 (CONFIG_ERROR).
    - repo_root discovery failure → 60 (WORKTREE_ERROR).
    - :class:`ResumeError` (run-id not found / not eligible /
      malformed) → 60.
    - :class:`TreeDriftError` (drift without --force-drift) → 60.
    - Any other orchestrator exception bubbles up to ``main()``
      where it gets the same mapping as the fresh-run path.

    The actual loop runs through the existing
    :func:`syncade.orchestrator.run_review` with
    ``resume_plan=plan, force_drift=args.force_drift``; its
    exit code is the resumed run's verdict.
    """
    # Import the resume helpers lazily — operators not running
    # --resume don't need to pay the import cost.
    from syncade import run_status
    from syncade.orchestrator import run_review
    from syncade.orchestrator.resume import (
        ResumeError,
        plan_resume,
        read_resume_decision,
        resolve_resume_target,
    )
    from syncade.snapshot import discover_repo_root

    # Discover the repo root for runs_root resolution and for any
    # downstream code that wants to know "what tree are we in".
    repo_root_hint = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    try:
        repo_root = discover_repo_root(repo_root_hint)
    except SnapshotError as exc:
        print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    def _emit_deprecation(message: str) -> None:
        print(f"[syncade] {message}", file=sys.stderr)

    try:
        config = load_config(repo_root, preset=args.preset, deprecation_callback=_emit_deprecation)
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    # --max-rounds overrides the original run's cap (same model_copy as the fresh handler).
    if args.max_rounds is not None:
        new_loop = config.loop.model_copy(update={"max_rounds": args.max_rounds})
        config = config.model_copy(update={"loop": new_loop})

    runs_root = repo_root / ".syncade" / "runs"

    # Read the current branch to filter "latest" by branch (detached HEAD → None).
    current_branch: str | None = None
    try:
        from syncade.orchestrator.resume import _current_head_branch

        current_branch = _current_head_branch(repo_root)
    except Exception:
        # Best-effort; a git failure falls back to no branch filter (drift check catches it).
        current_branch = None

    # Resolve the resume target + plan BEFORE the guard and auth_gate: the guard needs the
    # EFFECTIVE cap the resumed run will use (max(config, plan.max_rounds)), and a resume
    # error should surface without spawning a provider auth probe.
    try:
        run_id = resolve_resume_target(runs_root, args.resume, current_branch)
        plan = plan_resume(
            repo_root,
            runs_root / run_id,
            max_rounds_override=config.loop.max_rounds if args.max_rounds is not None else None,
        )
        # If the resumed round escalated, read the operator's decision (refuses with
        # ResumeError when none was recorded). Read BEFORE run_review drops the round dir.
        operator_decision = read_resume_decision(runs_root / run_id, plan.resumed_round)
        _report_hard_kill(runs_root / run_id, run_status)
    except ResumeError as exc:
        print(f"[syncade] resume error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    config = _resolve_resume_budget(config, args, plan)
    # Honor the configured/overridden worktree base on resume too (dogfood B2): without this a
    # resumed run silently falls back to /tmp/syncade even when the run relocated its worktrees.
    # Its own try rather than the load_config one above: the plan load sits between them, so
    # this cannot be hoisted. OverrideError is a ConfigError, hence exit 50 and no traceback.
    try:
        config = apply_worktree_base_override(config, args)
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    # Default-branch refusal (PR-v2-26, D1(c)). will_commit uses the EFFECTIVE cap
    # run_review will use — a single-pass resume (effective == 1) commits nothing and is
    # exempt; a multi-round resume with a base defers to run_review (auth probe first);
    # only a baseless multi-round resume is refused here before auth. Deriving it from
    # the current config/CLI max_rounds alone would be wrong: a run launched at max_rounds=3
    # but resumed with the config drifted to 1 still commits (effective == 3).
    from syncade.orchestrator.branch_guard import current_branch_name, guard_default_branch

    effective_max_rounds = max(config.loop.max_rounds, plan.max_rounds)
    # Propagate the effective cap into config so auth_gate sees it. Without this,
    # auth_gate._announce_unconfined_producer checks config.loop.max_rounds <= 1 and
    # suppresses the yolo notice when the raw reloaded config drifted to 1, even though
    # the resumed run will use effective_max_rounds (which may be > 1) and CAN dispatch
    # a producer. loop_resume.py applies the same bump for the same reason; doing it here
    # too means auth_gate and run_review both receive the same effective value.
    if effective_max_rounds != config.loop.max_rounds:
        config = config.model_copy(
            update={"loop": config.loop.model_copy(update={"max_rounds": effective_max_rounds})}
        )
    # Refuse ONLY when we can PROVE the resumed run commits (D1(c), PR-h-02d.5) — the same
    # inversion as the fresh path. Literal `base_oid == HEAD` equality was too narrow: a
    # resumed run whose pinned base is NOT HEAD but whose diff filters empty takes the
    # no_changes_to_review path, and refusing it before auth diverged from run_review.
    #
    # `two_dot=True` is correct and NOT the merge-base hazard the previous comment warned
    # about: plan.base_oid is the ALREADY-RESOLVED effective diff base, and a resumed run
    # re-snapshots with three_dot=False against it (loop.py). So the literal `base..HEAD`
    # range is exactly what run_review will diff — re-deriving a merge base here is what
    # would be wrong.
    from .resolve import _cli_proves_commit

    _resume_will_commit = effective_max_rounds > 1 and _cli_proves_commit(
        repo_root, plan.base_oid, config.review.strip_repo_context_files, two_dot=True
    )
    try:
        guard_default_branch(
            repo_root,
            current_branch_name(repo_root),
            allow=args.allow_default_branch,
            will_commit=_resume_will_commit,
        )
    except WorktreeError as exc:
        print(f"[syncade] worktree error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    gate = auth_gate(config, REVIEW_BLOCKS)
    if gate is not None:
        return gate

    logger = Logger("quiet" if args.quiet else "normal")
    if not args.quiet:
        print(
            f"[syncade] resuming run {plan.run_id} at round "
            f"{plan.resumed_round} (completed: {plan.completed_rounds}; "
            f"expected snapshot SHA: {plan.expected_sha[:12]})"
        )

    with run_status.install_signal_handlers():
        try:
            result = run_review(
                repo_root=repo_root,
                pr_doc_path=plan.pr_doc_path,
                config=config,
                base_ref=plan.base_oid if plan.base_oid is not None else plan.base_ref,
                timeout_seconds=args.timeout,
                logger=logger,
                force_dirty=args.force_dirty,
                allow_default_branch=args.allow_default_branch,
                resume_plan=plan,
                force_drift=args.force_drift,
                operator_decision=operator_decision,
                worktree_base=config.worktree_base,
            )
        except FileNotFoundError as exc:
            print(f"[syncade] error: {exc}", file=sys.stderr)
            return 2
        except NotADirectoryError as exc:
            print(f"[syncade] error: {exc}", file=sys.stderr)
            return 2
        except SnapshotError as exc:
            print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
            # These typed handlers intercept before the catch-all, so each finalizes
            # the breadcrumb itself — else a clean exit-60 leaves status.json `running`
            # and reads as a hard kill.
            run_status.finalize_active(f"exception:{type(exc).__name__}", WORKTREE_ERROR)
            return WORKTREE_ERROR
        except ResumeError as exc:
            print(f"[syncade] resume error: {exc}", file=sys.stderr)
            run_status.finalize_active(f"exception:{type(exc).__name__}", WORKTREE_ERROR)
            return WORKTREE_ERROR
        except WorktreeError as exc:
            print(f"[syncade] worktree error: {exc}", file=sys.stderr)
            run_status.finalize_active(f"exception:{type(exc).__name__}", WORKTREE_ERROR)
            return WORKTREE_ERROR
        except KeyboardInterrupt:
            return run_status.finalize_signal()
        except Exception as exc:
            run_status.finalize_active(f"exception:{type(exc).__name__}", None)
            raise
    return result.exit_code
