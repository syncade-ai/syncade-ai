"""Run-protection checks shared by GC planning and execution."""

from __future__ import annotations

import json
from pathlib import Path

from syncade.gc_worktrees import repo_owned_orphan_trees
from syncade.orchestrator.resume import find_resumable_runs
from syncade.orchestrator.resume_types import _RESUMABLE_EXIT_CODES, LOOP_MANIFEST_FILENAME
from syncade.persistence import RUN_INIT_FILENAME


def protected_run_ids_for_gc(runs_root: Path, run_dirs: list[Path] | None = None) -> set[str]:
    """Return run IDs GC must not slim."""
    if run_dirs is None:
        run_dirs = safe_iter_subdirs(runs_root)

    try:
        protected = set(find_resumable_runs(runs_root))
    except OSError:
        protected = set()

    for run_dir in run_dirs:
        if gc_should_conservatively_protect(run_dir):
            protected.add(run_dir.name)
    return protected


def gc_should_conservatively_protect(run_dir: Path) -> bool:
    run_init = run_dir / RUN_INIT_FILENAME
    try:
        has_run_init = run_init.is_file()
    except OSError:
        return True
    if not has_run_init:
        return True

    loop_manifest = run_dir / LOOP_MANIFEST_FILENAME
    try:
        has_loop_manifest = loop_manifest.is_file()
    except OSError:
        return True
    if not has_loop_manifest:
        return True

    try:
        data = json.loads(loop_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True

    exit_code = data.get("final_exit_code")
    return isinstance(exit_code, int) and exit_code in _RESUMABLE_EXIT_CODES


def current_protected_run_ids(repo_root: Path) -> set[str]:
    return protected_run_ids_for_gc(repo_root / ".syncade" / "runs")


def run_id_protected_now(repo_root: Path, run_id: str, protected_run_ids: set[str]) -> bool:
    if run_id in protected_run_ids:
        return True
    return run_dir_protected_now(repo_root / ".syncade" / "runs" / run_id)


def run_dir_slimmable_now(run_dir: Path, run_id: str, protected_run_ids: set[str]) -> bool:
    """Re-checked at execution time, not just at plan time: a run that became
    resume-eligible between plan and execute must not be touched.

    A **symlinked run dir is refused outright**. ``plan_gc`` never selects one, so the
    only way to reach here with a symlink is a plan/execute race (or a stale plan):
    plan against the real ``.syncade/runs/<id>/``, swap it for a symlink to an
    external directory dressed up with valid run markers, then execute. Both
    reviewers reproduced exactly that, and it let GC unlink ``*.stdout`` outside the
    corpus. ``is_dir()`` follows symlinks, so it cannot be the check.
    """
    if run_id in protected_run_ids:
        return False
    if run_dir_protected_now(run_dir):
        return False
    try:
        if run_dir.is_symlink():
            return False
        return run_dir.is_dir()
    except OSError:
        return False


def run_dir_protected_now(run_dir: Path) -> bool:
    try:
        if not run_dir.is_dir():
            return False
    except OSError:
        return True
    return gc_should_conservatively_protect(run_dir)


def orphan_worktree_still_orphan_now(
    repo_root: Path, tree: Path, protected_run_ids: set[str]
) -> bool:
    run_id = tree.name
    if run_id in protected_run_ids:
        return False
    try:
        if (repo_root / ".syncade" / "runs" / run_id).exists():
            return False
    except OSError:
        return False
    runs_root = repo_root / ".syncade" / "runs"
    known_run_ids = {d.name for d in safe_iter_subdirs(runs_root)} | protected_run_ids
    return tree in repo_owned_orphan_trees(repo_root, [tree], known_run_ids)


def safe_iter_subdirs(path: Path) -> list[Path]:
    """Return immediate subdirectories, or ``[]`` on any ``OSError``."""
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    subdirs: list[Path] = []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                subdirs.append(entry)
        except OSError:
            continue
    return subdirs
