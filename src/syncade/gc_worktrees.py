"""Worktree-tree selection for ``syncade --gc``.

Split from :mod:`syncade.gc` to keep both files under the blocking file-length
gate. Two concerns:

- :func:`repo_owned_existing_trees` — map run-ids being pruned to their on-disk
  ``/tmp/syncade/<run-id>/`` trees, requiring positive ownership proof.  A
  same-name tree owned by another repository (or carrying no record) is skipped.
  :func:`existing_worktree_trees` is the lower-level helper without that filter.
- :func:`repo_owned_orphan_trees` — ``/tmp/syncade/<run-id>/`` trees whose run
  dir is GONE but which are STILL provably this repo's, because the tree carries
  an ownership record naming this repo's git common dir and trusted repo state
  hard-links that exact record file. This is the ONLY safe
  way to clean a gone-run leftover off the shared worktree base without risking
  another repo's tree: a foreign repo's tree carries a different record, or
  none, and is never matched.

  The proof used to be a live nested git worktree registered in this repo's
  ``git worktree list``. PR-h-05 deliberately destroyed that evidence — a
  reviewer export has no ``.git`` and the producer's store is standalone — which
  narrowed the proof to the trusted test/check legs and left an orphan of the
  ordinary shape on disk forever. The record restores the proof without
  restoring the operator-path breadcrumb the isolation removed: it is a claim
  this repository WROTE into the tree, not a linkage git can follow back to us.
  It also removes the last reason this module ran a subprocess beyond deriving
  the repository identity shared with the writer.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from syncade.workspace_owner import (
    OWNER_RECORD_NAME,
    git_common_dir,
    owner_of,
    resolve_best_effort,
    workspace_claim_matches,
)

_ROUND_DIR_RE = re.compile(r"^round-\d+$")


def existing_worktree_trees(worktree_base: Path, run_ids: list[str]) -> list[Path]:
    """Map slimmable run-ids to their on-disk worktree trees (existing only).

    Does NOT verify ownership.  Use :func:`repo_owned_existing_trees` for the
    GC-destructive path; this helper is for diagnostics and lower-level callers.
    """
    trees: list[Path] = []
    for run_id in run_ids:
        tree = worktree_base / run_id
        try:
            if tree.is_symlink():
                continue
            if tree.is_dir():
                trees.append(tree)
        except OSError:
            # Unreadable worktree base (permission-denied) — skip; never abort.
            continue
    return trees


def repo_owned_existing_trees(
    repo_root: Path, worktree_base: Path, run_ids: list[str]
) -> list[Path]:
    """Map slimmable run-ids to owned on-disk trees, requiring positive ownership proof.

    Every returned tree carries an ownership record naming ``repo_root`` AND a
    hard-linked claim in trusted repo state.  A same-name tree owned by another
    repository, or one with no record, is not returned — the worktree base is
    shared across repositories, so name alone is not sufficient evidence.
    """
    repo_common = git_common_dir(repo_root)
    if repo_common is None:
        return []
    trees: list[Path] = []
    for run_id in run_ids:
        tree = worktree_base / run_id
        try:
            if tree.is_symlink():
                continue
            if not tree.is_dir():
                continue
        except OSError:
            continue
        if tree_contains_repo_root(tree, repo_root):
            continue
        if owner_of(tree) == repo_common and workspace_claim_matches(repo_root, tree):
            trees.append(tree)
    return trees


def repo_owned_orphan_trees(
    repo_root: Path, candidate_trees: list[Path], known_run_ids: set[str]
) -> list[Path]:
    """Immediate subdirs of the worktree base whose run is GONE and whose
    ownership record names ``repo_root``.

    Ownership used to be proven by finding a worktree registered in this repo's
    ``git worktree list`` under the tree. PR-h-05 deliberately destroyed that
    evidence — a reviewer export has no ``.git`` and the producer's store is
    standalone — so the proof narrowed to the trusted test/check legs and an
    orphan of the ordinary shape was left forever. The record restores the
    proof without restoring the operator-path breadcrumb the isolation removed:
    it is a claim this repository WROTE, not a linkage git can follow back.

    Ownership requires one record with two links: the workspace record must name
    this repo's common dir, AND trusted repo state must hard-link that exact
    record inode. A forged or replacement record can copy the bytes and run id,
    but it is still a different file and therefore cannot reuse a stale claim.

    A tree we cannot prove is ours is still LEFT. That is the whole safety
    property, and it now fails safe in one more way than before: an unreadable
    or absent record reads as "not ours", and a record without a matching claim
    file also reads as "not ours".
    """
    repo_common = git_common_dir(repo_root)
    if repo_common is None:
        return []
    repo_resolved = resolve_best_effort(repo_root)
    orphans: list[Path] = []
    for sub in candidate_trees:
        if sub.name in known_run_ids:
            continue
        try:
            if sub.is_symlink():
                continue
            sub_resolved = sub.resolve()
        except OSError:
            continue
        if tree_contains_repo_root(sub_resolved, repo_resolved):
            continue
        if owner_of(sub) == repo_common and workspace_claim_matches(repo_root, sub):
            orphans.append(sub)
    return sorted(orphans)


def tree_contains_repo_root(tree: Path, repo_root: Path) -> bool:
    """True when removing ``tree`` would remove the main checkout."""
    tree_resolved = resolve_best_effort(tree)
    repo_resolved = resolve_best_effort(repo_root)
    return _is_at_or_under(repo_resolved, tree_resolved)


def tree_identity(tree: Path) -> tuple[int, int, int] | None:
    """Best-effort identity token for a non-symlink directory.

    Includes ``st_ctime_ns`` alongside ``(st_dev, st_ino)`` because inode
    numbers are reused on Linux: a tree deleted and recreated between GC
    planning and execution can land on the SAME inode, so ``(st_dev, st_ino)``
    alone would still match and GC would delete the replacement. A recreated
    directory has a newer change-time, so including ``st_ctime_ns`` catches the
    swap. (macOS does not reuse the inode in this window, which is why the gap
    only surfaced on Linux/CI.) In the normal plan→execute window nothing
    touches the tree, so ``st_ctime_ns`` is stable and legitimate removals still
    proceed.
    """
    try:
        if tree.is_symlink():
            return None
        stat = tree.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_ctime_ns)


def _is_at_or_under(path: Path, ancestor: Path) -> bool:
    """True if ``path`` is ``ancestor`` or a descendant of it."""
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _existing_non_symlink_dir(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        return path.is_dir()
    except OSError:
        return False


def _has_record_file(sub: Path) -> bool | None:
    """Whether the ownership-record path exists, or ``None`` if inspection failed.

    ``owner_of`` returns ``None`` for both absent records AND malformed/untrusted
    ones, so callers that need to distinguish "no record at all" from "record
    present but untrusted" must check separately.  Using ``lstat`` avoids
    following a symlink placed at the record path. Permission and I/O failures
    are not absence: callers must preserve that unknown state.
    """
    try:
        (sub / OWNER_RECORD_NAME).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _looks_like_syncade_workspace(sub: Path) -> bool | None:
    """Whether ``sub`` has a ``round-N`` child, or ``None`` if unreadable.

    A pre-registry syncade workspace root always has this structure.  Arbitrary
    sibling directories under a shared ``worktree_base`` do not, so this test
    prevents unrelated directories from appearing in Item 5 of the GC report.
    """
    try:
        return any(
            _ROUND_DIR_RE.match(child.name)
            for child in sub.iterdir()
            if not child.is_symlink() and child.is_dir()
        )
    except OSError:
        return None


def unclaimable_trees(
    repo_root: Path, candidate_trees: list[Path], known_run_ids: set[str]
) -> list[Path]:
    """Workspace trees eligible for the inert manual-cleanup/inspection report.

    Inspectable entries are syncade-shaped trees with no ownership-record file.
    They may predate the registry or survive a best-effort record-write failure;
    no repository can prove they are its own, so GC will never reclaim them. A
    matching repo-local run directory does not change that: the normal removal
    path also requires positive ownership proof. ``--gc`` therefore reports
    recordless workspaces whether or not their run artifacts still exist.

    Two kinds of directory are deliberately excluded:

    - Inspectable trees with a record file (valid or malformed): a malformed
      record is not the same as no record, and reporting such a tree as
      "recordless" would misstate the situation.
    - Directories with no ``round-N`` structure: arbitrary siblings of
      worktree trees that were never syncade workspaces.  Reporting them as
      syncade-managed orphans would point operators at data they should not
      touch.

    An unreadable tree is included only when its name matches this repository's
    run artifacts. That known-run evidence is enough to report a non-destructive
    warning on this run, but never enough to authorize removal. Its
    ownership-record and shape states remain unknown rather than being misread
    as absent or false; after it becomes inspectable, a later GC may classify it
    through the normal ownership-proven path.

    A tree recorded to ANOTHER repository is also not here. It is not
    unclaimable; it is claimed, by someone else, and reporting a stranger's disk
    as our unfinished business would be a different kind of untrue.
    """
    orphans_we_own = set(repo_owned_orphan_trees(repo_root, candidate_trees, known_run_ids))
    unclaimable: list[Path] = []
    for sub in candidate_trees:
        if sub in orphans_we_own:
            continue
        try:
            if sub.is_symlink():
                continue
        except OSError:
            continue
        if tree_contains_repo_root(sub, repo_root):
            continue
        # Skip an inspectable record path. ``None`` is not absence: an
        # unreadable known-run tree still needs an honest warning.
        record_state = _has_record_file(sub)
        if record_state is True:
            continue
        shape = _looks_like_syncade_workspace(sub)
        if shape is False:
            continue
        if shape is None and sub.name not in known_run_ids:
            continue
        unclaimable.append(sub)
    return sorted(unclaimable)


def tree_size_bytes(tree: Path) -> int | None:
    """Total file size under ``tree``, or ``None`` when traversal is incomplete."""
    total = 0
    try:
        with os.scandir(tree) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_size = tree_size_bytes(Path(entry.path))
                    if child_size is None:
                        return None
                    total += child_size
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    except OSError:
        return None
    return total
