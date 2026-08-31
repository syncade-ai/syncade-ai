"""PR-h-06b item 1 — a liveness question that could not be answered refuses the delete.

``lsof`` answers "who has a cwd inside this tree". Before this, every way of FAILING to
get that answer — the tool absent, an error exit, a timeout, a failed per-pid recheck —
was rendered as "nobody is there", and the tree was removed anyway. The warning text said
so out loud: ``(still removing the directory)``.

The control test at the bottom is load-bearing: without it, "GC refused" and "GC had
nothing to remove" are indistinguishable from a passing suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import syncade.gc_execute as gc_execute_module
from syncade.gc import execute_gc, plan_gc
from syncade.process import SubprocessError, SubprocessNotFoundError, SubprocessResult
from syncade.workspace_owner import record_owner

from ._helpers import make_repo, write_run

RUN_ID = "run-live"


def _repo_with_owned_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A repo whose ``RUN_ID`` workspace is ownership-proven and GC-selectable."""
    repo = make_repo(tmp_path)
    write_run(repo, RUN_ID, final_exit_code=0, with_round=True)
    wt_base = tmp_path / "wt"
    tree = wt_base / RUN_ID
    tree.mkdir(parents=True)
    (tree / "marker").write_text("x", encoding="utf-8")
    record_owner(tree, repo)
    return repo, wt_base, tree


def _run_gc(repo: Path, wt_base: Path) -> object:
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert plan.worktree_trees_to_remove, "fixture must select the tree, else the test is vacuous"
    return execute_gc(plan, dry_run=False, repo_root=repo)


def _install_lsof(monkeypatch: pytest.MonkeyPatch, lsof):  # noqa: ANN001, ANN202
    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if argv[0] == "lsof":
            return lsof(argv)
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)


def test_absent_lsof_refuses_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool is not installed: GC cannot prove the tree is free, so it keeps it."""
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def lsof(argv):  # noqa: ANN001, ANN202
        raise SubprocessNotFoundError("lsof")

    _install_lsof(monkeypatch, lsof)

    report = _run_gc(repo, wt_base)

    assert tree.exists(), "a tree with no liveness proof must not be deleted"
    assert report.worktrees_removed == []
    assert any(str(tree) in err and "lsof" in err for err in report.errors), report.errors


def test_errored_lsof_refuses_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool is present but could not answer for this tree."""
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)
    _install_lsof(
        monkeypatch,
        lambda argv: SubprocessResult(
            returncode=1, stdout="", stderr="lsof: status error", duration_seconds=0.0
        ),
    )

    report = _run_gc(repo, wt_base)

    assert tree.exists()
    assert report.worktrees_removed == []
    assert any(str(tree) in err for err in report.errors), report.errors


def test_timed_out_lsof_refuses_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout is a failure to answer, not an answer of 'nobody'."""
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def lsof(argv):  # noqa: ANN001, ANN202
        raise SubprocessError("timed out")

    _install_lsof(monkeypatch, lsof)

    report = _run_gc(repo, wt_base)

    assert tree.exists()
    assert report.worktrees_removed == []


def test_failed_pid_recheck_refuses_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot named a live pid and the recheck could not confirm it.

    The recheck decides whether to SIGKILL that pid. Failing it used to skip the kill and
    then delete the tree anyway — the worst of both: the process survives and its cwd does
    not.
    """
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def lsof(argv):  # noqa: ANN001, ANN202
        if "-p" in argv:
            raise SubprocessError("recheck failed")
        return SubprocessResult(returncode=0, stdout="999999\n", stderr="", duration_seconds=0.0)

    _install_lsof(monkeypatch, lsof)

    report = _run_gc(repo, wt_base)

    assert tree.exists()
    assert report.worktrees_removed == []
    assert report.pids_reaped == []


def test_answered_empty_still_removes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTROL. lsof answered, and the answer was 'nobody' — removal proceeds.

    Without this, a fail-closed bug that refuses everything would pass the four tests
    above. ``lsof`` exits 1 with empty stderr when nothing matches; that is an ANSWER.
    """
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)
    _install_lsof(
        monkeypatch,
        lambda argv: SubprocessResult(returncode=1, stdout="", stderr="", duration_seconds=0.0),
    )

    report = _run_gc(repo, wt_base)

    assert not tree.exists(), "an answered-empty tree must still be reclaimed"
    assert report.worktrees_removed == [tree]
    assert report.errors == []


def test_no_pid_is_killed_when_a_later_recheck_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof is completed BEFORE anything is signalled.

    Killing as the loop goes means a failed recheck halfway through leaves processes
    dead in service of a deletion that then does not happen — and the report says
    ``0 process(es) reaped`` while a process was killed. GC may destroy a live process
    only as the price of a removal it is actually going to perform.
    """
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)
    killed: list[int] = []
    monkeypatch.setattr(gc_execute_module.os, "kill", lambda pid, sig: killed.append(pid))

    def lsof(argv):  # noqa: ANN001, ANN202
        if "-p" in argv:
            if "111" in argv:
                return SubprocessResult(
                    returncode=0, stdout="111\n", stderr="", duration_seconds=0.0
                )
            raise SubprocessError("recheck failed for the second pid")
        return SubprocessResult(returncode=0, stdout="111\n222\n", stderr="", duration_seconds=0.0)

    _install_lsof(monkeypatch, lsof)

    report = _run_gc(repo, wt_base)

    assert killed == [], "no process may be killed for a removal that will not happen"
    assert report.pids_reaped == []
    assert tree.exists()


def test_dry_run_predicts_the_same_refusal_as_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--gc-dry-run`` must predict what ``--gc`` does, including the refusals.

    A dry run that skips the per-pid recheck promises a removal the real run declines.
    """
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def lsof(argv):  # noqa: ANN001, ANN202
        if "-p" in argv:
            raise SubprocessError("recheck failed")
        return SubprocessResult(returncode=0, stdout="999999\n", stderr="", duration_seconds=0.0)

    _install_lsof(monkeypatch, lsof)
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)

    dry = execute_gc(plan, dry_run=True, repo_root=repo)

    assert dry.worktrees_removed == [], "dry run promised a removal execute refuses"
    assert dry.pids_reaped == []
    assert tree.exists()


# REMOVED: test_vanished_tree_is_not_blamed_on_lsof, with the guard it pinned.
# A first review round asked for a guard against asking lsof about a tree that had
# vanished (real lsof exits 1 with its whole usage block on stderr for a nonexistent
# path, which reads as "lsof errored"). A second round MEASURED that path unreachable
# through `execute_gc`: `_planned_tree_identity_still_matches` catches a tree that
# disappeared after planning first, with a better message. The guard was dead code
# whose only effect if it ever fired was to report a removal that never happened —
# appending the tree to `worktrees_removed` and dropping the PR-h-06a ownership claim.
# Its test called `_reap_and_remove_tree` directly, manufacturing the reachability it
# was meant to demonstrate. Both deleted.


def test_lsof_runs_with_warnings_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-w`` is on the argv, so lsof's warning channel cannot reach the refusal predicate.

    This pins the FLAG, which is all it can honestly pin. Measured on macOS (lsof 4.91)
    ``-w`` changes no observable output, so there is no behaviour here to assert against;
    it is kept for Linux, where ``+D``'s documented per-entry "can't stat()" warnings
    would otherwise trip ``returncode != 0 and stderr.strip()`` on a complete answer.
    Unverified on Linux — see :func:`~syncade.gc_execute._lsof_pids_in_tree`.
    """
    repo, wt_base, _tree = _repo_with_owned_tree(tmp_path)
    seen: list[list[str]] = []

    def lsof(argv):  # noqa: ANN001, ANN202
        seen.append(argv)
        return SubprocessResult(returncode=1, stdout="", stderr="", duration_seconds=0.0)

    _install_lsof(monkeypatch, lsof)
    _run_gc(repo, wt_base)

    assert seen, "the fixture must reach lsof"
    for argv in seen:
        assert "-w" in argv, argv


def test_unkillable_live_process_refuses_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process PROVEN to be in the tree, which GC is not permitted to kill.

    This is the inverse of the case above and strictly worse: "I could not get an
    answer" refuses, so "the answer was YES and I could not act on it" must refuse
    harder. ``PermissionError`` means the process is alive and unkillable (a setuid
    child that kept its cwd across the exec; another user's process on the world-shared
    default base). ``ProcessLookupError`` is the opposite — the process is already
    gone — and must still allow the removal.
    """
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def refuse_kill(pid, sig):  # noqa: ANN001, ANN202
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(gc_execute_module.os, "kill", refuse_kill)
    _install_lsof(
        monkeypatch,
        lambda argv: SubprocessResult(
            returncode=0, stdout="4242\n", stderr="", duration_seconds=0.0
        ),
    )

    report = _run_gc(repo, wt_base)

    assert tree.exists(), "a live process we cannot kill must keep its working directory"
    assert report.worktrees_removed == []
    assert report.worktrees_declined == [tree]
    # The operator-facing wording is part of the behaviour: it must name the tree and
    # both causes, and must NOT tell someone to install a tool that is already there
    # and answered correctly.
    joined = " ".join(report.errors)
    assert f"declined worktree tree {tree}" in joined, report.errors
    assert "could not be stopped" in joined, report.errors
    assert "cannot signal live pid 4242" in joined, report.errors


def test_already_dead_pid_does_not_block_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL for the test above. ``ProcessLookupError`` means the process is gone."""
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def gone(pid, sig):  # noqa: ANN001, ANN202
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(gc_execute_module.os, "kill", gone)
    _install_lsof(
        monkeypatch,
        lambda argv: SubprocessResult(
            returncode=0, stdout="4242\n", stderr="", duration_seconds=0.0
        ),
    )

    report = _run_gc(repo, wt_base)

    assert not tree.exists()
    assert report.worktrees_removed == [tree]


def test_declined_workspaces_are_reported_as_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal must be a first-class outcome, not an absence.

    Without it the summary an operator reads is byte-identical to a run that had
    nothing to remove: ``0 worktree(s) removed`` at exit 0, with the reason buried on
    stderr under a headline that contradicts it.
    """
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def lsof(argv):  # noqa: ANN001, ANN202
        raise SubprocessNotFoundError("lsof")

    _install_lsof(monkeypatch, lsof)

    report = _run_gc(repo, wt_base)

    assert report.worktrees_declined == [tree]
    assert report.worktrees_removed == []


def test_no_pid_is_killed_when_a_later_pid_is_unkillable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof must be complete before ANY signal, including the permission to signal.

    Hoisting the rechecks closed half of this; the ``EPERM`` refusal reopened the
    identical class one loop later — the first pid was really SIGKILLed, the refusal
    then discarded that fact, and the report read ``0 process(es) reaped`` over a
    destroyed process whose directory survived. ``os.kill(pid, 0)`` asks "may I signal
    this?" without signalling, so the whole set can be proven before any of it is acted
    on.
    """
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)
    killed: list[int] = []

    def kill(pid, sig):  # noqa: ANN001, ANN202
        if pid == 222:
            raise PermissionError(1, "Operation not permitted")
        if sig != 0:
            killed.append(pid)

    monkeypatch.setattr(gc_execute_module.os, "kill", kill)
    _install_lsof(
        monkeypatch,
        lambda argv: SubprocessResult(
            returncode=0,
            stdout="111\n" if "-p" in argv and "111" in argv else "111\n222\n",
            stderr="",
            duration_seconds=0.0,
        ),
    )

    report = _run_gc(repo, wt_base)

    assert killed == [], "nothing may be killed once any pid in the tree is unkillable"
    assert report.pids_reaped == []
    assert tree.exists()


def test_dry_run_predicts_an_unkillable_process_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--gc-dry-run`` must reach the EPERM refusal too, not just the lsof ones."""
    repo, wt_base, tree = _repo_with_owned_tree(tmp_path)

    def kill(pid, sig):  # noqa: ANN001, ANN202
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(gc_execute_module.os, "kill", kill)
    _install_lsof(
        monkeypatch,
        lambda argv: SubprocessResult(
            returncode=0, stdout="4242\n", stderr="", duration_seconds=0.0
        ),
    )
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)

    dry = execute_gc(plan, dry_run=True, repo_root=repo)

    assert dry.worktrees_removed == [], "dry run promised a removal execute refuses"
    assert dry.worktrees_declined == [tree]
    assert dry.pids_reaped == []


def test_declined_orphan_tree_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The orphan branch reports declines too — and it is where it matters most.

    An orphan has no run directory left, so it is re-selected and re-declined on every
    future GC forever. ``worktrees_declined`` is the only channel that names it on the
    stdout an operator reads.
    """
    repo = make_repo(tmp_path)
    wt_base = tmp_path / "wt"
    tree = wt_base / "orphan-run"
    tree.mkdir(parents=True)
    (tree / "marker").write_text("x", encoding="utf-8")
    record_owner(tree, repo)

    def lsof(argv):  # noqa: ANN001, ANN202
        raise SubprocessNotFoundError("lsof")

    _install_lsof(monkeypatch, lsof)
    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert plan.orphan_worktree_trees == [tree], "fixture must select an ORPHAN, else vacuous"

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert report.worktrees_declined == [tree]
    assert tree.exists()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof not installed")
def test_real_lsof_on_an_empty_tree_still_removes_it(tmp_path: Path) -> None:
    """The over-refusal control, against REAL lsof rather than a constructed result.

    Every other test here hands ``_lsof_pids_in_tree`` a ``SubprocessResult`` the author
    wrote, including the control — so a platform where a quiet, empty answer does not
    look the way this module assumes would refuse everything with the suite green. This
    repo has already had to revert one GC hardening that silently stopped reclaiming.
    Deliberately NOT ``@pytest.mark.smoke``: ``addopts = -m 'not smoke'`` would exclude
    it from the dev gate, the loop's test leg, and CI — and Linux CI is the platform
    whose lsof behaviour this module documents rather than measures.
    """
    repo = make_repo(tmp_path)
    write_run(repo, RUN_ID, final_exit_code=0, with_round=True)
    wt_base = tmp_path / "wt"
    tree = wt_base / RUN_ID
    tree.mkdir(parents=True)
    (tree / "marker").write_text("x", encoding="utf-8")
    record_owner(tree, repo)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert plan.worktree_trees_to_remove == [tree]
    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert not tree.exists(), "real lsof answered 'empty' and the tree must be reclaimed"
    assert report.worktrees_removed == [tree]
    assert report.worktrees_declined == []
