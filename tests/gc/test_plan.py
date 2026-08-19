"""``plan_gc`` partitioning tests (PR-27 task 1) — pure, no destructive execution.

Pins the load-bearing invariants: protected (resume-eligible or ambiguous) runs
are NEVER in the slim set; non-run siblings (config.toml / last-reviewed.json /
draft-spec-*.md / runs/.gitignore) are NEVER candidates; keep-N + age gating;
ambiguous pre-init/malformed state handled conservatively; worktree-tree mapping.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import syncade.gc_worktrees as gcw_module
from syncade.gc import GcPlan, GcReport, plan_gc

from ._helpers import make_repo, write_run


def test_repo_owned_orphan_tree_is_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-27 dogfood finding 1: a ``/tmp/syncade/<id>`` tree whose run dir is GONE
    but which has a worktree registered in THIS repo (provably ours) IS collected
    as an orphan; a foreign tree (not registered) is left alone. (My finding-2 fix
    over-corrected by dropping orphan collection entirely; this restores it
    safely.)"""
    repo = make_repo(tmp_path)  # .syncade/runs/ has no run dirs → both are "gone"
    wt_base = tmp_path / "wt"
    ours = wt_base / "gone-ours"
    (ours / "round-0" / "rv1").mkdir(parents=True)
    foreign = wt_base / "foreign-run"
    foreign.mkdir(parents=True)

    # Pretend the active Git-ownership proof found a live worktree UNDER the
    # 'ours' tree only. Real-git coverage for this boundary lives in
    # test_execute.py and test_self_orphan_protection.py.
    monkeypatch.setattr(
        gcw_module,
        "_active_repo_worktree_paths",
        lambda repo_root, repo_resolved: {(ours / "round-0" / "rv1").resolve()},
    )

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert ours in plan.orphan_worktree_trees
    assert foreign not in plan.orphan_worktree_trees


def test_protected_runs_never_in_slim_set(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    # Resume-eligible runs (interrupted + env-failure exit codes) must be
    # protected regardless of age/count.
    write_run(repo, "2026-01-01T00-00-00", with_loop_manifest=False)  # interrupted
    write_run(repo, "2026-01-02T00-00-00", final_exit_code=40)  # env failure
    write_run(repo, "2026-01-03T00-00-00", final_exit_code=60)
    write_run(repo, "2026-01-04T00-00-00", final_exit_code=70)
    write_run(repo, "2026-01-05T00-00-00", final_exit_code=10)  # decision-needed
    # A normally-completed run is a candidate.
    write_run(repo, "2026-01-06T00-00-00", final_exit_code=0)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_max_age_days=0)

    protected = set(plan.protected_run_ids)
    assert protected == {
        "2026-01-01T00-00-00",
        "2026-01-02T00-00-00",
        "2026-01-03T00-00-00",
        "2026-01-04T00-00-00",
        "2026-01-05T00-00-00",
    }
    assert protected.isdisjoint(set(plan.runs_to_slim))
    assert "2026-01-06T00-00-00" in plan.runs_to_slim


def test_siblings_never_candidates(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    runs = repo / ".syncade" / "runs"
    (runs / ".gitignore").write_text("*\n", encoding="utf-8")
    # Non-run state lives ABOVE runs/, but pin that it never leaks in.
    (repo / ".syncade" / "config.toml").write_text("x = 1\n", encoding="utf-8")
    (repo / ".syncade" / "last-reviewed.json").write_text("{}", encoding="utf-8")
    (repo / ".syncade" / "draft-spec-abc.md").write_text("# draft\n", encoding="utf-8")

    write_run(repo, "2026-01-06T00-00-00", final_exit_code=0)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_max_age_days=0)

    assert plan.runs_to_slim == ["2026-01-06T00-00-00"]
    assert ".gitignore" not in plan.runs_to_slim
    assert "config.toml" not in plan.runs_to_slim


def test_keep_n_retains_newest_candidates(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        write_run(repo, f"run-{i}", started_at=base + timedelta(days=i), final_exit_code=0)

    plan = plan_gc(repo, keep=2, max_age_days=0, worktree_max_age_days=0)

    assert set(plan.runs_to_slim) == {"run-0", "run-1", "run-2"}


def test_age_gate_only_deletes_beyond_n_and_older_than_d(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    now = datetime.now(UTC)
    write_run(repo, "run-recent", started_at=now - timedelta(days=1), final_exit_code=0)
    write_run(repo, "run-old", started_at=now - timedelta(days=100), final_exit_code=0)

    # keep=0 so both are beyond keep, but age gate D=30 protects the recent one.
    plan = plan_gc(repo, keep=0, max_age_days=30, worktree_max_age_days=0)

    assert plan.runs_to_slim == ["run-old"]


def test_age_gate_off_when_zero(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    now = datetime.now(UTC)
    write_run(repo, "run-recent", started_at=now - timedelta(days=1), final_exit_code=0)
    write_run(repo, "run-old", started_at=now - timedelta(days=100), final_exit_code=0)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_max_age_days=0)

    assert set(plan.runs_to_slim) == {"run-recent", "run-old"}


def test_candidates_ordered_newest_first_by_started_at(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    base = datetime(2026, 3, 1, tzinfo=UTC)
    # Out of lexical order to prove sort uses started_at, not name.
    write_run(repo, "zzz", started_at=base + timedelta(days=2), final_exit_code=0)
    write_run(repo, "aaa", started_at=base + timedelta(days=1), final_exit_code=0)
    write_run(repo, "mmm", started_at=base, final_exit_code=0)

    plan = plan_gc(repo, keep=1, max_age_days=0, worktree_max_age_days=0)

    assert set(plan.runs_to_slim) == {"aaa", "mmm"}


def test_missing_started_at_falls_back_to_mtime(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    d_old = write_run(repo, "no-ts-old", final_exit_code=0)
    d_new = write_run(repo, "no-ts-new", final_exit_code=0)
    old_t = time.time() - 10_000
    new_t = time.time() - 100
    os.utime(d_old, (old_t, old_t))
    os.utime(d_new, (new_t, new_t))

    plan = plan_gc(repo, keep=1, max_age_days=0, worktree_max_age_days=0)

    assert plan.runs_to_slim == ["no-ts-old"]


def test_run_without_run_init_is_protected_as_pre_init_state(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "orphan-dir", with_run_init=False, with_loop_manifest=False)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_max_age_days=0)

    assert "orphan-dir" in plan.protected_run_ids
    assert "orphan-dir" not in plan.runs_to_slim


def test_malformed_loop_manifest_is_protected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    run_dir = write_run(repo, "malformed", with_loop_manifest=False)
    (run_dir / "loop-manifest.json").write_text("{not json", encoding="utf-8")

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_max_age_days=0)

    assert "malformed" in plan.protected_run_ids
    assert "malformed" not in plan.runs_to_slim


def test_no_runs_dir_returns_empty_plan(tmp_path: Path) -> None:
    plan = plan_gc(tmp_path, keep=20, max_age_days=0, worktree_max_age_days=0)
    assert plan.runs_to_slim == []
    assert plan.protected_run_ids == []


def test_worktree_trees_mapped_for_each_deleted_run(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    write_run(repo, "run-keep", with_loop_manifest=False)  # protected

    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)
    (wt_base / "run-keep").mkdir(parents=True)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)

    assert (wt_base / "run-del") in plan.worktree_trees_to_remove
    # The protected run's worktree tree is NEVER removed.
    assert (wt_base / "run-keep") not in plan.worktree_trees_to_remove


def test_foreign_worktree_tree_with_no_run_is_left_alone(tmp_path: Path) -> None:
    """PR-27 dogfood finding 2: GC must NOT remove a ``/tmp/syncade/<id>`` tree
    that has no matching run dir in THIS repo — it may belong to another repo
    sharing the worktree base. Only worktrees of runs being pruned are removed
    (those are provably ours). The old shared-base "orphan" scan deleted a
    foreign repo's tree; that behavior is gone."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)  # ours, slimmable
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)
    (wt_base / "foreign-other-repo-run").mkdir(parents=True)  # NOT ours

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)

    # Our pruned run's tree IS scheduled; the foreign tree is left alone.
    assert (wt_base / "run-del") in plan.worktree_trees_to_remove
    assert (wt_base / "foreign-other-repo-run") not in plan.worktree_trees_to_remove


def test_gcplan_and_gcreport_are_frozen() -> None:
    plan = GcPlan(
        protected_run_ids=[],
        runs_to_slim=[],
        worktree_trees_to_remove=[],
        orphan_worktree_trees=[],
    )
    import pytest

    with pytest.raises((AttributeError, TypeError)):
        plan.runs_to_slim = ["x"]  # type: ignore[misc]
    report = GcReport(
        runs_slimmed=[],
        worktrees_removed=[],
        pids_reaped=[],
        errors=[],
        dry_run=True,
    )
    with pytest.raises((AttributeError, TypeError)):
        report.dry_run = False  # type: ignore[misc]
