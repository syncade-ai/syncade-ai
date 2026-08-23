"""Tier-3 selection must not depend on an actor workspace being a linked Git worktree.

PR-h-05 changed what lives under ``<worktree_base>/<run-id>/``: reviewers get a Git-less
filesystem export and the producer gets a STANDALONE repository. Neither is a linked worktree
any more; only the trusted test/check legs still are. GC has two selection paths and they are
affected differently, so both are pinned here.

The orphan half records a REAL, deliberate narrowing rather than a bug. ``repo_owned_orphan_trees``
proves ownership by finding a worktree registered in this repo's ``git worktree list`` under the
tree — and PR-h-05's D5 ("standalone means no shared storage or path breadcrumbs") deliberately
destroys exactly that evidence. The two goals are in direct tension and the isolation wins: an
ownership breadcrumb is the operator-path linkage this PR exists to remove. GC's own rule already
covers the outcome — "a tree we cannot prove is ours is LEFT" — so this fails safe, toward leaked
disk rather than deleted data.

It is also nearly unreachable: an orphan is a run whose ``.syncade/runs/<id>/`` is GONE, and tier 1
is never deleted, so only a hand-deletion produces one. Everything else is reclaimed by the normal
path below, which is shape-agnostic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from syncade.gc_worktrees import existing_worktree_trees, repo_owned_orphan_trees


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
    )


def _run_workspace(base: Path, run_id: str) -> Path:
    """The post-PR-h-05 shape: a Git-less reviewer export plus a standalone producer repo."""
    tree = base / run_id
    export = tree / "reviewer-1"
    export.mkdir(parents=True)
    (export / "app.py").write_text("x = 1\n")
    assert not (export / ".git").exists(), "a reviewer export must supply no repository"
    _git_init(tree / "producer-worktree" / "producer")
    return tree


def test_the_normal_path_selects_a_run_whatever_shape_its_workspaces_are(tmp_path):
    """Selection is by ``<worktree_base>/<run-id>``, so tier 3 covers exports and standalone
    repositories without knowing anything about either."""
    base = tmp_path / "wtbase"
    tree = _run_workspace(base, "run-1")
    assert existing_worktree_trees(base, ["run-1"]) == [tree]


def test_an_orphan_of_the_new_shape_is_left_rather_than_guessed_at(tmp_path):
    """Unprovable ownership must LEAVE the tree — never delete on a name match.

    ``<worktree_base>`` is shared across repos and its default is ``/tmp/syncade``; removing a
    directory because its NAME looks like a run id would let one repo's GC delete another's
    workspace, or a stray directory a human put there.
    """
    base = tmp_path / "wtbase"
    _run_workspace(base, "run-orphan")
    operator = tmp_path / "operator"
    _git_init(operator)

    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    assert candidates, "fixture must produce a candidate"
    assert repo_owned_orphan_trees(operator, candidates, known_run_ids=set()) == []


def test_an_orphan_is_still_reclaimed_when_a_trusted_worktree_pins_it(tmp_path):
    """The orphan path is narrowed, not dead: a run with a test/check leg still registers a
    linked worktree, and that remains provable ownership."""
    base = tmp_path / "wtbase"
    tree = _run_workspace(base, "run-orphan")
    operator = tmp_path / "operator"
    _git_init(operator)
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(tree / "test-worktree")],
        cwd=operator,
        check=True,
        capture_output=True,
    )

    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    assert repo_owned_orphan_trees(operator, candidates, known_run_ids=set()) == [tree]
