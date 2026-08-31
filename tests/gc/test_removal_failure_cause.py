"""PR-h-06b item 3 — a failed delete carries its cause.

``shutil.rmtree(tree, ignore_errors=True)`` discards which path failed and why, so the
only signal left was ``tree.exists()`` afterwards and the report had to GUESS
(``permission denied?``). Measured, that guess is wrong for at least one case the
standard library gets right on its own: rmtree refuses a symlinked top entry with
``Cannot call rmtree on a symbolic link``, and ``ignore_errors`` swallows it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import syncade.gc_execute as gc_execute_module
from syncade.gc import execute_gc, plan_gc
from syncade.process import SubprocessResult
from syncade.workspace_owner import record_owner

from ._helpers import make_repo, write_run

RUN_ID = "run-fail"


@pytest.fixture
def _quiet_lsof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0
        ),
    )


def test_undeletable_child_is_reported_with_its_path_and_os_cause(
    tmp_path: Path, _quiet_lsof: None
) -> None:
    """The operator gets the failing path and the OS reason, not a guess."""
    repo = make_repo(tmp_path)
    write_run(repo, RUN_ID, final_exit_code=0)
    wt_base = tmp_path / "wt"
    tree = wt_base / RUN_ID
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "locked.txt").write_text("x", encoding="utf-8")
    (tree / "marker.bin").write_bytes(b"0" * 4096)
    record_owner(tree, repo)
    os.chmod(tree / "sub", 0o500)  # read-only dir: its children cannot be unlinked
    try:
        plan = plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
        report = execute_gc(plan, dry_run=False, repo_root=repo)

        assert tree.exists()
        assert report.worktrees_removed == []
        joined = " ".join(report.errors)
        # The FULL path, not just the basename: `_rmtree_safe_fd` unlinks fd-relatively,
        # so a raised exception would carry only "locked.txt" and satisfy a weaker
        # assertion while telling the operator nothing about which tree it was in.
        assert str(tree / "sub" / "locked.txt") in joined, report.errors
        assert "Permission denied" in joined, report.errors
        assert "permission denied?" not in joined, "the guess must be gone"
        # Everything deletable is still reclaimed; a tree kept at full size would recur
        # on every future GC.
        assert not (tree / "marker.bin").exists(), "deletable siblings must still go"
    finally:
        os.chmod(tree / "sub", 0o700)
