"""``syncade --gc`` mode handler.

A one-shot maintenance pass that prunes bulk transcripts from ``.syncade/runs/``
and removes ``/tmp/syncade/`` worktree leftovers, then safely reaps orphaned
subprocesses (cwd-scoped). Run history is never deleted. Split into its own
module so :mod:`syncade.cli.modes` stays under the blocking file-length cap;
re-exported from ``modes`` so the public import path is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

from syncade.exit_codes import SUCCESS, WORKTREE_ERROR
from syncade.snapshot import SnapshotError, discover_repo_root


def _run_gc(args) -> int:
    """Dispatch ``syncade --gc``: a one-shot maintenance pass that
    prunes bulk transcripts from ``.syncade/runs/`` and removes
    ``/tmp/syncade/`` worktree leftovers, then safely reaps orphaned
    subprocesses (cwd-scoped). Run history is never deleted.

    Thin wrapper around :func:`syncade.gc.plan_gc` → :func:`syncade.gc.execute_gc`:
    resolve the git repo root, build a plan, execute it (honoring
    ``--gc-dry-run``), and print a human-readable report. ``--gc`` operates
    only on its own artifacts and is best-effort over them — per-item failures
    are reported but never fail the pass.

    Exit codes follow the CLI mode-handler convention (CLAUDE.md "Exit-code
    convention for CLI mode handlers"): success → 0; repo-discovery failure →
    60 (WORKTREE_ERROR). GC never raises out and partial per-item failures
    still exit 0.
    """
    # Local import keeps the gc module out of the default-import cost —
    # operators not running --gc don't pay for it.
    from syncade.cli.config_overrides import apply_worktree_base_override
    from syncade.config_loader import ConfigError, load_config
    from syncade.exit_codes import CONFIG_ERROR
    from syncade.gc import execute_gc, plan_gc

    repo_root_hint = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    try:
        repo_root = discover_repo_root(repo_root_hint)
    except SnapshotError as exc:
        print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    # A malformed [gc] value is a config ERROR, not a per-item GC failure: it fails exit 50 like
    # every other config-consuming path (review, doctor), so a typo cannot silently run GC with the
    # wrong retention. (--gc's never-raise contract is about per-item cleanup failures, not
    # config validation.)
    try:
        # check_api_keys=False: --gc spawns no model actors, so a missing actor API key must not
        # block maintenance — but a malformed [gc] value still fails schema validation → exit 50.
        config = load_config(repo_root, preset=args.preset, check_api_keys=False)
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR
    config = apply_worktree_base_override(config, args)  # --worktree-base cleans a custom base too

    # Retention precedence: CLI --gc-keep/--gc-max-age-days → [gc] config → default (GcConfig's
    # defaults reproduce gc.DEFAULT_KEEP / DEFAULT_MAX_AGE_DAYS). Flag defaults are None so "passed
    # without a value" stays detectable in main()'s mutex.
    keep = args.gc_keep if args.gc_keep is not None else config.gc.keep
    max_age_days = (
        args.gc_max_age_days if args.gc_max_age_days is not None else config.gc.max_age_days
    )
    # worktree_base: clean the base the runs actually use (config.worktree_base / --worktree-base),
    # not the hardcoded default — else a relocated base leaves worktree leftovers uncollected.
    plan = plan_gc(
        repo_root,
        keep=keep,
        max_age_days=max_age_days,
        # Tier 3 has no CLI flag on purpose: it is a machine-level retention preference set once,
        # not a per-invocation choice, and `--config set gc.worktree_max_age_days N` already
        # reaches it. Another flag would need parser, validation, docs and two drift-test entries
        # for a knob nobody tunes twice.
        worktree_max_age_days=config.gc.worktree_max_age_days,
        worktree_base=config.worktree_base,
    )
    report = execute_gc(plan, dry_run=args.gc_dry_run, repo_root=repo_root)

    dry = report.dry_run
    prefix = "(dry run — nothing removed) " if dry else ""
    print(
        f"[syncade] {prefix}gc: {len(report.runs_slimmed)} run(s) "
        f"{'would be ' if dry else ''}slimmed "
        f"({_human_bytes(report.bytes_freed)} of transcripts "
        f"{'would be ' if dry else ''}freed; run history kept), "
        f"{len(plan.protected_run_ids)} protected, "
        f"{len(report.worktrees_removed)} worktree(s) "
        f"{'would be ' if dry else ''}removed, "
        f"{len(report.pids_reaped)} process(es) {'would be reaped' if dry else 'reaped'}."
    )
    if not args.quiet:
        for run_id in report.runs_slimmed:
            print(f"  {'would slim' if dry else 'slimmed'} run: {run_id}")
        for tree in report.worktrees_removed:
            print(f"  {'would remove' if dry else 'removed'} worktree: {tree}")
        for pid in report.pids_reaped:
            print(f"  {'would reap' if dry else 'reaped'} pid: {pid}")
    for err in report.errors:
        print(f"[syncade] gc: {err}", file=sys.stderr)

    return SUCCESS


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0  # type: ignore[assignment]
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"
