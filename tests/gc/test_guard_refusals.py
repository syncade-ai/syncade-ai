"""GC guard-refusal stdout path (PR-h-06b).

Guard refusals before ``_reap_and_remove_tree`` (repo-root containment,
identity mismatch since planning) must name their workspace on stdout, not
only in ``errors`` → stderr. Before the fix the summary showed
``0 worktrees removed`` with no path — indistinguishable from a clean run.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import syncade.gc_execute as gc_execute_module
from syncade.gc import GcPlan, execute_gc, plan_gc
from syncade.gc_worktrees import tree_identity
from syncade.process import SubprocessResult
from syncade.workspace_owner import record_owner

from ._helpers import make_repo, write_run


@pytest.fixture
def _fake_lsof_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0
        ),
    )


def test_guard_refused_identity_mismatch_appears_in_worktrees_refused(
    tmp_path: Path, _fake_lsof_empty
) -> None:
    """A worktree whose on-disk identity changed since planning lands in
    ``worktrees_refused``, not silently in ``errors`` only.

    Before the fix, the identity-mismatch guard only appended to ``errors``
    (stderr) and continued — the path never appeared in any stdout-rendered
    bucket, so the summary showed ``0 worktree(s) removed`` with no path,
    indistinguishable from a clean run.
    """
    repo = make_repo(tmp_path)
    write_run(
        repo,
        "run-changed",
        started_at=datetime.now(UTC) - timedelta(days=60),
        final_exit_code=0,
        with_round=True,
    )
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-changed"
    tree.mkdir(parents=True)
    record_owner(tree, repo)

    plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert tree in plan.worktree_trees_to_remove

    shutil.rmtree(tree)
    tree.mkdir(parents=True)
    record_owner(tree, repo)

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert tree in report.worktrees_refused, (
        "identity-mismatch guard refusal must appear in worktrees_refused"
    )
    assert tree not in report.worktrees_removed
    assert tree not in report.worktrees_declined


def test_guard_refused_repo_root_containment_appears_in_worktrees_refused(
    tmp_path: Path, _fake_lsof_empty
) -> None:
    """A worktree that contains the repo root lands in ``worktrees_refused`` on stdout.

    Before the fix, the repo-root-containment guard only appended to ``errors``
    (stderr) and continued — the path never appeared in any stdout-rendered bucket.
    """
    repo = make_repo(tmp_path)
    write_run(
        repo,
        "run-contains-repo",
        started_at=datetime.now(UTC) - timedelta(days=60),
        final_exit_code=0,
        with_round=True,
    )
    identity = tree_identity(repo)
    plan = GcPlan(
        protected_run_ids=[],
        runs_to_slim=[],
        worktree_trees_to_remove=[repo],
        orphan_worktree_trees=[],
        worktree_tree_identities={repo: identity} if identity else {},
    )

    report = execute_gc(plan, dry_run=False, repo_root=repo)

    assert repo in report.worktrees_refused, (
        "repo-root-containment guard refusal must appear in worktrees_refused"
    )
    assert repo not in report.worktrees_removed
    assert repo not in report.worktrees_declined
