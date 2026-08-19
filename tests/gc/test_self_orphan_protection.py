from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import syncade.gc_execute as gc_execute_module
from syncade.gc import GcPlan, execute_gc, plan_gc
from tests.gc._helpers import write_run


def test_gc_does_not_collect_main_checkout_as_orphan(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "i")

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path, worktree_max_age_days=0)

    assert repo not in plan.orphan_worktree_trees
    assert repo not in plan.worktree_trees_to_remove

    report = execute_gc(plan, dry_run=False, repo_root=repo)
    assert repo.exists()
    assert repo not in report.worktrees_removed


def test_gc_does_not_plan_main_checkout_as_pruned_run_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    (repo / "keep.txt").write_text("main checkout\n", encoding="utf-8")
    write_run(repo, repo.name, final_exit_code=0)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=tmp_path, worktree_max_age_days=0)

    assert plan.runs_to_slim == [repo.name]
    assert repo not in plan.worktree_trees_to_remove
    assert repo not in plan.orphan_worktree_trees

    report = execute_gc(plan, dry_run=False, repo_root=repo)
    assert repo.exists()
    assert (repo / "keep.txt").exists()
    assert repo not in report.worktrees_removed
    assert (repo / ".syncade" / "runs" / repo.name).exists()  # slimmed, not deleted


def test_execute_gc_refuses_replacement_pruned_run_tree_from_stale_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    run_id = "2026-06-28T15-00-00"
    write_run(repo, run_id, final_exit_code=0)
    wt_base = tmp_path / "worktree_base"
    tree = wt_base / run_id
    tree.mkdir(parents=True)
    (tree / "old-owned.txt").write_text("original\n", encoding="utf-8")

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert tree in plan.worktree_trees_to_remove
    assert tree in plan.worktree_tree_identities

    shutil.rmtree(tree)
    tree.mkdir(parents=True)
    marker = tree / "foreign-owned.txt"
    marker.write_text("must survive\n", encoding="utf-8")

    report = execute_gc(plan, dry_run=False, repo_root=repo)
    assert tree.exists()
    assert marker.exists()
    assert tree not in report.worktrees_removed
    assert any("changed since GC planning" in error for error in report.errors)
    assert (repo / ".syncade" / "runs" / run_id).exists()  # slimmed, not deleted


def test_execute_gc_refuses_stale_plan_that_would_remove_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    (repo / "keep.txt").write_text("main checkout\n", encoding="utf-8")
    monkeypatch.setattr(gc_execute_module, "_reap_processes_in_tree", lambda *args, **kwargs: [])

    report = execute_gc(
        GcPlan(
            protected_run_ids=[],
            runs_to_slim=[],
            worktree_trees_to_remove=[],
            orphan_worktree_trees=[repo],
        ),
        dry_run=False,
        repo_root=repo,
    )

    assert repo.exists()
    assert (repo / "keep.txt").exists()
    assert report.worktrees_removed == []
    assert any("contains repo root" in error for error in report.errors)


def test_execute_gc_refuses_manual_worktree_plan_without_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    tree = tmp_path / "worktree_base" / "manual-run"
    tree.mkdir(parents=True)
    marker = tree / "marker.txt"
    marker.write_text("must survive\n", encoding="utf-8")

    report = execute_gc(
        GcPlan(
            protected_run_ids=[],
            runs_to_slim=[],
            worktree_trees_to_remove=[tree],
            orphan_worktree_trees=[],
        ),
        dry_run=False,
        repo_root=repo,
    )

    assert tree.exists()
    assert marker.exists()
    assert tree not in report.worktrees_removed
    assert any("no recorded directory identity" in error for error in report.errors)


def test_gc_does_not_remove_replacement_tree_from_stale_worktree_registry(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "i")

    wt_base = tmp_path / "worktree_base"
    gone = wt_base / "gone-run"
    registered_child = gone / "round-0" / "rv1"
    git("worktree", "add", "--detach", "-q", str(registered_child))

    stale_plan = plan_gc(
        repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0
    )
    assert gone in stale_plan.orphan_worktree_trees

    shutil.rmtree(gone)
    gone.mkdir(parents=True)
    marker = gone / "foreign-owned.txt"
    marker.write_text("must survive\n", encoding="utf-8")

    fresh_plan = plan_gc(
        repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0
    )
    assert gone not in fresh_plan.orphan_worktree_trees

    report = execute_gc(stale_plan, dry_run=False, repo_root=repo)
    assert gone.exists()
    assert marker.exists()
    assert gone not in report.worktrees_removed


def test_gc_does_not_collect_top_level_registered_worktree_as_orphan(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")

    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "i")

    wt_base = tmp_path / "worktree_base"
    top_level_worktree = wt_base / "manual-worktree"
    git("worktree", "add", "--detach", "-q", str(top_level_worktree))

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert top_level_worktree not in plan.orphan_worktree_trees

    report = execute_gc(plan, dry_run=False, repo_root=repo)
    assert top_level_worktree.exists()
    assert top_level_worktree not in report.worktrees_removed
