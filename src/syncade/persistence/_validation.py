"""Reviewer-name validation shared across persistence write paths.

Reviewer names are validated against a basename rule in
``WorktreeManager._validate_reviewer_name``; that rule guarantees
``<round_dir> / <reviewer_name>`` stays under ``round_dir``. Persistence
applies the same rule defensively in case a caller bypasses the
worktree manager.
"""

from __future__ import annotations

from pathlib import Path

_FORBIDDEN_NAME_CHARS = ("/", "\\")


def _validate_reviewer_filename_basename(name: str) -> None:
    """Refuse a name that would let the persistence path escape
    ``round_dir``.

    The worktree manager already enforces this for the per-reviewer
    directory under ``/tmp/syncade/``; we re-validate here so a caller
    that constructs a :class:`ReviewerRunResult` directly (no worktree
    involved) can't trick persistence into writing outside the run
    directory.
    """
    if (
        not name
        or name in (".", "..")
        or any(c in name for c in _FORBIDDEN_NAME_CHARS)
        or Path(name).is_absolute()
    ):
        raise ValueError(
            f"reviewer name {name!r} cannot be used as a persistence "
            f"basename (must be a plain basename, no separators, parent "
            f"refs, or absolute paths)"
        )
