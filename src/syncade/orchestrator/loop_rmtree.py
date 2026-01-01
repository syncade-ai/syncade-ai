"""Guarded removal of resumed-run leftovers.

Split out of :mod:`syncade.orchestrator.loop` to keep that module under the
file-length cap. ``loop`` re-imports :func:`_safe_resume_rmtree` so
``run_review``'s call site — and the tests that monkeypatch it on the ``loop``
module — are unchanged. Its GC imports stay function-local for the same
import-cycle reason documented on ``loop._autoprune_old_transcripts``.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def _safe_resume_rmtree(target: Path, base: Path, repo_root: Path, *, reap: bool = False) -> None:
    """Remove a resumed-run leftover dir, but only after proving it is a
    real (non-symlink) directory strictly under ``base`` and not an
    ancestor of ``repo_root``.

    Mirrors the containment + ``(st_dev, st_ino)`` identity guards GC
    applies in :mod:`syncade.gc_execute`, reusing
    :func:`~syncade.gc_worktrees.tree_identity` and
    :func:`~syncade.gc_worktrees.tree_contains_repo_root`, so a swapped
    symlink or an out-of-base path can never redirect the delete. Any
    failed guard — including a missing target — is a safe no-op.

    When ``reap`` is true the proven-safe tree also has any in-cwd process
    reaped (:func:`~syncade.gc_execute.reap_processes_in_tree`) before the
    ``rmtree``, so the dir is never removed out from under a live subprocess
    — mirroring GC's worktree-tree removal. This is for EXTERNAL worktrees
    only (throwaway trees under the worktree base, where an orphaned
    subprocess is syncade's own). Persisted run-artifact dirs under
    ``.syncade/runs/`` are removed with ``reap=False``: GC never SIGKILLs
    inside ``.syncade/runs/``, and an operator may be inspecting them, so
    SIGKILL would over-reach (M2).
    """
    from syncade.gc_worktrees import tree_contains_repo_root, tree_identity

    identity = tree_identity(target)
    if identity is None:
        # Missing, a symlink, or unstatable → refuse.
        return
    try:
        resolved = target.resolve()
        base_resolved = base.resolve()
    except OSError:
        return
    if resolved == base_resolved:
        return
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        # Escapes the base (e.g. via a parent symlink) → refuse.
        return
    if tree_contains_repo_root(resolved, repo_root):
        return
    if tree_identity(resolved) != identity:
        # Identity changed between stat and removal → refuse (TOCTOU guard).
        return
    # Tree is proven safe. For external worktrees (reap=True) kill any in-cwd
    # process first, so we never delete out from under a live subprocess —
    # GC-equivalent. Run-artifact dirs pass reap=False: a plain guarded
    # rmtree, never SIGKILL (M2). Reap is reached
    # ONLY past every guard above, so a refused/out-of-base tree is untouched.
    if reap:
        from syncade.gc_execute import reap_processes_in_tree

        reap_processes_in_tree(resolved)
    shutil.rmtree(resolved, ignore_errors=True)
