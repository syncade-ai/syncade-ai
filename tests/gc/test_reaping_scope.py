"""Reaping is cwd-scoped to a freshly re-verified PID, never widened to a
process group (PR-27 adversarial fixes).

The cwd-scoping safety claim is that GC kills ONLY processes whose working dir
is inside the worktree being removed at the moment of signaling. ``os.killpg``
would widen that to the PID's whole process GROUP and ``lsof`` snapshots can be
stale, so the implementation re-checks each PID's cwd immediately before
``os.kill(pid, SIGKILL)`` and never escalates to a group kill.

These unit tests fake ``lsof`` + ``os.getpgid`` so no real process is touched;
the real-process collateral repro lives in ``test_smoke.py``
(``@pytest.mark.smoke``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import syncade.gc_execute as gc_execute_module
from syncade.process import SubprocessResult


def _fake_lsof(monkeypatch: pytest.MonkeyPatch, tree: Path, pid: int) -> None:
    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return SubprocessResult(
                returncode=0, stdout=f"{pid}\n", stderr="", duration_seconds=0.0
            )
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)


def test_non_leader_in_tree_pid_is_killed_by_pid_not_by_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An in-tree PID that is NOT its own group leader must be SIGKILL'd via
    ``os.kill(pid)`` — never ``os.killpg`` (which would hit out-of-tree members
    of its group, e.g. the operator's interactive shell)."""
    tree = tmp_path / "wt" / "run-x"
    tree.mkdir(parents=True)
    _fake_lsof(monkeypatch, tree, pid=4242)

    # 4242 is a NON-leader: its group leader is a DIFFERENT pid (9999, the
    # operator's out-of-tree shell). killpg(9999) would kill that shell.
    monkeypatch.setattr(gc_execute_module.os, "getpgid", lambda pid: 9999)

    killpg_calls: list[int] = []
    kill_calls: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "killpg", lambda pgid, sig: killpg_calls.append(pgid))
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: kill_calls.append(pid))

    errors: list[str] = []
    reaped = gc_execute_module._reap_processes_in_tree(tree, errors)

    assert reaped == [4242]
    assert kill_calls == [4242], "non-leader must be killed by exact pid"
    assert killpg_calls == [], "must NOT killpg a non-leader (out-of-tree collateral)"


def test_group_leader_in_tree_pid_is_killed_by_pid_not_by_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even a group leader is killed by exact PID only; group-leader status alone
    does not prove the whole process group is syncade-owned."""
    tree = tmp_path / "wt" / "run-x"
    tree.mkdir(parents=True)
    _fake_lsof(monkeypatch, tree, pid=5050)

    monkeypatch.setattr(gc_execute_module.os, "getpgid", lambda pid: pid)  # leader

    killpg_calls: list[int] = []
    kill_calls: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "killpg", lambda pgid, sig: killpg_calls.append(pgid))
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: kill_calls.append(pid))

    errors: list[str] = []
    reaped = gc_execute_module._reap_processes_in_tree(tree, errors)

    assert reaped == [5050]
    assert kill_calls == [5050], "leader must still be killed by exact pid"
    assert killpg_calls == [], "group-leader status alone must not trigger killpg"


def test_pid_recheck_accepts_stdout_pid_when_lsof_returns_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tree = tmp_path / "wt" / "run-x"
    tree.mkdir(parents=True)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof" and "-p" in argv:
            return SubprocessResult(returncode=1, stdout="6060\n", stderr="", duration_seconds=0.0)
        if argv[0] == "lsof":
            return SubprocessResult(returncode=0, stdout="6060\n", stderr="", duration_seconds=0.0)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(gc_execute_module.os, "killpg", lambda pgid, sig: None)

    errors: list[str] = []
    reaped = gc_execute_module._reap_processes_in_tree(tree, errors)

    assert reaped == [6060]
    assert killed == [6060]
    assert errors == []


def test_stale_lsof_pid_is_rechecked_and_not_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A PID from the initial lsof snapshot may exit and be reused before GC
    signals it; if the immediate per-PID cwd recheck no longer finds it under the
    tree, GC must skip it."""
    tree = tmp_path / "wt" / "run-x"
    tree.mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        if argv[0] == "lsof" and "-p" in argv:
            return SubprocessResult(returncode=1, stdout="", stderr="", duration_seconds=0.0)
        if argv[0] == "lsof":
            return SubprocessResult(returncode=0, stdout="6060\n", stderr="", duration_seconds=0.0)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)
    killpg_calls: list[int] = []
    kill_calls: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(gc_execute_module.os, "killpg", lambda pgid, sig: killpg_calls.append(pgid))
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: kill_calls.append(pid))

    errors: list[str] = []
    reaped = gc_execute_module._reap_processes_in_tree(tree, errors)

    assert reaped == []
    assert kill_calls == []
    assert killpg_calls == []
    assert any("-p" in call and "6060" in call for call in calls)
