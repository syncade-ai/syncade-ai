"""``execute_gc`` execution + cwd-scoped reaping tests (PR-27 tasks 2-3).

All deletion/reaping runs over ``tmp_path`` synthetic trees. ``lsof`` is faked
(``monkeypatch`` of ``run_subprocess``) so no real processes are touched here —
the real-process reaping check lives in ``test_smoke.py`` behind
``@pytest.mark.smoke``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import syncade.gc_execute as gc_execute_module
from syncade.gc import GcPlan, execute_gc, plan_gc
from syncade.process import SubprocessNotFoundError, SubprocessResult

from ._helpers import STRUCTURED_ROUND_ARTIFACTS, make_repo, snapshot_tree, write_run


@pytest.fixture
def _fake_lsof_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_subprocess returns empty lsof (no in-dir pids) + no-op git."""

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)


def test_execute_slims_runs_and_removes_worktrees(tmp_path: Path, _fake_lsof_empty: None) -> None:
    """GC prunes transcripts; the run directory and every structured artifact in it
    SURVIVE. Whole-dir deletion was a data-loss bug: metrics.db is a derived view over
    .syncade/runs/, so deleting a run destroys its history on the next rebuild."""
    repo = make_repo(tmp_path)
    run_dir = write_run(repo, "run-del", final_exit_code=0, with_round=True)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)
    (wt_base / "run-del" / "marker").write_text("x", encoding="utf-8")

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    # the run and its history survive
    assert run_dir.exists()
    assert (run_dir / "run-init.json").exists()
    assert (run_dir / "loop-manifest.json").exists()
    for name in STRUCTURED_ROUND_ARTIFACTS:
        assert (run_dir / "round-0" / name).exists(), f"GC destroyed {name}"
    # only the transcripts are gone
    assert not (run_dir / "round-0" / "codex-reviewer.stdout").exists()
    assert not (run_dir / "round-0" / "codex-reviewer.stderr").exists()

    assert not (wt_base / "run-del").exists()
    assert "run-del" in report.runs_slimmed
    assert report.bytes_freed > 0
    assert (wt_base / "run-del") in report.worktrees_removed


def test_dry_run_changes_nothing(tmp_path: Path, _fake_lsof_empty: None) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0, with_round=True)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)
    (wt_base / "run-del" / "marker").write_text("x", encoding="utf-8")

    before = snapshot_tree(tmp_path)
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=True, repo_root=repo)
    after = snapshot_tree(tmp_path)

    assert before == after, "dry-run must not change the tree"
    assert (repo / ".syncade" / "runs" / "run-del").exists()
    assert (wt_base / "run-del").exists()
    # dry run must not touch the transcripts either
    assert (repo / ".syncade" / "runs" / "run-del" / "round-0" / "codex-reviewer.stdout").exists()
    # The report still describes what WOULD be removed.
    assert "run-del" in report.runs_slimmed
    assert report.bytes_freed > 0


def test_execute_best_effort_skips_missing_worktree(tmp_path: Path, _fake_lsof_empty: None) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0, with_round=True)
    wt_base = tmp_path / "wt"
    # No worktree tree on disk for run-del — execute must not raise.
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert (repo / ".syncade" / "runs" / "run-del").exists()
    assert "run-del" in report.runs_slimmed


def test_execute_calls_git_worktree_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(argv)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    execute_gc(plan, dry_run=False, repo_root=repo)

    assert any(c[:3] == ["git", "worktree", "prune"] for c in calls), calls


def test_reaping_kills_only_in_dir_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)

    # Tabular lsof reports two PIDs (4242, 4243) with files inside tree.
    lsof_stdout = (
        "COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        "python  4242  user  cwd    DIR  1,2    4096     10  " + str(tree) + "\n"
        "python  4243  user    3r   REG  1,2    100      20  " + str(tree / "f.txt") + "\n"
    )

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(
                returncode=0, stdout=lsof_stdout, stderr="", duration_seconds=0.0
            )
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert sorted(killed) == [4242, 4243]
    assert sorted(report.pids_reaped) == [4242, 4243]


def test_reaping_parses_terse_lsof_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)

    # `lsof -t` terse output: one PID per line (the real flag set we use).
    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(
                returncode=0, stdout="7001\n7002\n7001\n", stderr="", duration_seconds=0.0
            )
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    # Deduped (7001 appears twice), in first-seen order.
    assert report.pids_reaped == [7001, 7002]


def test_reaping_dry_run_kills_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(returncode=0, stdout="9999\n", stderr="", duration_seconds=0.0)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    execute_gc(plan, dry_run=True, repo_root=repo)

    assert killed == [], "dry-run must kill nothing"
    assert (wt_base / "run-del").exists()


def test_lsof_missing_warns_and_still_rmtrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            raise SubprocessNotFoundError("lsof")
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    err = capsys.readouterr().err
    assert "lsof" in err.lower()
    # Reaping skipped, but the tree is STILL removed.
    assert not (wt_base / "run-del").exists()
    assert (wt_base / "run-del") in report.worktrees_removed


def test_lsof_error_does_not_abort_gc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(
                returncode=1, stdout="", stderr="lsof: boom", duration_seconds=0.0
            )
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)  # must not raise
    assert not (wt_base / "run-del").exists()
    assert report.pids_reaped == []


def test_dead_pid_during_reap_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(
                returncode=0, stdout="5555\n5556\n", stderr="", duration_seconds=0.0
            )
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    def fake_kill(pid: int, sig: int) -> None:
        if pid == 5555:
            raise ProcessLookupError  # already dead
        killed.append(pid)

    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", fake_kill)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    # Only the live PID was killed; the dead one was skipped without aborting.
    assert killed == [5556]
    assert report.pids_reaped == [5556]


def test_foreign_worktree_tree_is_not_removed_or_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-27 dogfood finding 2 (execute level): a ``/tmp/syncade/<id>`` tree with
    no matching run in THIS repo is left untouched — never removed, never even
    probed/reaped — while a real run IS pruned."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)  # ours, deletable
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)
    foreign = wt_base / "foreign-other-repo-run"
    foreign.mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            # GC must never even probe a foreign tree.
            assert str(foreign) not in argv, "GC probed a foreign worktree tree"
            return SubprocessResult(returncode=0, stdout="8800\n", stderr="", duration_seconds=0.0)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    # The foreign tree is untouched; our pruned run's tree is removed + reaped.
    assert foreign.exists()
    assert foreign not in report.worktrees_removed
    assert not (wt_base / "run-del").exists()
    assert (wt_base / "run-del") in report.worktrees_removed
    assert 8800 in killed


def test_symlink_worktree_entry_is_not_reaped_or_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    wt_base.mkdir()
    outside_target = tmp_path / "outside-target"
    outside_target.mkdir()
    symlink_tree = wt_base / "run-del"
    symlink_tree.symlink_to(outside_target, target_is_directory=True)

    lsof_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            lsof_calls.append(list(argv))
            return SubprocessResult(returncode=0, stdout="9900\n", stderr="", duration_seconds=0.0)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert symlink_tree not in plan.worktree_trees_to_remove
    assert lsof_calls == []
    assert killed == []
    assert symlink_tree.is_symlink()
    assert outside_target.exists()
    assert symlink_tree not in report.worktrees_removed


def test_execute_defensively_skips_symlink_tree_from_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    wt_base = tmp_path / "wt"
    wt_base.mkdir()
    outside_target = tmp_path / "outside-target"
    outside_target.mkdir()
    symlink_tree = wt_base / "manual-plan"
    symlink_tree.symlink_to(outside_target, target_is_directory=True)

    lsof_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            lsof_calls.append(list(argv))
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    plan = GcPlan(
        protected_run_ids=[],
        runs_to_slim=[],
        worktree_trees_to_remove=[symlink_tree],
        orphan_worktree_trees=[],
    )

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert lsof_calls == []
    assert symlink_tree.is_symlink()
    assert outside_target.exists()
    assert symlink_tree not in report.worktrees_removed
    assert any("unsafe symlink" in error for error in report.errors)


def test_lsof_invocation_is_cwd_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PR-27 dogfood finding 1: discovery must restrict to the cwd descriptor
    (``-a -d cwd``), NOT match any open file under the tree."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)

    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            captured.append(list(argv))
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    execute_gc(plan, dry_run=False, repo_root=repo)

    assert captured, "lsof was not invoked"
    argv = captured[0]
    assert "-a" in argv and "-d" in argv and "cwd" in argv, argv
    # the -d flag's value is exactly 'cwd' (the cwd descriptor restriction).
    assert argv[argv.index("-d") + 1] == "cwd", argv


def test_dry_run_reports_would_reap_pids_without_killing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-27 dogfood finding 4: dry-run must REPORT the PIDs it would reap (via a
    read-only lsof probe) while killing nothing and removing nothing."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(returncode=0, stdout="12345\n", stderr="", duration_seconds=0.0)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=True, repo_root=repo)

    # The would-reap PID IS reported, but nothing is actually killed/removed.
    assert 12345 in report.pids_reaped
    assert killed == []
    assert (wt_base / "run-del").exists()
    assert (repo / ".syncade" / "runs" / "run-del").exists()


def test_lsof_real_error_emits_loud_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """PR-27 dogfood finding 5: a non-zero lsof exit WITH stderr (e.g. permission
    denied) must emit a loud stderr warning + be recorded in report.errors, not
    silently swallowed. (rc==1 with empty stderr = "no match", stays quiet.)"""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    (wt_base / "run-del").mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(
                returncode=2, stdout="", stderr="lsof: permission denied", duration_seconds=0.0
            )
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert any("lsof errored" in e for e in report.errors), report.errors
    assert "lsof errored" in capsys.readouterr().err
    # best-effort: the tree is still removed even though reaping was skipped.
    assert (wt_base / "run-del") in report.worktrees_removed


def test_worktree_removal_failure_is_reported_not_silent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-27 dogfood finding 2: when `rmtree` can't remove the tree (a
    non-writable parent), GC must record an error AND NOT claim the tree was
    removed — `ignore_errors=True` previously returned `removed=True` silently."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission checks")
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)
    (tree / "f").write_text("x", encoding="utf-8")

    # lsof empty → no reaping noise.
    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0
        ),
    )
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)

    os.chmod(wt_base, 0o500)  # parent non-writable → rmdir(tree) can't succeed
    try:
        report = execute_gc(plan, dry_run=False, repo_root=repo)
    finally:
        os.chmod(wt_base, 0o755)

    assert tree.exists(), "precondition: rmtree should have been blocked"
    assert tree not in report.worktrees_removed
    assert any("still present after rmtree" in e for e in report.errors), report.errors


def test_real_git_orphan_with_registered_worktree_is_removed(tmp_path: Path) -> None:
    """PR-27 dogfood finding 1 (the operator's real-git repro): a gone-run tree
    whose nested worktree is registered in THIS repo IS collected + removed; a
    foreign tree (not registered) is left untouched. End-to-end against real git."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    (repo / ".syncade" / "runs").mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "i")

    wt_base = tmp_path / "wt"
    gone = wt_base / "gone-run"
    git("worktree", "add", "--detach", "-q", str(gone / "round-0" / "rv1"))  # registers under gone
    foreign = wt_base / "foreign-run"
    foreign.mkdir(parents=True)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert gone in plan.orphan_worktree_trees, plan.orphan_worktree_trees
    assert foreign not in plan.orphan_worktree_trees

    report = execute_gc(plan, dry_run=False, repo_root=repo)
    assert not gone.exists(), "provably-ours gone-run tree should be removed"
    assert foreign.exists(), "foreign tree must be left untouched"
    assert gone in report.worktrees_removed
