"""Worktree name/path/registration plumbing.

Holds :class:`WorktreeError` plus the pure helpers that don't touch the
patched ``subprocess`` / ``datetime`` module globals: reviewer-name validation,
the strip-files deletion, and the stale-metadata structural check. Extracted
verbatim from ``WorktreeManager``'s static/helper methods (now module functions
called by the manager); behavior unchanged. ``worktree.py`` re-exports
``WorktreeError`` so ``syncade.worktree.WorktreeError`` is the same class.

This module stays a leaf — it must NOT import ``worktree`` (that's a one-way
dependency: ``worktree`` imports this).
"""

from __future__ import annotations

import os
from pathlib import Path


class WorktreeError(Exception):
    """Raised when worktree provisioning or cleanup fails.

    The message must include the underlying ``git`` error output when
    applicable so the CLI can surface it via exit code 60
    (``WORKTREE_ERROR``) without further introspection.
    """


def _validate_reviewer_name(reviewer_name: str) -> None:
    """Reject anything that isn't a plain basename.

    Raised at the top of :meth:`WorktreeManager.create` before any side effects
    so a malformed name (empty, ``"."``, ``".."``, separator-containing,
    or absolute) cannot make the worktree path resolve outside
    :attr:`WorktreeManager.run_dir`.
    """
    if (
        not reviewer_name
        or reviewer_name in (".", "..")
        or "/" in reviewer_name
        or "\\" in reviewer_name
        or Path(reviewer_name).is_absolute()
    ):
        raise WorktreeError(
            f"invalid reviewer_name {reviewer_name!r}: must be a plain "
            "basename (no separators, parent refs, or absolute paths)"
        )


def _strip_files(worktree_path: Path, names: list[str]) -> None:
    """Best-effort recursive deletion of named files in the worktree.

    Each entry must be a plain basename — entries containing a path
    separator, a parent-directory reference, or an absolute path
    are silently skipped so a malformed config cannot delete files
    outside the worktree. Missing files (and directories — only files
    and symlinks are unlinked) and unlink failures are also swallowed
    silently, matching the brief's "best-effort" rule.
    """
    strip_basenames: set[str] = set()
    for name in names:
        # Refuse to escape the worktree root. We silently skip
        # rather than raising, mirroring how missing files are
        # handled — both are "no eligible basename matches".
        #
        # A backslash is NOT an escape vector here (PR-h-02c item 2 / D2): on
        # POSIX it is a legal filename character that cannot separate paths,
        # so the `/`, `..`, and is_absolute() checks below already close every
        # way out of the worktree. Rejecting it bought nothing and silently
        # dropped a legitimate strip target — `back\slash.md` survived into
        # the reviewer worktree, which is the leak this PR exists to close.
        if not name or name in (".", "..") or "/" in name or Path(name).is_absolute():
            continue
        strip_basenames.add(name)
    if not strip_basenames:
        return

    for root, dirnames, filenames in os.walk(worktree_path):
        root_path = Path(root)
        for dirname in list(dirnames):
            target = root_path / dirname
            if dirname not in strip_basenames or not target.is_symlink():
                continue
            try:
                target.unlink()
            except OSError:
                pass
            dirnames.remove(dirname)
        for filename in filenames:
            if filename not in strip_basenames:
                continue
            target = root_path / filename
            try:
                if target.is_file() or target.is_symlink():
                    target.unlink()
            except OSError:
                # Best-effort — leave anything we couldn't remove.
                pass


def _registered_path_differs_from_actual(repo_root: Path, worktree) -> bool:
    """Structural check for the stale-metadata-drift
    case. Returns True iff git has a registration for this
    worktree name AND the ``gitdir`` file inside that
    registration points at a path different from the actual
    on-disk worktree path.

    Uses git's registration metadata rather than matching English stderr, so
    the check works regardless of git's message language (operator-locale
    environments, custom git locale settings).

    The check is best-effort: if the ``gitdir`` file can't be
    read for any reason (permissions, race, malformed
    contents), returns False — falling back to the
    no-recovery-attempted path. False negatives just mean we
    miss the recovery opportunity; we don't risk false
    positives that would trigger inappropriate recovery.

    See git-worktree(1) §"DETAILS" for the ``.git/worktrees/
    <name>/gitdir`` format: a single line whose contents are
    the absolute path to the worktree's ``.git`` file (i.e.
    ``<worktree_path>/.git``).
    """
    gitdir_file = repo_root / ".git" / "worktrees" / worktree.reviewer_name / "gitdir"
    if not gitdir_file.is_file():
        return False
    try:
        registered_text = gitdir_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    # ``gitdir`` contents point at ``<worktree_path>/.git``.
    # Compare resolved paths to handle symlink differences
    # (/tmp vs /private/tmp on macOS).
    try:
        registered = Path(registered_text).resolve(strict=False)
        expected = (worktree.path / ".git").resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return registered != expected
