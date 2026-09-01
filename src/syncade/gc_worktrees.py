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
import stat as _stat
from pathlib import Path
from typing import NamedTuple

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


class UnclaimableTrees(NamedTuple):
    """The inert report set, SPLIT by which of its two operator actions applies.

    The two halves are disjoint and are classified in the SAME pass that selects
    them, deliberately: re-deriving the split from the returned paths would make
    the label a second source of truth that can drift from the selection.

    ``recordless`` is proven — the tree was inspectable, it is syncade-shaped, and
    it has no ownership record. That state is permanent, so the action is manual
    removal. ``unreadable`` is an absence of knowledge, not a fact about the tree:
    some part of the classification (the record, the shape, or both) could not be
    read, and the tree is here only because its NAME matches a repo-local run. It
    may become reclaimable through the normal ownership-proven path once it is
    inspectable, so the action is to fix the permissions and rerun GC.
    """

    recordless: list[Path]
    unreadable: list[Path]

    @property
    def all_trees(self) -> list[Path]:
        """Both halves, sorted — the whole inert set, for sizing and counting."""
        return sorted([*self.recordless, *self.unreadable])


def unclaimable_trees(
    repo_root: Path, candidate_trees: list[Path], known_run_ids: set[str]
) -> UnclaimableTrees:
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
    recordless: list[Path] = []
    unreadable: list[Path] = []
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
            # lstat succeeded; verify the record is a readable regular file.
            # A FIFO would block read_bytes() forever; a symlink would be followed
            # even though owner_of() refuses symlink records — both must be treated
            # as unreadable rather than probed with read_bytes().
            record_path = sub / OWNER_RECORD_NAME
            try:
                lst = record_path.lstat()
            except OSError:
                if sub.name in known_run_ids:
                    unreadable.append(sub)
                continue
            if not _stat.S_ISREG(lst.st_mode):
                if sub.name in known_run_ids:
                    unreadable.append(sub)
                continue
            try:
                # Bounded probe: open and immediately close the descriptor.
                # read_bytes() would load the entire file with no size cap, which
                # can crash GC planning on a large or corrupt record before the
                # directory is proven syncade-shaped or repo-local.  Opening the
                # descriptor is sufficient to check read permission; the record's
                # contents are validated by owner_of() if the tree reaches the
                # ownership-proven removal path.
                _probe_fd = os.open(record_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                os.close(_probe_fd)
            except OSError:
                if sub.name in known_run_ids:
                    unreadable.append(sub)
                continue
            continue
        shape = _looks_like_syncade_workspace(sub)
        if shape is False:
            continue
        if shape is None and sub.name not in known_run_ids:
            continue
        # The split, decided HERE because this is where both states are known.
        # Only a tree whose record was READ as absent AND whose shape was READ as
        # syncade's is proven recordless; every other survivor reached this line
        # through an unreadable record, an unreadable shape, or both, and its
        # classification is unknown rather than settled.
        if record_state is False and shape is True:
            recordless.append(sub)
        else:
            unreadable.append(sub)
    return UnclaimableTrees(recordless=sorted(recordless), unreadable=sorted(unreadable))


def allocated_bytes(st: os.stat_result) -> int:
    """The disk a file actually occupies, in bytes — not its logical length.

    ``st_size`` is what the file CLAIMS to be; ``st_blocks`` is what the filesystem
    gave it, in POSIX-defined 512-byte units regardless of the filesystem's own
    block size. GC reports the second, because deleting a tree returns the second.
    Summing ``st_size`` understated this repo's own stranded corpus by 15.6% —
    1.81 GB against 2.14 GB of real disk — in the one number the v0.9.0 upgrade
    note asks an operator to act on.

    ``st_blocks`` is POSIX-only; on a platform without it the logical size is the
    honest fallback, since no better answer is available there.
    """
    blocks = getattr(st, "st_blocks", None)
    return st.st_size if blocks is None else blocks * 512


def tree_size_bytes(tree: Path) -> int | None:
    """Disk allocated to files under ``tree``, or ``None`` when traversal is incomplete.

    CEILING, stated because it is a real approximation: directory inodes' own
    allocated blocks are not counted. Measured on APFS they are zero and this
    function matches ``du -sk`` to the byte; on a filesystem that does allocate for
    directories the answer is low by roughly one block per directory, which against
    a workspace of many-KB files is far below the error this replaced. Counting
    them would mean sizing the root outside the recursion for a sub-percent effect.

    Hard-linked inodes are counted once **within this tree**, matching ``du`` for a
    single-tree walk and the disk reclaimed if this tree alone were removed.

    THE SCOPE IS THE GUARANTEE, and it is deliberately narrow. A caller that sums
    this function over SEVERAL roots gets no cross-root deduplication: an inode
    linked from two reported workspaces is counted once per tree. That is a known,
    accepted approximation, not an oversight — the promise this function makes is
    "never UNDERSTATES the disk this tree holds", not exact ``du`` parity over an
    arbitrary selection. Four review rounds were spent widening this before the
    guarantee was bounded instead, so treat a cross-root parity report as a request
    to re-open a decision rather than as a defect: what would change it is a
    measurement that real workspace trees share hard-linked inodes, not a fixture
    demonstrating the arithmetic.
    """
    return _tree_size_bytes(tree, set())


def _tree_size_bytes(tree: Path, seen: set[tuple[int, int]]) -> int | None:
    total = 0
    try:
        with os.scandir(tree) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_size = _tree_size_bytes(Path(entry.path), seen)
                    if child_size is None:
                        return None
                    total += child_size
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    inode_key = (st.st_dev, st.st_ino)
                    if inode_key in seen:
                        continue
                    seen.add(inode_key)
                    total += allocated_bytes(st)
    except OSError:
        return None
    return total
