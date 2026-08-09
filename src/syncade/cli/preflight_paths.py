"""Refuse unusable write targets BEFORE auto-init mutates the operator's directory.

Both directories checked here are created later — ``worktree_base`` by the loop's worktree
provisioning, ``.syncade/runs/`` by the run-dir mkdir. Left alone, an unusable one surfaces
as a ``WorktreeError``/exit-60 or an uncaught ``PermissionError`` *after* the baseline commit,
so a refused run would have created a repository on its way to failing. Checking here keeps
that refusal free.

Split out of ``cli/__init__.py``: it is one self-contained concern (*can syncade write where
it is about to write?*) with one output, and the dogfood panel called out `_run` for holding
validation, mutation, dispatch and cleanup at once.

``exists()`` follows symlinks and reports a DANGLING link as absent, which would read as
"absent but creatable" and let a broken link silently become a creation target. Every check
below tests ``is_symlink()`` first for that reason.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from syncade.exit_codes import CONFIG_ERROR, WORKTREE_ERROR


def _unusable(path: Path, *, need_read: bool = False) -> str | None:
    """Why ``path`` cannot be written to, as a message SUFFIX, or ``None`` if it can.

    The suffix carries its own separator so callers concatenate rather than choosing one.
    """
    if path.is_symlink() and not path.exists():
        return " is a dangling symlink"
    if path.exists() and not path.is_dir():
        return " exists but is not a directory"
    if path.is_dir():
        if need_read and not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            return ": directory is not readable and writable"
        if not need_read and not os.access(path, os.W_OK | os.X_OK):
            return ": directory is not writable"
    return None


def check_write_targets(worktree_base: Path, repo_root: Path) -> int | None:
    """Return an exit code if a write target is unusable, else ``None``.

    ``worktree_base`` problems are CONFIG errors (the operator configured the path);
    ``.syncade`` problems are environment errors, matching where each value comes from.
    """
    reason = _unusable(worktree_base)
    if reason is not None:
        print(
            f"[syncade] config error: worktree-base {str(worktree_base)!r}{reason}",
            file=sys.stderr,
        )
        return CONFIG_ERROR

    # worktree_base is created on demand, so an absent one shifts the question to its parent.
    if not worktree_base.exists():
        parent = worktree_base.parent
        if not parent.exists():
            print(
                f"[syncade] config error: worktree-base {str(worktree_base)!r}: "
                f"parent directory {str(parent)!r} does not exist",
                file=sys.stderr,
            )
            return CONFIG_ERROR
        if not parent.is_dir():
            print(
                f"[syncade] config error: worktree-base {str(worktree_base)!r}: "
                f"parent {str(parent)!r} exists but is not a directory",
                file=sys.stderr,
            )
            return CONFIG_ERROR
        if not os.access(parent, os.W_OK | os.X_OK):
            print(
                f"[syncade] config error: worktree-base {str(worktree_base)!r}: "
                f"parent directory {str(parent)!r} is not writable",
                file=sys.stderr,
            )
            return CONFIG_ERROR

    syncade_dir = repo_root / ".syncade"
    # runs/ needs READ too: resume and the run-id collision loop enumerate it.
    for path, need_read in ((syncade_dir, False), (syncade_dir / "runs", True)):
        reason = _unusable(path, need_read=need_read)
        if reason is not None:
            print(f"[syncade] error: {str(path)!r}{reason}", file=sys.stderr)
            return WORKTREE_ERROR
    return None
