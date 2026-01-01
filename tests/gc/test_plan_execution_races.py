from __future__ import annotations

from pathlib import Path

import syncade.gc_execute as gc_execute_module
from syncade.gc import GcPlan, execute_gc

from ._helpers import write_run


def test_execute_gc_rechecks_protected_runs_before_deleting(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_id = "2026-06-24T10-00-00"
    run_dir = repo / ".syncade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    tree = tmp_path / "worktrees" / run_id
    tree.mkdir(parents=True)

    monkeypatch.setattr(gc_execute_module, "current_protected_run_ids", lambda repo_root: {run_id})

    removed_trees: list[Path] = []

    def fake_remove(tree_path: Path, *, dry_run: bool, errors: list[str]):
        removed_trees.append(tree_path)
        return [], True

    monkeypatch.setattr(gc_execute_module, "_reap_and_remove_tree", fake_remove)

    report = execute_gc(
        GcPlan(
            protected_run_ids=[],
            runs_to_slim=[run_id],
            worktree_trees_to_remove=[tree],
            orphan_worktree_trees=[],
        ),
        dry_run=False,
        repo_root=repo,
    )

    assert run_dir.exists()
    assert removed_trees == []
    assert report.runs_slimmed == []
    assert report.worktrees_removed == []


def test_execute_gc_rechecks_pre_run_init_dir_before_deleting(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run_id = "2026-06-24T10-30-00"
    run_dir = repo / ".syncade" / "runs" / run_id
    run_dir.mkdir(parents=True)

    report = execute_gc(
        GcPlan(
            protected_run_ids=[],
            runs_to_slim=[run_id],
            worktree_trees_to_remove=[],
            orphan_worktree_trees=[],
        ),
        dry_run=False,
        repo_root=repo,
    )

    assert run_dir.exists()
    assert report.runs_slimmed == []


def test_execute_gc_skips_newly_protected_self_orphan_tree(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    run_id = "2026-06-24T11-00-00"
    tree = tmp_path / "worktrees" / run_id
    tree.mkdir(parents=True)

    monkeypatch.setattr(gc_execute_module, "current_protected_run_ids", lambda repo_root: {run_id})
    removed_trees: list[Path] = []

    def fake_remove(tree_path: Path, *, dry_run: bool, errors: list[str]):
        removed_trees.append(tree_path)
        return [], True

    monkeypatch.setattr(gc_execute_module, "_reap_and_remove_tree", fake_remove)

    report = execute_gc(
        GcPlan(
            protected_run_ids=[],
            runs_to_slim=[],
            worktree_trees_to_remove=[],
            orphan_worktree_trees=[tree],
        ),
        dry_run=False,
        repo_root=repo,
    )

    assert tree.exists()
    assert removed_trees == []
    assert report.worktrees_removed == []


def test_execute_gc_rechecks_run_protection_after_initial_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    run_id = "race-run"
    run_dir = write_run(repo, run_id, final_exit_code=0)
    tree = tmp_path / "worktrees" / run_id
    tree.mkdir(parents=True)

    def fake_current_protected(repo_root: Path) -> set[str]:
        (run_dir / "loop-manifest.json").unlink()
        return set()

    monkeypatch.setattr(gc_execute_module, "current_protected_run_ids", fake_current_protected)
    removed_trees: list[Path] = []

    def fake_remove(tree_path: Path, *, dry_run: bool, errors: list[str]):
        removed_trees.append(tree_path)
        return [], True

    monkeypatch.setattr(gc_execute_module, "_reap_and_remove_tree", fake_remove)

    report = execute_gc(
        GcPlan(
            protected_run_ids=[],
            runs_to_slim=[run_id],
            worktree_trees_to_remove=[tree],
            orphan_worktree_trees=[],
        ),
        dry_run=False,
        repo_root=repo,
    )

    assert run_dir.exists()
    assert tree.exists()
    assert removed_trees == []
    assert report.runs_slimmed == []
    assert report.worktrees_removed == []


def test_execute_gc_rechecks_orphan_status_after_initial_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    runs_root = repo / ".syncade" / "runs"
    runs_root.mkdir(parents=True)
    run_id = "race-run"
    run_dir = runs_root / run_id
    tree = tmp_path / "worktrees" / run_id
    tree.mkdir(parents=True)

    def fake_current_protected(repo_root: Path) -> set[str]:
        run_dir.mkdir()
        return set()

    monkeypatch.setattr(gc_execute_module, "current_protected_run_ids", fake_current_protected)
    removed_trees: list[Path] = []

    def fake_remove(tree_path: Path, *, dry_run: bool, errors: list[str]):
        removed_trees.append(tree_path)
        return [], True

    monkeypatch.setattr(gc_execute_module, "_reap_and_remove_tree", fake_remove)

    report = execute_gc(
        GcPlan(
            protected_run_ids=[],
            runs_to_slim=[],
            worktree_trees_to_remove=[],
            orphan_worktree_trees=[tree],
        ),
        dry_run=False,
        repo_root=repo,
    )

    assert run_dir.exists()
    assert tree.exists()
    assert removed_trees == []
    assert report.worktrees_removed == []
