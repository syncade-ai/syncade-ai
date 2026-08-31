"""Real-process cwd-scoped reaping smoke test (PR-27 task 3).

Marked ``@pytest.mark.smoke`` so the default suite deselects it (the default
``addopts`` is ``-m 'not smoke'``). It spawns real ``sleep`` processes with
cwds inside a ``tmp_path`` worktree base it creates — NEVER ``/tmp/syncade``,
and NEVER invokes ``syncade --gc`` for real.

The whole safety claim of PR-27 is that reaping is cwd-scoped: a process whose
cwd is inside a GC'd worktree is reaped; one whose cwd is OUTSIDE is untouched.
This test exercises that against real ``lsof`` + exact-PID ``kill``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from syncade.gc import execute_gc, plan_gc
from syncade.workspace_owner import record_owner

from ._helpers import make_repo, write_run


def _assert_dead(proc: subprocess.Popen, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {proc.pid} was not reaped within {timeout}s")


@pytest.mark.smoke
def test_real_process_reaping_is_cwd_scoped(tmp_path: Path) -> None:
    """Spawn two real ``sleep`` processes — one with cwd INSIDE a tmp worktree
    tree, one with cwd OUTSIDE it. GC reaping must kill only the in-tree one."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)

    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)
    record_owner(tree, repo)
    outside = tmp_path / "outside"
    outside.mkdir()

    # start_new_session=True mirrors process.run_subprocess while GC still kills
    # only the exact cwd-confirmed PID.
    inside_proc = subprocess.Popen(["sleep", "60"], cwd=str(tree), start_new_session=True)
    outside_proc = subprocess.Popen(["sleep", "60"], cwd=str(outside), start_new_session=True)
    try:
        # Give the OS a beat to register the cwds for lsof.
        time.sleep(0.5)

        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
        report = execute_gc(plan, dry_run=False, repo_root=repo)

        # The in-tree sleep must be reaped.
        assert inside_proc.pid in report.pids_reaped, report.pids_reaped
        _assert_dead(inside_proc, timeout=5.0)

        # The outside sleep must be untouched.
        assert outside_proc.pid not in report.pids_reaped
        assert outside_proc.poll() is None, "outside process must still be alive"
    finally:
        for p in (inside_proc, outside_proc):
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _ps_state(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True)
    s = r.stdout.strip()
    return s if s else "GONE"


@pytest.mark.smoke
def test_real_reaping_does_not_killpg_out_of_tree_group_member(tmp_path: Path) -> None:
    """The collateral-kill case the original smoke test cannot reach: an in-tree
    process that is NOT a group leader, sharing its process group with an
    out-of-tree leader (the shape of "operator cd'd into a leaked worktree from
    their interactive shell"). GC must reap ONLY the in-tree PID, leaving the
    out-of-tree leader alive — a ``killpg`` would have taken the whole group."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)

    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)
    record_owner(tree, repo)
    outside = tmp_path / "outside"
    outside.mkdir()

    # bash leader: cwd OUTSIDE the tree (own new session → group leader). It
    # forks a child in the SAME group whose cwd is INSIDE the tree (no setsid),
    # then itself sleeps. lsof will see only the in-tree child.
    script = f'cd "{outside}"; ( cd "{tree}"; exec sleep 60 ) & echo "L=$$ C=$!"; exec sleep 60'
    proc = subprocess.Popen(
        ["bash", "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    leader_pid = proc.pid
    child_pid = int(line.split("C=")[1])
    try:
        time.sleep(0.6)
        assert os.getpgid(child_pid) == os.getpgid(leader_pid), "child must share leader's group"

        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
        report = execute_gc(plan, dry_run=False, repo_root=repo)

        # In-tree child reaped.
        assert child_pid in report.pids_reaped, report.pids_reaped
        # Out-of-tree leader (same group, cwd OUTSIDE) must SURVIVE.
        assert leader_pid not in report.pids_reaped
        time.sleep(0.4)
        assert _ps_state(leader_pid) not in ("Z", "GONE"), (
            f"out-of-tree leader was collateral-killed (state={_ps_state(leader_pid)})"
        )
    finally:
        for pid in (child_pid, leader_pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if proc.stdout is not None:
            proc.stdout.close()  # PR-27 finding 6: avoid ResourceWarning under -W error
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@pytest.mark.smoke
def test_real_reaping_ignores_open_file_with_cwd_outside(tmp_path: Path) -> None:
    """PR-27 dogfood finding 1: a process whose cwd is OUTSIDE the tree but which
    holds an OPEN FILE inside it must NOT be reaped — only cwd-in-tree is killed.
    Plain ``lsof +D`` matched any open file and wrongly killed such a process;
    ``lsof -a -d cwd +D`` does not. Verified against real lsof."""
    repo = make_repo(tmp_path)
    write_run(repo, "run-del", final_exit_code=0)

    wt_base = tmp_path / "wt"
    tree = wt_base / "run-del"
    tree.mkdir(parents=True)
    record_owner(tree, repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    afile = tree / "afile.txt"
    afile.write_text("data", encoding="utf-8")

    # victim: cwd OUTSIDE the tree, but holds an open fd on a file INSIDE it.
    victim = subprocess.Popen(["tail", "-f", str(afile)], cwd=str(outside), start_new_session=True)
    # control: cwd INSIDE the tree — must still be reaped.
    inside = subprocess.Popen(["sleep", "60"], cwd=str(tree), start_new_session=True)
    try:
        time.sleep(0.5)
        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
        report = execute_gc(plan, dry_run=False, repo_root=repo)

        # cwd-in-tree control IS reaped.
        assert inside.pid in report.pids_reaped, report.pids_reaped
        # open-file-but-cwd-OUTSIDE victim is NOT reaped and stays alive.
        assert victim.pid not in report.pids_reaped, report.pids_reaped
        time.sleep(0.3)
        assert victim.poll() is None, "cwd-outside (open-file-only) process was wrongly reaped"
    finally:
        for p in (victim, inside):
            if p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
