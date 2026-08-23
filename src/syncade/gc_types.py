"""Shared data shapes for ``syncade --gc`` planning and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

BULK_ARTIFACT_SUFFIXES = (".stdout", ".stderr")
"""Raw subprocess transcripts under ``round-*/`` — the only artifacts GC removes.

Measured over the real 263-run corpus (2026-07-12): ``.stdout`` + ``.stderr`` are
**45.97 MB of 50.59 MB — 90.9%** — across 2138 files (1069 of each), while every
structured artifact combined (round manifests, parsed findings, summaries, exit
codes, and the whole run root) is **4.62 MB**, about 18 KB per run. So pruning
transcripts frees essentially all the disk while costing none of the data that
anything reads for a verdict or a metric.

GC used to ``rmtree`` whole run directories. That is now a data-loss bug, not a
cleanup: ``.syncade/metrics.db`` is a *derived, rebuildable view* over
``.syncade/runs/`` — it drop-and-recreates itself on schema drift — so deleting a run
directory destroys that run's history permanently the next time the view rebuilds.
Retention is therefore **four-tier**: run history (``runs/``) kept forever; transcripts
disposable beyond the configured keep window; actor workspaces removed once a run can no longer
plausibly be resumed (age-bounded by ``gc.worktree_max_age_days``, default 14 days);
and ``metrics.db`` is a droppable derived view. The asymmetry between tiers 1 and 3 is
intentional: reviewer exports and linked worktrees are reconstructible from recorded snapshot
state, so GC can safely remove them without removing history; a run directory is
not reconstructible, so it is never deleted."""


@dataclass(frozen=True)
class GcPlan:
    """The result of ``plan_gc``: what GC would do."""

    protected_run_ids: list[str]
    runs_to_slim: list[str]
    """Runs whose ``round-*/`` transcripts will be pruned. The run directory and
    every structured artifact in it SURVIVE — see :data:`BULK_ARTIFACT_SUFFIXES`."""
    worktree_trees_to_remove: list[Path]
    orphan_worktree_trees: list[Path]
    worktree_age_released: list[str] = field(default_factory=list)
    """Run-ids still resume-eligible whose WORKTREES have aged past
    ``gc.worktree_max_age_days``. Their transcripts and run directories stay protected — this
    releases tier 3 only. Carried on the plan so ``execute_gc`` can keep its TOCTOU re-check
    (a run may have become protected since planning) without that re-check silently undoing
    the age bound."""
    worktree_tree_identities: dict[Path, tuple[int, int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class GcReport:
    """The result of ``execute_gc``: what GC did, or would do on dry-run."""

    runs_slimmed: list[str]
    """Runs whose transcripts were pruned. A run already slim contributes nothing
    (slimming is idempotent) and does not appear here."""
    worktrees_removed: list[Path]
    pids_reaped: list[int]
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
