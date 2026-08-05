"""Worktree strip escape guard — PR-h-02c item 3 (D2), audit rank 6 (R5).

``_strip_files`` rejected any name containing a backslash as a path-escape defence. On
POSIX a backslash is a legal filename character that CANNOT separate paths, so the ``/``,
``..``, and ``is_absolute()`` checks already close every way out of the worktree. The
backslash check bought nothing and silently dropped a legitimate strip target, leaving
``back\\slash.md`` in the reviewer's worktree — the exact leak this PR closes.

Narrowing a security guard is the risky direction, so the escape cases below are the
load-bearing tests: each asserts a file OUTSIDE the worktree still survives.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from syncade.worktree_paths import _strip_files


class TestLegitimateTargetsAreStripped:
    @pytest.mark.parametrize(
        "name",
        [
            "CLAUDE.md",
            "back\\slash.md",  # the shape D2 unblocks
            "naïve.md",
            'we"ird.md',
            "ta\tb.md",
            "my notes.md",
        ],
    )
    def test_target_is_removed_from_the_worktree(self, tmp_path, name):
        (tmp_path / name).write_text("SECRET\n")
        _strip_files(tmp_path, [name])
        assert not (tmp_path / name).exists()


class TestEscapeGuardStillHolds:
    """The property narrowing the guard must not break."""

    @pytest.mark.parametrize("name", ["../outside.md", "../../outside.md", "..", ".", ""])
    def test_nothing_outside_the_worktree_is_touched(self, tmp_path, name):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        victim = tmp_path / "outside.md"
        victim.write_text("DO NOT DELETE\n")
        (worktree / "keep.md").write_text("keep\n")

        _strip_files(worktree, [name])

        assert victim.exists(), f"{name!r} escaped the worktree"
        assert (worktree / "keep.md").exists()

    def test_absolute_path_target_survives(self, tmp_path):
        external = Path(tempfile.mkdtemp()) / "abs.md"
        external.write_text("DO NOT DELETE\n")
        _strip_files(tmp_path, [str(external)])
        assert external.exists()
