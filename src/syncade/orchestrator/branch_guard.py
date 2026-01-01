"""Refuse to run the committing loop on the repo's default branch (PR-v2-26).

The producer fast-forwards the CURRENT branch. With no guard, a stranger's first
`syncade <brief>` run while sitting on `main` silently lands producer commits on
their default branch — reproduced: 7 commits on `main`, warned only after they had
landed. This guard refuses that up front, before any subprocess is dispatched.

Enforced at the run-entry choke (:func:`syncade.orchestrator.run_review`), not the
CLI wrapper, so a direct library call and a `--resume` are covered too — the same
"guard where the work happens, not where the CLI happens" lesson as the PR-v2-24
auth gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from syncade.base_resolution import local_default_branch, remote_default_branch
from syncade.worktree import WorktreeError

# Branch names that conventionally ARE a repo's integration branch. Consulted only in the
# remote-less path: a repo that HAS a local main/master but is checked out on one of these
# (e.g. `trunk` beside a vestigial `main`) is refused, because HEAD may be the real default
# even though it is not the local main/master.
_COMMON_DEFAULT_NAMES = frozenset({"main", "master", "trunk", "develop", "development", "default"})


def current_branch_name(repo_root: Path) -> str | None:
    """The checked-out branch name, or ``None`` for detached HEAD.

    Lets the CLI run :func:`guard_default_branch` BEFORE the auth gate probes a provider CLI
    (``codex login status`` is a subprocess), so a committing run on the default branch
    refuses before any subprocess spawns — ``run_review`` re-checks for library callers.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def guard_default_branch(
    repo_root: Path,
    current_branch: str | None,
    *,
    allow: bool,
    will_commit: bool,
) -> None:
    """Raise :class:`WorktreeError` when a *committing* run could land on the default branch.

    Exempt (no raise): ``will_commit`` is False (single-pass commits nothing), ``allow`` is
    True (``--allow-default-branch`` — or a repo syncade itself just auto-created), or
    ``current_branch`` is None (detached HEAD).

    Resolution, most authoritative first:

    1. ``origin/HEAD`` present → refuse iff HEAD *is* that branch, whatever it is named;
       otherwise allow. A remote proves the default exactly.
    2. No remote, but a local ``main`` / ``master`` exists → treat it as the default: refuse
       iff HEAD is it, OR HEAD is another common integration name
       (:data:`_COMMON_DEFAULT_NAMES`, e.g. ``trunk`` beside a vestigial ``main``). A plain
       feature branch alongside ``main`` proceeds.
    3. No remote AND no local ``main`` / ``master`` → HEAD is effectively this repo's only
       integration branch (a repo living on ``release`` / ``trunk`` with nothing else), so
       refuse.

    The fresh-dir auto-init is exempted upstream via ``allow`` so onboarding still works.
    """
    if not will_commit or allow or current_branch is None:
        return

    remote_default = remote_default_branch(repo_root)
    if remote_default is not None:
        if current_branch == remote_default:
            raise WorktreeError(
                f"refusing: HEAD is the default branch {current_branch!r}, and loop mode "
                f"would fast-forward producer commits onto it. Re-run on a feature branch, "
                f"or pass --allow-default-branch to commit there deliberately."
            )
        return

    local_default = local_default_branch(repo_root)
    if local_default is None:
        raise WorktreeError(
            f"refusing: HEAD is {current_branch!r}, and with no origin/HEAD and no local "
            f"main/master there is nothing to prove it is not this repo's default branch, so "
            f"producer commits could land there. Set origin/HEAD "
            f"(git remote set-head origin <branch>), or pass --allow-default-branch."
        )
    if current_branch == local_default or current_branch in _COMMON_DEFAULT_NAMES:
        raise WorktreeError(
            f"refusing: HEAD ({current_branch!r}) looks like this repo's default/integration "
            f"branch (local default is {local_default!r}; no origin/HEAD to prove otherwise), "
            f"so producer commits could land there. Re-run on a feature branch, or pass "
            f"--allow-default-branch to commit here deliberately."
        )
