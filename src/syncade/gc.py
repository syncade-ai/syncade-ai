"""Run-bloat GC planning plus the public ``syncade.gc`` API.

``syncade --gc`` prunes bulk transcripts from ``.syncade/runs/<run-id>/`` and
removes identity-checked ``/tmp/syncade/<run-id>/`` worktree leftovers, and safely
reaps orphaned reviewer/producer subprocesses left behind by an abnormal parent exit.
Run history (structured artifacts) is never deleted.

Planning stays here because it is pure and auditable. Destructive execution and
process reaping live in :mod:`syncade.gc_execute`; shared protection checks live
in :mod:`syncade.gc_protection`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from syncade.gc_execute import (
    _git_worktree_prune,
    _lsof_pids_in_tree,
    _parse_lsof_pids,
    _pid_cwd_is_still_in_tree,
    _reap_and_remove_tree,
    _reap_processes_in_tree,
    execute_gc,
)
from syncade.gc_protection import (
    current_protected_run_ids as _current_protected_run_ids,
)
from syncade.gc_protection import (
    gc_should_conservatively_protect as _gc_should_conservatively_protect,
)
from syncade.gc_protection import (
    orphan_worktree_still_orphan_now as _orphan_worktree_still_orphan_now,
)
from syncade.gc_protection import (
    protected_run_ids_for_gc as _protected_run_ids_for_gc,
)
from syncade.gc_protection import run_dir_protected_now as _run_dir_protected_now
from syncade.gc_protection import run_dir_slimmable_now as _run_dir_slimmable_now
from syncade.gc_protection import run_id_protected_now as _run_id_protected_now
from syncade.gc_protection import safe_iter_subdirs as _safe_iter_subdirs
from syncade.gc_types import GcPlan, GcReport
from syncade.gc_worktrees import (
    existing_worktree_trees,
    repo_owned_orphan_trees,
    tree_contains_repo_root,
    tree_identity,
)
from syncade.persistence import RUN_INIT_FILENAME
from syncade.worktree import DEFAULT_WORKTREE_BASE

__all__ = [
    "DEFAULT_KEEP",
    "DEFAULT_MAX_AGE_DAYS",
    "GcPlan",
    "GcReport",
    "_current_protected_run_ids",
    "_gc_should_conservatively_protect",
    "_git_worktree_prune",
    "_lsof_pids_in_tree",
    "_orphan_worktree_still_orphan_now",
    "_parse_lsof_pids",
    "_pid_cwd_is_still_in_tree",
    "_protected_run_ids_for_gc",
    "_reap_and_remove_tree",
    "_reap_processes_in_tree",
    "_run_dir_slimmable_now",
    "_run_dir_protected_now",
    "_run_id_protected_now",
    "_safe_iter_subdirs",
    "autoprune_transcripts",
    "execute_gc",
    "plan_gc",
]

DEFAULT_KEEP: int = 20
"""Default number of most-recent non-protected runs to keep."""

DEFAULT_MAX_AGE_DAYS: int = 0
"""Default age floor in days. ``0`` disables the age gate."""


def autoprune_transcripts(
    repo_root: Path,
    *,
    keep: int = DEFAULT_KEEP,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> GcReport:
    """Prune old runs' transcripts. Called at the start of every fresh loop so
    ``.syncade/runs/`` stays bounded without anyone remembering ``syncade --gc``.

    **Deliberately narrower than ``--gc``: transcripts only.** It does not remove
    worktrees, shell out to ``lsof``/``git worktree prune``, or reap processes. Those
    are the slow and destructive half of GC, and a loop's opening moments — with a
    concurrent syncade possibly mid-flight — are the wrong place for them. Disk growth
    is what auto-prune exists to bound, and disk growth is transcripts (90.9% of the
    corpus). ``--gc`` remains the explicit, full-power maintenance mode.

    Bounded in practice, not just in intent: measured at **165 ms cold / 69 ms warm**
    over the real 261-run corpus with 224 already-slim candidates (the worst case,
    where every candidate is re-walked and nothing is freed). That is noise against a
    review loop measured in minutes, so there is no artificial per-run cap — a cap
    would only leave a backlog that never drains.

    Protection is inherited whole from :func:`plan_gc`: resume-eligible runs, runs
    with a live status breadcrumb, and the newest ``keep`` runs are never touched.
    """
    plan = plan_gc(repo_root, keep=keep, max_age_days=max_age_days, skip_worktrees=True)
    return execute_gc(plan, dry_run=False, repo_root=repo_root)


def plan_gc(
    repo_root: Path,
    *,
    keep: int = DEFAULT_KEEP,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    worktree_base: Path = DEFAULT_WORKTREE_BASE,
    skip_worktrees: bool = False,
) -> GcPlan:
    """Partition ``.syncade/runs/`` into protected vs slimmable.

    ``skip_worktrees=True`` omits all worktree and orphan discovery, avoiding
    the ``git worktree list`` subprocess and the ``/tmp/syncade/`` walk.  Used by
    :func:`autoprune_transcripts` so routine loop startup never touches worktree
    planning — that is the slow and destructive half of GC, unsuitable for the
    opening moments of a loop.
    """
    runs_root = repo_root / ".syncade" / "runs"

    run_dirs = _safe_iter_subdirs(runs_root)
    protected = _protected_run_ids_for_gc(runs_root, run_dirs)

    candidates = [d for d in run_dirs if d.name not in protected]
    candidates.sort(key=_run_sort_key, reverse=True)

    to_slim = _select_for_slimming(candidates, keep=keep, max_age_days=max_age_days)
    slim_names = [d.name for d in to_slim]

    if skip_worktrees:
        return GcPlan(
            protected_run_ids=sorted(protected),
            runs_to_slim=slim_names,
            worktree_trees_to_remove=[],
            orphan_worktree_trees=[],
            worktree_tree_identities={},
        )

    worktree_trees = [
        tree
        for tree in existing_worktree_trees(worktree_base, slim_names)
        if not tree_contains_repo_root(tree, repo_root)
    ]
    known_run_ids = {d.name for d in run_dirs} | protected
    orphan_trees = repo_owned_orphan_trees(
        repo_root, _safe_iter_subdirs(worktree_base), known_run_ids
    )
    tree_identities = {
        tree: identity
        for tree in [*worktree_trees, *orphan_trees]
        if (identity := tree_identity(tree)) is not None
    }

    return GcPlan(
        protected_run_ids=sorted(protected),
        runs_to_slim=slim_names,
        worktree_trees_to_remove=worktree_trees,
        orphan_worktree_trees=orphan_trees,
        worktree_tree_identities=tree_identities,
    )


def _run_sort_key(run_dir: Path) -> float:
    started = _started_at_timestamp(run_dir)
    if started is not None:
        return started
    try:
        return run_dir.stat().st_mtime
    except OSError:
        return 0.0


def _started_at_timestamp(run_dir: Path) -> float | None:
    run_init = run_dir / RUN_INIT_FILENAME
    try:
        data = json.loads(run_init.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("started_at_utc")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return dt.timestamp()


def _select_for_slimming(
    candidates_newest_first: list[Path],
    *,
    keep: int,
    max_age_days: int,
) -> list[Path]:
    beyond_keep = candidates_newest_first[max(keep, 0) :]
    if max_age_days <= 0:
        return list(beyond_keep)

    cutoff = datetime.now(UTC).timestamp() - (max_age_days * 86400)
    return [d for d in beyond_keep if _run_sort_key(d) < cutoff]
