"""Worktree-tree selection for ``syncade --gc``.

Split from :mod:`syncade.gc` to keep both files under the blocking file-length
gate. Two concerns:

- :func:`existing_worktree_trees` — map run-ids being pruned to their on-disk
  ``/tmp/syncade/<run-id>/`` trees (those are provably this repo's: each maps to
  a run dir under this ``.syncade/runs/``).
- :func:`repo_owned_orphan_trees` — ``/tmp/syncade/<run-id>/`` trees whose run
  dir is GONE but which are STILL provably this repo's, because a live nested git
  worktree strictly under them is registered in this repo's ``git worktree
  list``. This is the ONLY safe way to clean a gone-run leftover off the shared
  worktree base without risking another repo's tree: a foreign repo's tree is
  not in our live worktree registry, so it is never matched. Stale registry
  strings are not trusted: the registered path must still exist, share this
  repo's git common dir, and live strictly under the candidate tree. Paths are
  ``resolve()``-d before comparison because git reports worktrees under
  ``/private/tmp`` on macOS while the base is ``/tmp`` (a symlink).
"""

from __future__ import annotations

from pathlib import Path

from syncade.process import SubprocessError, run_subprocess

_GIT_WORKTREE_LIST_TIMEOUT_SECONDS: float = 30.0


def existing_worktree_trees(worktree_base: Path, run_ids: list[str]) -> list[Path]:
    """Map slimmable run-ids to their on-disk worktree trees (existing only)."""
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


def repo_owned_orphan_trees(
    repo_root: Path, candidate_trees: list[Path], known_run_ids: set[str]
) -> list[Path]:
    """From ``candidate_trees`` (immediate subdirs of the worktree base), return
    those whose run-id is GONE (not in ``known_run_ids``) AND which are provably
    owned by ``repo_root`` — i.e. a worktree registered in this repo's
    ``git worktree list`` lives strictly under the tree. A tree we cannot prove
    is ours (e.g. another repo's, a stale registry path, or a stray dir) is LEFT
    (the brief's "when unsure, leave it")."""
    repo_resolved = _resolve_best_effort(repo_root)
    registered = _active_repo_worktree_paths(repo_root, repo_resolved)
    if not registered:
        return []
    orphans: list[Path] = []
    for sub in candidate_trees:
        if sub.name in known_run_ids:
            continue
        try:
            if sub.is_symlink():
                continue
        except OSError:
            continue
        try:
            sub_resolved = sub.resolve()
        except OSError:
            continue
        if tree_contains_repo_root(sub_resolved, repo_resolved):
            continue
        if any(reg != sub_resolved and _is_at_or_under(reg, sub_resolved) for reg in registered):
            orphans.append(sub)
    return sorted(orphans)


def tree_contains_repo_root(tree: Path, repo_root: Path) -> bool:
    """True when removing ``tree`` would remove the main checkout."""
    tree_resolved = _resolve_best_effort(tree)
    repo_resolved = _resolve_best_effort(repo_root)
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


def _active_repo_worktree_paths(repo_root: Path, repo_resolved: Path) -> set[Path]:
    """Registered worktree paths that still exist and belong to ``repo_root``.

    ``git worktree list`` can retain stale paths after a worktree directory is
    removed out-of-band. A stale path string under ``/tmp/syncade/<id>/`` is not
    proof that the *current* directory at ``<id>`` is ours; another repo or a
    human could have recreated that tree. Re-prove ownership through Git before
    using the registered path as GC authority.
    """
    repo_common_dir = _git_common_dir(repo_root)
    if repo_common_dir is None:
        return set()

    active: set[Path] = set()
    for path in _registered_worktree_paths(repo_root):
        resolved = _resolve_best_effort(path)
        if resolved == repo_resolved:
            continue
        if not _existing_non_symlink_dir(resolved):
            continue
        if _git_common_dir(resolved) == repo_common_dir:
            active.add(resolved)
    return active


def _registered_worktree_paths(repo_root: Path) -> set[Path]:
    """Resolved paths of every worktree registered in ``repo_root``'s
    ``git worktree list`` (best-effort: empty set if git is missing/errors or the
    dir is not a git repo)."""
    try:
        result = run_subprocess(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            timeout=_GIT_WORKTREE_LIST_TIMEOUT_SECONDS,
        )
    except SubprocessError:
        return set()
    if result.returncode != 0:
        return set()
    paths: set[Path] = set()
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            raw = line[len("worktree ") :].strip()
            try:
                paths.add(Path(raw).resolve())
            except OSError:
                paths.add(Path(raw))
    return paths


def _git_common_dir(path: Path) -> Path | None:
    try:
        result = run_subprocess(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=path,
            timeout=_GIT_WORKTREE_LIST_TIMEOUT_SECONDS,
        )
    except SubprocessError:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        common_dir = path / common_dir
    return _resolve_best_effort(common_dir)


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


def _resolve_best_effort(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path
