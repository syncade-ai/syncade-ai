"""``plan_gc`` is best-effort over its own artifacts: a permission-denied /
unreadable directory is skipped, never raised out (PR-27 robustness invariant).

The brief promises GC "never aborts mid-pass and never raises out of the mode
handler". The mode handler is a thin ``plan_gc`` → ``execute_gc`` → print
wrapper, so the raise-free guarantee reduces to those two never raising. An
unreadable ``.syncade/runs/`` (or worktree base) at mode ``0o000`` makes both
``find_resumable_runs`` (which ``iterdir``'s ``runs/``) and ``plan_gc``'s own
enumeration raise ``PermissionError`` — these tests pin that they degrade to an
empty plan instead.

All fixtures are ``tmp_path`` synthetic dirs; permissions are restored in a
``finally`` so the test runner can always clean up.
"""

from __future__ import annotations

import os
from pathlib import Path

from syncade.gc import plan_gc

from ._helpers import make_repo, write_run


def test_plan_gc_does_not_raise_on_unreadable_runs_dir(tmp_path: Path) -> None:
    """A 0o000 ``.syncade/runs/`` must not crash planning — degrade to an empty
    plan (we can't read it, so we delete nothing: the under-delete bias)."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-old", final_exit_code=0)
    runs = repo / ".syncade" / "runs"
    os.chmod(runs, 0o000)
    try:
        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path / "no-wt")
    finally:
        os.chmod(runs, 0o755)

    assert plan.runs_to_slim == []
    assert plan.protected_run_ids == []
    assert plan.worktree_trees_to_remove == []


def test_plan_gc_does_not_raise_on_unreadable_worktree_base(tmp_path: Path) -> None:
    """A 0o000 worktree base must not crash planning — skip it."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-keep", final_exit_code=0)
    wt_base = tmp_path / "wt"
    wt_base.mkdir()
    os.chmod(wt_base, 0o000)
    try:
        plan = plan_gc(repo, keep=20, max_age_days=0, worktree_base=wt_base)
    finally:
        os.chmod(wt_base, 0o755)

    # The (readable, protected) run is still planned correctly; the unreadable
    # worktree base contributes no removals rather than crashing.
    assert plan.worktree_trees_to_remove == []
