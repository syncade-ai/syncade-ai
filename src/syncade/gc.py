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
    repo_owned_existing_trees,
    repo_owned_orphan_trees,
    tree_identity,
    tree_size_bytes,
    unclaimable_trees,
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
    "repo_owned_existing_trees",
]

DEFAULT_KEEP: int = 20
"""Default number of most-recent non-protected runs to keep."""

DEFAULT_MAX_AGE_DAYS: int = 0

#: Tier 3 (PR-h-12 item 2). Calibrated on this repo's corpus rather than picked round: over 421
#: runs, 65 of them resume-protected, a 14-day floor releases 56 (86%) and keeps 9. A 7-day floor
#: buys 6% more for half the safety margin; 30 leaves 19 runs at ~130 MB/round still accruing,
#: which is the unbounded growth this exists to stop. ``0`` disables the bound entirely.
DEFAULT_WORKTREE_MAX_AGE_DAYS: int = 14
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
    worktree_max_age_days: int = DEFAULT_WORKTREE_MAX_AGE_DAYS,
    worktree_base: Path = DEFAULT_WORKTREE_BASE,
    skip_worktrees: bool = False,
) -> GcPlan:
    """Partition ``.syncade/runs/`` into protected vs slimmable.

    ``skip_worktrees=True`` omits all worktree and orphan discovery, avoiding
    the ``git rev-parse`` subprocess and the ``/tmp/syncade/`` walk.  Used by
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
            worktree_age_released=[],
        )

    # TIER 3 (PR-h-12 item 2). Worktree removal is independent of tier-2 transcript slimming:
    # `slim_names` is the transcript filter (beyond-keep AND old-enough), but a run INSIDE the
    # keep window can still have an ancient worktree nobody is inspecting. The two tiers must be
    # selected by their own criteria — `worktree_max_age_days` for tier 3, `keep`/`max_age_days`
    # for tier 2 — so they can be non-zero for one and zero for the other simultaneously.
    #
    # `released` is the PROTECTED half: protected runs aged past the floor, with a TOCTOU bypass
    # flag so execute_gc skips the on-disk protection re-check (the disk state hasn't changed —
    # only age releases it). `worktree_aged` is the UNPROTECTED half: candidates aged past the
    # floor, selected independently of slim_names. Both are empty when worktree_max_age_days=0
    # (the opt-out that reproduces the pre-PR-h-12 behaviour).
    # TIER 3, selected by its OWN rule — never derived from `slim_names`. See
    # `_worktree_removable`: deriving tier-3 targets from tier-2's selection is what let a
    # one-day-old worktree be deleted because its transcripts had aged out.
    all_worktree_ids, released = _worktree_removable(run_dirs, protected, worktree_max_age_days)
    worktree_trees = repo_owned_existing_trees(repo_root, worktree_base, all_worktree_ids)
    known_run_ids = {d.name for d in run_dirs} | protected
    subdirs = _safe_iter_subdirs(worktree_base)
    orphan_trees = repo_owned_orphan_trees(repo_root, subdirs, known_run_ids)
    stranded = unclaimable_trees(repo_root, subdirs, known_run_ids)
    stranded_sizes = [tree_size_bytes(tree) for tree in stranded.all_trees]
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
        worktree_age_released=released,
        unclaimable_recordless_trees=stranded.recordless,
        unclaimable_unreadable_trees=stranded.unreadable,
        unclaimable_bytes=(
            None
            if any(size is None for size in stranded_sizes)
            else sum(size for size in stranded_sizes if size is not None)
        ),
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


def _worktree_removable(
    run_dirs: list[Path], protected: set[str], worktree_max_age_days: int
) -> tuple[list[str], list[str]]:
    """``(removable_run_ids, of_those_that_were_protected)`` — TIER 3's only rule.

    One predicate, because the coupling was the defect. Worktree selection used to be a union
    with ``slim_names``, so a run beyond ``gc.keep`` lost its worktree the moment its TRANSCRIPTS
    became eligible — a one-day-old inspection worktree deleted under normal run volume, with the
    age floor bypassed entirely. That contradicted the four-tier policy in ``CLAUDE.md``, which
    says tier 3 is removed on its own age rule; the document was right and the code never matched
    it. Two dogfood rounds fixed half of it each, because each added a source instead of removing
    the coupling.

    A worktree is removable when it is OLDER than the floor, and — for a run that is still
    resume-eligible — only when a floor is actually configured::

        age > floor  and  (not protected  or  floor > 0)

    ``worktree_max_age_days = 0`` is the opt-out and keeps its meaning at both ends: an
    unprotected run's worktree is collectable immediately, exactly as before PR-h-12, while a
    resumable run's is never released. The second return value is the protected subset, which
    ``execute_gc`` needs to bypass its protection re-check for those specific runs.
    """
    cutoff = datetime.now(UTC).timestamp() - (worktree_max_age_days * 86400)
    removable = [d for d in run_dirs if _run_sort_key(d) < cutoff]
    if worktree_max_age_days <= 0:
        removable = [d for d in removable if d.name not in protected]
    return (
        sorted(d.name for d in removable),
        sorted(d.name for d in removable if d.name in protected),
    )


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
