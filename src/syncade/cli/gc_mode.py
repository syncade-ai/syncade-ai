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
    declined = (
        f", {len(report.worktrees_declined)} {'would be ' if dry else ''}declined"
        if report.worktrees_declined
        else ""
    )
    print(
        f"[syncade] {prefix}gc: {len(report.runs_slimmed)} run(s) "
        f"{'would be ' if dry else ''}slimmed "
        f"({_human_bytes(report.bytes_freed)} of transcripts "
        f"{'would be ' if dry else ''}freed; run history kept), "
        f"{len(plan.protected_run_ids)} protected, "
        f"{len(report.worktrees_removed)} worktree(s) "
        f"{'would be ' if dry else ''}removed, "
        f"{len(report.pids_reaped)} process(es) {'would be reaped' if dry else 'reaped'}"
        f"{declined}."
    )
    if not args.quiet:
        for run_id in report.runs_slimmed:
            print(f"  {'would slim' if dry else 'slimmed'} run: {run_id}")
        for tree in report.worktrees_removed:
            print(f"  {'would remove' if dry else 'removed'} worktree: {tree}")
        for pid in report.pids_reaped:
            print(f"  {'would reap' if dry else 'reaped'} pid: {pid}")
    _report_declined(report, quiet=args.quiet)
    _report_failed(report)
    _report_refused(report)
    _report_unclaimable(plan, quiet=args.quiet)

    for err in report.errors:
        print(f"[syncade] gc: {err}", file=sys.stderr)

    return SUCCESS


def _report_declined(report, *, quiet: bool) -> None:  # noqa: ARG001
    """Name the owned workspaces GC kept because deleting them was not provably safe.

    A decline is an OUTCOME, not an absence. Left to stderr alone it is invisible under
    a headline reading ``0 worktree(s) removed`` at exit 0 — indistinguishable from a
    run that had nothing to do. Distinct from :func:`_report_unclaimable`, which is
    about workspaces this repository cannot prove it OWNS; these are provably ours.

    Per-tree paths are printed unconditionally (not gated on ``quiet``) because the
    path IS the actionable information — an operator reading stdout must see which
    workspace to inspect, even on a terminal that only shows the summary line.
    """
    if not report.worktrees_declined:
        return
    print(
        f"[syncade] gc: {len(report.worktrees_declined)} owned workspace(s) declined and "
        f"kept — syncade could not establish that they are free of live processes, or "
        f"found one it could not stop. Reasons are on stderr; if lsof is missing, install "
        f"it and rerun GC."
    )
    for tree in report.worktrees_declined:
        print(f"  declined (not provably safe to remove): {tree}")


def _report_failed(report) -> None:
    """Name the workspaces GC proved safe but could not fully delete.

    The cause lands in ``report.errors`` → stderr. The path must also reach stdout
    so an operator reading stdout can see which tree to inspect — ``0 worktrees
    removed`` with a silent partial failure is indistinguishable from a clean run.
    Paths are printed unconditionally: the path IS the actionable information.
    """
    if not report.worktrees_failed:
        return
    print(
        f"[syncade] gc: {len(report.worktrees_failed)} owned workspace(s) could not be "
        f"fully removed — the tree was proven safe but rmtree failed. Causes are on "
        f"stderr; inspect and remove the path(s) manually."
    )
    for tree in report.worktrees_failed:
        print(f"  failed to remove: {tree}")


def _report_refused(report) -> None:
    """Name workspaces skipped by a safety guard before any liveness check.

    Guard refusals (repo-root containment, identity mismatch since planning)
    fire before ``_reap_and_remove_tree`` and only land in ``report.errors``
    (stderr). Paths must also reach stdout so an operator reading the summary
    can see which workspace triggered the guard — ``0 worktrees removed`` with
    a silent guard refusal is indistinguishable from a clean run.
    Paths are printed unconditionally: the path IS the actionable information.
    """
    if not report.worktrees_refused:
        return
    print(
        f"[syncade] gc: {len(report.worktrees_refused)} workspace(s) skipped by a safety guard "
        f"(repo-root containment or identity mismatch since planning). "
        f"Reasons are on stderr; inspect the path(s) if unexpected."
    )
    for tree in report.worktrees_refused:
        print(f"  refused (safety guard): {tree}")


def _report_unclaimable(plan, *, quiet: bool) -> None:
    """Report the inert manual-cleanup/inspection set without overstating it.

    An entry is either an inspectable, recordless syncade-shaped tree or an
    unreadable tree whose name matches this repository's run artifacts. The
    former will never become ownership-proven and needs manual removal. The
    latter is not yet classifiable: after it becomes inspectable, a later GC may
    prove ownership and reclaim it. The plan intentionally carries one inert
    list for both, so the shared message names both cases and both operator
    actions instead of claiming permanence for an unreadable unknown.
    """
    count = len(plan.unclaimable_trees)
    if not count:
        return
    size = (
        "size unknown (unreadable contents)"
        if plan.unclaimable_bytes is None
        else _human_bytes(plan.unclaimable_bytes)
    )
    print(
        f"[syncade] gc: {count} workspace(s) need inspection or manual cleanup "
        f"({size}): recordless syncade-shaped trees, plus unreadable trees whose names "
        f"match repo-local run artifacts. syncade will not remove these paths on this "
        f"run. Remove recordless trees yourself; make unreadable trees inspectable and "
        f"rerun GC."
    )
    if not quiet:
        for tree in plan.unclaimable_trees:
            print(f"  not removed (recordless or unreadable known-run workspace): {tree}")


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0  # type: ignore[assignment]
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"
