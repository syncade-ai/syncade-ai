"""PR-h-06b item 4 — resume cleanup honours the same liveness proof GC does.

Item 1 made GC decline a workspace whose liveness ``lsof`` could not establish, but
left the public seam coercing that refusal back to an empty list, so this path — the
other place syncade deletes an external worktree — still deleted on an unanswered
question. Two implementations of one safety rule is how the installer came to need the
same fix twice (``CLAUDE.md``, PR-h-04.5).

The ``reap=False`` path is deliberately untouched: it removes run-artifact dirs under
``.syncade/runs/``, asks no liveness question, and must not start.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import syncade.gc_execute as gc_execute_module
from syncade.orchestrator.loop import _safe_resume_rmtree
from syncade.process import SubprocessError, SubprocessResult
from syncade.workspace_owner import record_owner


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Unowned external tree — for tests that probe refusal paths before ownership."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    base = tmp_path / "wt"
    tree = base / "run-x" / "round-0"
    tree.mkdir(parents=True)
    (tree / "marker").write_text("x", encoding="utf-8")
    return repo, base, tree


def _owned_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Owned external tree — for tests that must reach the liveness and rmtree paths.

    Uses a real git repo (required by git_common_dir) and records ownership so
    the PR-h-06a ownership check passes and the test actually exercises liveness /
    rmtree behaviour rather than refusing on missing record.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    base = tmp_path / "wt"
    tree = base / "run-x" / "round-0"
    tree.mkdir(parents=True)
    (tree / "marker").write_text("x", encoding="utf-8")
    # Ownership is recorded at the run root (tree.parent = base/"run-x"), not the
    # round dir itself — create_run_dir stamps the record one level above the round.
    record_owner(tree.parent, repo)
    return repo, base, tree


def test_unanswerable_lsof_keeps_the_external_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, tree = _owned_tree(tmp_path)

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        if "lsof" in argv:
            raise SubprocessError("lsof unavailable")
        raise SubprocessError(f"unexpected command: {argv}")

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    _safe_resume_rmtree(tree, base, repo, reap=True)

    assert tree.exists(), "resume cleanup must not delete on an unanswered question"


def test_answered_empty_still_removes_the_external_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTROL. An owned tree with an answered-empty lsof is still reclaimed.

    This is the positive control — the fix must not refuse every removal.
    Uses an owned tree (real git repo + record_owner) so the PR-h-06a
    ownership check passes and the liveness result is actually exercised.
    """
    repo, base, tree = _owned_tree(tmp_path)
    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=1, stdout="", stderr="", duration_seconds=0.0
        ),
    )

    _safe_resume_rmtree(tree, base, repo, reap=True)

    assert not tree.exists()


def test_run_artifact_path_never_asks_and_still_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reap=False`` asks no liveness question, so item 1's rule cannot reach it."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    runs_root = repo / ".syncade" / "runs"
    target = runs_root / "run-x" / "round-0"
    target.mkdir(parents=True)

    def explode(argv, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError(f"run-artifact cleanup must not shell out: {argv}")

    monkeypatch.setattr(gc_execute_module, "run_subprocess", explode)

    _safe_resume_rmtree(target, runs_root, repo, reap=False)

    assert not target.exists()


def test_resume_refuses_loudly_and_keeps_the_round_artifacts(
    repo_with_pr_doc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace that cannot be cleared stops the resume BEFORE anything is destroyed.

    The first version of item 4 abandoned the workspace silently, after the round's
    artifact directory had already been removed. The resumed round then failed at exit
    60 from a workspace manager refusing a pre-existing target — with the artifacts
    gone, and identically on every future resume, because nothing ever removes that
    tree. Order and voice are the fix: workspace first, and say so.
    """
    import subprocess

    from syncade.adapters.fake import FakeAdapter
    from syncade.config import SyncadeConfig
    from syncade.logging import Logger
    from syncade.orchestrator import run_review
    from syncade.orchestrator.resume import plan_resume
    from syncade.worktree import WorktreeError
    from tests.orchestrator._helpers import _factory_returning, _ship
    from tests.orchestrator._resume_fixtures import _prepare_aborted_run

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo_root,
        pr_doc,
        completed_round_count=1,
        max_rounds=2,
        aborted_round_partial=True,
        aborted_exit_code=40,
    )
    partial_round_dir = run_dir / "round-1"
    assert (partial_round_dir / "rv1.stdout").read_text() == "partial output"

    wt_base = tmp_path / "wt-base"
    leftover = wt_base / run_dir.name / "round-1"
    leftover.mkdir(parents=True)
    (leftover / "marker").write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: (_ for _ in ()).throw(SubprocessError("lsof unavailable")),
    )

    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 2},
        worktree_base=str(wt_base),
    )
    with pytest.raises(WorktreeError) as excinfo:
        run_review(
            repo_root=repo_root,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
            logger=Logger(level="quiet"),
            resume_plan=plan_resume(repo_root, run_dir),
            force_drift=True,
        )

    assert str(leftover) in str(excinfo.value)
    assert "lsof" in str(excinfo.value)
    assert leftover.exists(), "the workspace it could not prove free must be left alone"
    assert (partial_round_dir / "rv1.stdout").read_text() == "partial output", (
        "the round's artifacts must survive a refusal that happens before them"
    )


def test_guard_refused_symlink_target_is_not_reported_as_cleared(
    tmp_path: Path,
) -> None:
    """A symlink leftover at the workspace path must return False, not True.

    Before the fix, guard-refused existing targets all returned True — identical
    to "nothing in the way". A symlink at the exact resumed workspace path blocks
    re-provisioning just as a failed delete would; the caller must see False so it
    stops before destroying the round's artifacts.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    base = tmp_path / "wt"
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    symlink_target = base / "run-x" / "round-0"
    symlink_target.parent.mkdir(parents=True)
    symlink_target.symlink_to(real_dir)

    cleared = _safe_resume_rmtree(symlink_target, base, repo, reap=False)

    assert cleared is False, "a symlink at the workspace path must not be reported as cleared"
    assert symlink_target.is_symlink(), "the symlink must be left in place"


def test_truly_absent_target_is_reported_as_cleared(
    tmp_path: Path,
) -> None:
    """A genuinely absent target returns True — nothing blocks re-provisioning."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    base = tmp_path / "wt"
    base.mkdir()
    absent = base / "run-x" / "round-0"  # does not exist at all

    cleared = _safe_resume_rmtree(absent, base, repo, reap=False)

    assert cleared is True, "a truly absent target must be reported as cleared (nothing in the way)"


def test_a_failed_rmtree_is_not_reported_as_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Proven free" is not "removed". The liveness proof is only half the question.

    Item 3 replaced `ignore_errors=True` in GC because it discards the outcome. The
    sibling here kept it AND returned "cleared" unconditionally, so a workspace that
    could not actually be deleted reached the caller as a success — which then destroys
    the round's artifacts and fails provisioning at exit 60. Same regression item 4
    exists to close, reached by a different cause.
    """
    import os

    repo, base, tree = _owned_tree(tmp_path)
    (tree / "sub").mkdir()
    (tree / "sub" / "f.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=1, stdout="", stderr="", duration_seconds=0.0
        ),
    )
    os.chmod(tree / "sub", 0o500)
    try:
        cleared = _safe_resume_rmtree(tree, base, repo, reap=True)
    finally:
        os.chmod(tree / "sub", 0o700)

    assert tree.exists(), "precondition: the removal must actually have been blocked"
    assert cleared is False, "a tree that is still there must not be reported as cleared"


def test_unowned_external_worktree_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external worktree with no ownership record returns False even when
    path guards and liveness all pass.

    Resume cleanup must match GC's PR-h-06a ownership requirement: a recordless,
    foreign, or malformed tree must not be deleted. Before this fix, passing all
    path/identity/lsof guards was sufficient to proceed to rmtree regardless of
    whether the workspace record proves this repository owns the tree.
    """
    repo, base, tree = _tree(tmp_path)  # _tree creates no ownership record
    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=1, stdout="", stderr="", duration_seconds=0.0
        ),
    )

    cleared = _safe_resume_rmtree(tree, base, repo, reap=True)

    assert tree.exists(), "unowned tree must not be deleted"
    assert cleared is False, "unowned tree must return False — not cleared"


def test_absent_leaf_with_unowned_run_root_is_not_cleared(
    tmp_path: Path,
) -> None:
    """Absent round leaf + existing unowned run root returns False for reap=True.

    When the round-leaf is already gone but the run-root still exists without a
    valid ownership record, re-provisioning will be hard-refused by the workspace
    manager. Returning True here would cause run_review to destroy the round's
    artifacts and then fail at exit 60 — the same regression as returning True for
    a guard-refused existing leaf. The fix validates run-root ownership before
    declaring the slot clear.
    """
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    base = tmp_path / "wt"
    run_root = base / "run-x"
    run_root.mkdir(parents=True)  # run root exists but has no ownership record
    absent_leaf = run_root / "round-0"  # the round leaf was already cleaned up

    cleared = _safe_resume_rmtree(absent_leaf, base, repo, reap=True)

    assert cleared is False, (
        "an absent leaf under an unowned run root must return False — "
        "the run root blocks re-provisioning just as a live leaf would"
    )
