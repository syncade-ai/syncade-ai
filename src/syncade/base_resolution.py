"""Base / scope resolution.

Turns a *scope token* into a concrete base commit SHA, which the caller feeds
into the existing ``snapshot.take_snapshot(base_ref=...)`` → ``git diff
<base>..HEAD`` path. The diff machinery is unchanged — base resolution only chooses the
base. This is the Python half of "review *what*?" when the operator doesn't name
an explicit ``--base``; the scope-aware NL skill maps phrases onto these tokens.

The three scope anchors (operator-locked decisions):

- ``everything`` → the branch point off the default branch
  (``git merge-base HEAD <default>``).
- ``local`` ("what I just did") → the local-ahead commits, i.e. the merge-base
  with the branch's upstream (``@{upstream}``). No upstream → fall back to the
  branch point (= ``everything``) with a note (a fresh local branch's "what I
  did" is its work since it diverged).
- ``since-last-review`` → the recorded per-branch last-reviewed SHA (passed in by
  the caller, read from ``.syncade/last-reviewed.json``). No record → fall back
  to the branch point with a note ("everything since last review" with no prior
  review unambiguously means everything).

A scope that cannot resolve a base (no default branch found for a fallback path)
raises :class:`BaseResolutionError`; the CLI maps that to a stop-before-the-loop
per CLAUDE.md's "Exit-code convention for CLI mode handlers", and the skill asks
for an explicit base. All git runs through
:func:`syncade.process.run_subprocess` (snapshot-module discipline), never
``subprocess.run``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from syncade.process import SubprocessNotFoundError, run_subprocess

_GIT_TIMEOUT_SECONDS = 30.0

VALID_SCOPES = ("everything", "local", "since-last-review")
"""The scope tokens the CLI ``--scope`` flag accepts and this module resolves."""


class BaseResolutionError(Exception):
    """A scope could not be resolved to a concrete base (e.g. no default branch
    for a branch-point fallback). The CLI surfaces it as a stop-before-loop and
    the skill asks the operator for an explicit ``--base``."""


@dataclass(frozen=True)
class ResolvedBase:
    """The resolved base ref + an optional operator-facing note explaining any
    fallback (e.g. "no upstream; reviewing since the branch point")."""

    base_sha: str
    note: str | None = None


def _git(repo_root: Path, *args: str) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``repo_root`` → (rc, stdout, stderr). Mirrors
    :func:`syncade.snapshot._git` (own copy to keep this a standalone module).

    ``--no-replace-objects`` is prepended unconditionally: merge-base and
    is-ancestor traversals are graph-sensitive operations, and a
    producer-writable ``refs/replace/*`` ref can substitute a different
    ancestor chain, poisoning scope resolution and ancestry checks.
    """
    try:
        result = run_subprocess(
            ["git", "--no-replace-objects", *args], cwd=repo_root, timeout=_GIT_TIMEOUT_SECONDS
        )
    except SubprocessNotFoundError as exc:
        raise BaseResolutionError(
            "git binary not found on PATH — install git to use syncade"
        ) from exc
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _rev_parse_commit(repo_root: Path, ref: str) -> str | None:
    """``git rev-parse <ref>^{commit}`` → commit object ID, or ``None`` if absent.

    Git repositories may use SHA-1 (40 hex chars) or SHA-256 (64 hex chars).
    Trust git's successful parse instead of hard-coding an object-id length.
    """
    rc, out, _ = _git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out if rc == 0 and out else None


def _is_ancestor(repo_root: Path, possible_ancestor: str) -> bool:
    rc, _, _ = _git(repo_root, "merge-base", "--is-ancestor", possible_ancestor, "HEAD")
    return rc == 0


def detect_default_branch(repo_root: Path) -> str | None:
    """The repo's default branch ref, or ``None`` if none can be determined.

    Resolution chain: ``refs/remotes/origin/HEAD`` (e.g. ``origin/main``) → a
    local ``main`` → a local ``master`` → ``None`` (caller asks).
    """
    rc, out, _ = _git(repo_root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if rc == 0 and out.startswith("refs/remotes/"):
        return out[len("refs/remotes/") :]  # e.g. "origin/main"
    for candidate in ("main", "master"):
        if _rev_parse_commit(repo_root, candidate) is not None:
            return candidate
    return None


def local_default_branch(repo_root: Path) -> str | None:
    """A best-effort LOCAL default branch — ``main`` or ``master`` if either exists — else
    ``None``. Used by the default-branch guard only when there is no authoritative
    ``origin/HEAD``: a local ``main``/``master`` is a reasonable (not proven) default, so a
    feature branch alongside it can proceed while HEAD *on* it is refused."""
    for candidate in ("main", "master"):
        if _rev_parse_commit(repo_root, candidate) is not None:
            return candidate
    return None


def remote_default_branch(repo_root: Path) -> str | None:
    """The default branch's BARE name from ``refs/remotes/origin/HEAD`` (AUTHORITATIVE),
    or ``None`` when there is no remote default.

    Unlike :func:`detect_default_branch`, this does NOT fall back to a local ``main`` /
    ``master`` guess: the default-branch commit guard needs to know whether the default is
    PROVEN by the remote, versus merely guessed. A local-only repo therefore returns
    ``None`` here (the guard then uses a common-name heuristic), so a repo checked out on
    ``trunk`` with a vestigial local ``main`` is not wrongly cleared.
    """
    rc, out, _ = _git(repo_root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    remote_prefix = "refs/remotes/origin/"
    if rc == 0 and out.startswith(remote_prefix):
        return out[len(remote_prefix) :]  # e.g. "main"
    return None


def _branch_point(repo_root: Path) -> str:
    """``git merge-base HEAD <default-branch>`` — the point HEAD diverged from the
    default branch. Raises :class:`BaseResolutionError` if there's no default
    branch or no merge-base (unrelated histories)."""
    default = detect_default_branch(repo_root)
    if default is None:
        raise BaseResolutionError(
            "could not determine a default branch (no origin/HEAD, main, or "
            "master) to compute the branch point from — pass an explicit base "
            "(e.g. --base <ref>)"
        )
    rc, out, _ = _git(repo_root, "merge-base", "HEAD", default)
    if rc != 0 or not out:
        raise BaseResolutionError(
            f"no merge-base between HEAD and the default branch {default!r} "
            "(unrelated histories?) — pass an explicit base"
        )
    return out


def resolve_scope(
    repo_root: Path,
    scope: str,
    *,
    last_reviewed_sha: str | None = None,
) -> ResolvedBase:
    """Resolve a scope token into a :class:`ResolvedBase`. See the module
    docstring for the per-scope semantics and the fallback notes."""
    if scope not in VALID_SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {VALID_SCOPES}")

    if scope == "everything":
        return ResolvedBase(base_sha=_branch_point(repo_root))

    if scope == "local":
        upstream = _rev_parse_commit(repo_root, "@{upstream}")
        if upstream is not None:
            rc, out, _ = _git(repo_root, "merge-base", "HEAD", "@{upstream}")
            if rc == 0 and out:
                return ResolvedBase(base_sha=out)
            # Upstream exists but shares no merge-base with HEAD (unrelated
            # histories). Silently widening to the branch point would review
            # far more than the operator intended; stop and ask for an explicit base.
            raise BaseResolutionError(
                "upstream branch exists but shares no merge-base with HEAD "
                "(unrelated histories?) — pass an explicit base (e.g. --base <ref>)"
            )
        # No upstream: a fresh local branch's "what I did" is its work since it
        # diverged from the default branch.
        return ResolvedBase(
            base_sha=_branch_point(repo_root),
            note="no upstream tracking branch; reviewing since the branch point",
        )

    # since-last-review
    if last_reviewed_sha is not None:
        resolved = _rev_parse_commit(repo_root, last_reviewed_sha)
        if resolved is not None:
            if _is_ancestor(repo_root, resolved):
                return ResolvedBase(base_sha=resolved)
            return ResolvedBase(
                base_sha=_branch_point(repo_root),
                note=(
                    f"recorded last-reviewed SHA {resolved[:12]} is not an "
                    "ancestor of HEAD; reviewing since the branch point"
                ),
            )
        # Recorded SHA no longer resolves (history rewritten / gone) → fall back.
        return ResolvedBase(
            base_sha=_branch_point(repo_root),
            note=(
                f"recorded last-reviewed SHA {last_reviewed_sha[:12]} no longer "
                "resolves; reviewing since the branch point"
            ),
        )
    return ResolvedBase(
        base_sha=_branch_point(repo_root),
        note="no prior review recorded for this branch; reviewing since the branch point",
    )
