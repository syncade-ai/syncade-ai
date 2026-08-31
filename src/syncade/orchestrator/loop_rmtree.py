"""Guarded removal of resumed-run leftovers.

Split out of :mod:`syncade.orchestrator.loop` to keep that module under the
file-length cap. ``loop`` re-imports :func:`_safe_resume_rmtree` so
``run_review``'s call site — and the tests that monkeypatch it on the ``loop``
module — are unchanged. Its GC imports stay function-local for the same
import-cycle reason documented on ``loop._autoprune_old_transcripts``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _genuinely_absent(path: Path) -> bool:
    """Whether nothing is at ``path`` — as opposed to "I could not look".

    ``Path.exists()`` conflates the two. It SWALLOWS ``ENOTDIR``, ``ELOOP`` and
    ``EBADF`` and answers False, so a regular file planted where a run root belongs
    makes the round dir beneath it read as absent; the caller is told nothing is in the
    way, destroys the round's artifacts, and only then has provisioning refuse the
    obstruction that was there all along. (``EACCES`` it re-raises, which the caller
    already handled — but a guard that is right for one errno and wrong for three is
    the enumeration this repo keeps paying for.)

    Only ``ENOENT`` means absent. Every other error means something is there.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _pinned_under_base(target: Path, base: Path) -> Path | None:
    """``target`` rebuilt under the RESOLVED base, with every component BELOW the base
    proven not to be a symlink — or ``None`` when it is not safely addressable.

    This replaces resolving the target and then asking whether the ANSWER is still under
    the base. That test passes for a run root which is a symlink to a SIBLING run root
    inside the same base: the resolved path is under the base, it does not contain the
    repo root, and the sibling is owned by this same repository — so every guard agrees
    and the sibling's directory is deleted while the caller is told the resumed path was
    cleared. Both blind reviewers reproduced it, on the workspace path and on the
    ``.syncade/runs/`` artifact path, where the victim is tier-1 run history.

    The cure is the one ``CLAUDE.md`` records for ``workspace_owner``: do not derive the
    answer and then defend it against spellings. With no component below the base allowed
    to be a symlink, the lexical path and the real path are the same path by
    construction, and there is nothing left to enumerate.

    **The ceiling, stated rather than defended.** This closes a symlink PLANTED below the
    base — a deterministic redirect needing no timing at all, which is what both blind
    reviewers reproduced. It does NOT close a same-user process that swaps a component
    between this walk and the delete. That window was chased for four producer commits
    (an ``os.fwalk`` deletion, an anchored-fd success oracle, dir-symlink unlinking); the
    machinery grew this module from 150 to 311 lines, regressed ordinary deletion once —
    a directory symlink inside a normal tree made cleanup fail, found unanimously — and
    the window was still reported open. Reverted, on the precedent ``CLAUDE.md`` records
    for ``create_run_dir``: six commits, a unanimous panel after 611 lines, reverted, and
    the rule that came out of it — *a narrow guarantee stated plainly beats a broad one
    defended by a growing list of windows.* A same-user local racer is outside the wave's
    threat model, and is the same attacker PR-h-06a already declined to defend against.

    **The base itself IS resolved, deliberately.** ``DEFAULT_WORKTREE_BASE`` is
    ``/tmp/syncade`` and ``/tmp`` is a symlink to ``/private/tmp`` on stock macOS, so
    refusing symlinks at or above the base would disable the default configuration —
    the same trap that silently turned off the PR-h-06a ownership record.
    """
    try:
        rel = target.relative_to(base)
    except ValueError:
        return None  # not lexically under the base (a `..`, or another root entirely)
    if not rel.parts or ".." in rel.parts:
        return None
    try:
        # NOT strict: on a first resume the worktree base may not exist yet, and a
        # missing base means a missing target — genuinely nothing in the way. Requiring
        # it to exist turned that safe no-op into a refusal that failed the resume
        # (caught by two existing tests, not by the new ones).
        pinned = base.resolve()
    except OSError:
        return None
    for part in rel.parts:
        pinned = pinned / part
        if pinned.is_symlink():
            return None
    return pinned


def _safe_resume_rmtree(target: Path, base: Path, repo_root: Path, *, reap: bool = False) -> bool:
    """Remove a resumed-run leftover dir, but only after proving it is a
    real (non-symlink) directory strictly under ``base`` and not an
    ancestor of ``repo_root``.

    Mirrors the containment + ``(st_dev, st_ino)`` identity guards GC
    applies in :mod:`syncade.gc_execute`, reusing
    :func:`~syncade.gc_worktrees.tree_identity` and
    :func:`~syncade.gc_worktrees.tree_contains_repo_root`. Redirection is prevented by
    :func:`_pinned_under_base` rather than by inspecting a resolved path — see there for
    why the earlier spelling let a symlinked run root redirect the delete to a sibling. A missing
    target is a safe no-op (returns ``True``); a guard-refused existing target
    returns ``False`` so the caller stops before destroying anything else.

    For external worktrees (``reap=True``), PR-h-06a ownership is also required:
    :func:`~syncade.workspace_owner.owner_of` plus
    :func:`~syncade.workspace_owner.workspace_claim_matches` are checked against
    the RUN ROOT (``target.parent`` — the ``<base>/<run_id>`` level where
    :func:`~syncade.workspace_owner.create_run_dir` stores the record) before any
    removal. A recordless, foreign, or malformed run root returns ``False`` even
    when every path/identity guard passes.

    When ``reap`` is true the proven-safe tree also has any in-cwd process
    reaped (:func:`~syncade.gc_execute.reap_processes_in_tree`) before the
    ``rmtree``, and — mirroring GC since PR-h-06b item 4 — the removal is ABANDONED
    when that check could not answer, rather than deleting on the assumption that
    nobody is there.

    Returns ``True`` when the target is GONE or was genuinely absent — either
    outcome leaves no obstacle for re-provisioning. Returns ``False`` for
    everything else: a liveness abandonment, a removal that failed, or a
    guard-refused EXISTING target (a symlink, an out-of-base path, a path that
    contains the repo root, or a path whose identity changed between stat and
    delete). A guard refusal does not destroy anything, but an existing target
    blocks re-provisioning exactly as a failed delete would — so returning
    ``True`` there is the same regression as abandoning silently: the caller
    would proceed to destroy the round's artifacts and only then fail at exit
    60 from workspace provisioning. A missing target IS safe (nothing in the
    way); a symlink leftover at the exact resumed workspace path IS NOT — every
    workspace manager hard-refuses a pre-existing target. This is for EXTERNAL
    worktrees only (throwaway trees under the worktree base, where an orphaned
    subprocess is syncade's own). Persisted run-artifact dirs under
    ``.syncade/runs/`` are removed with ``reap=False``: GC never SIGKILLs
    inside ``.syncade/runs/``, and an operator may be inspecting them, so
    SIGKILL would over-reach (M2).
    """
    from syncade.gc_worktrees import tree_contains_repo_root, tree_identity

    safe = _pinned_under_base(target, base)
    if safe is None:
        # Not addressable without following a symlink below the base. Whatever is at
        # that path is still there and still blocks re-provisioning, so this is a
        # refusal, never a no-op.
        return False

    identity = tree_identity(safe)
    if identity is None:
        # Could be missing (safe — nothing blocks re-provisioning) OR a symlink /
        # unstatable entry that EXISTS at the path. Only return True when the path
        # is genuinely absent; any detectable presence means something is in the way.
        absent = _genuinely_absent(safe)
        if not absent:
            return False
        # Target leaf is genuinely absent. For external worktrees (reap=True) the
        # run root may still exist even when its round child is gone — a partial
        # cleanup, an interrupted previous attempt, or a foreign tree planted at the
        # same path. If the run root exists but is unowned or foreign, workspace
        # provisioning will hard-refuse it, so this is NOT a safe no-op: returning
        # True here causes run_review to destroy the round's artifacts and then fail
        # at exit 60 from provisioning — identical to the regression this function
        # was fixed to prevent. Validate ownership of the run root when it exists.
        if reap:
            from syncade.workspace_owner import git_common_dir, owner_of, workspace_claim_matches

            run_root = safe.parent
            run_root_present = not _genuinely_absent(run_root)
            if run_root_present:
                repo_common = git_common_dir(repo_root)
                if (
                    repo_common is None
                    or owner_of(run_root) != repo_common
                    or not workspace_claim_matches(repo_root, run_root)
                ):
                    return False
        return True
    if tree_contains_repo_root(safe, repo_root):
        # Contains repo root → guard refused.
        return False
    if tree_identity(safe) != identity:
        # Identity changed between the two stats → TOCTOU guard refused. Narrow by
        # construction now that both reads name the same pinned path.
        return False
    # Tree is proven safe against the path guards. For external worktrees (reap=True)
    # verify PR-h-06a ownership before touching anything: a recordless, foreign, or
    # malformed tree must not be deleted even when path/identity guards pass. GC
    # enforces the same requirement via repo_owned_existing_trees / owner_of /
    # workspace_claim_matches; resume must match it.
    if reap:
        from syncade.workspace_owner import git_common_dir, owner_of, workspace_claim_matches

        # Ownership is recorded at the RUN root (<base>/<run_id>), one level above
        # the round dir (<base>/<run_id>/round-N). create_run_dir stamps the record
        # there; owner_of and workspace_claim_matches read it from that directory.
        run_root = safe.parent
        repo_common = git_common_dir(repo_root)
        if (
            repo_common is None
            or owner_of(run_root) != repo_common
            or not workspace_claim_matches(repo_root, run_root)
        ):
            return False
        from syncade.gc_execute import reap_processes_in_tree

        _reaped, proven_free = reap_processes_in_tree(safe)
        if not proven_free:
            return False
    # `ignore_errors=True` stays — this is best-effort cleanup inside a live run and must
    # not raise — but its outcome is no longer ASSUMED. Returning "cleared" over a tree
    # that is still there is the same regression as abandoning one silently: the caller
    # goes on to destroy the round's artifacts and then fails provisioning against the
    # leftover it was told was gone.
    shutil.rmtree(safe, ignore_errors=True)
    return not safe.exists()
