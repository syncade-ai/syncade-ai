"""Snapshot the git state at the start of a syncade run.

A :class:`Snapshot` is a frozen value object that records exactly what
:mod:`syncade.orchestrator` needs to reproduce the worktrees a reviewer sees:
which commit they're checked out at, what branch (if any) the
run originated from, and — if the user supplied ``--base <ref>`` — the
diff to render into the reviewer prompt.

This module deliberately does NOT use :func:`subprocess.run` directly.
Every git call goes through :func:`syncade.process.run_subprocess` so
the shared subprocess machinery (timeout handling, error classification,
process-group cleanup) is exercised consistently across the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from syncade.git_object_id import is_full_git_object_id
from syncade.process import (
    SubprocessNotFoundError,
    run_subprocess,
)

DirtyState = Literal["clean", "tracked", "untracked", "both"]
"""Four-state classification of ``git status --porcelain`` output.

- ``"clean"`` — empty porcelain output.
- ``"tracked"`` — at least one line, all non-``??`` (modifications
  and/or staged changes to tracked files). The actually-dangerous
  case: the operator has local code changes the reviewers cannot
  see at HEAD. Strong warning surface.
- ``"untracked"`` — at least one line, all starting with ``??``.
  The alpha-briefings case: the operator has scratch files the
  reviewers cannot see at HEAD, which is usually intentional. Soft
  note surface.
- ``"both"`` — lines of both kinds present. The operator should
  know about both. Emits BOTH messages (strong first, soft
  second)."""

# Wall-clock ceiling for any single git invocation made by this module.
# `git diff` against a large base ref can run for several seconds on a
# big repo; everything else is sub-second. 30s is generous without
# being indefinite.
_GIT_TIMEOUT_SECONDS: float = 30.0

_NORMALIZED_DIFF_ARGS: Final[tuple[str, ...]] = (
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "diff",
    "--no-color",
)


class SnapshotError(Exception):
    """Raised when snapshotting fails.

    Covers: cwd is not a git repository, HEAD is unresolvable (empty
    repo), the supplied ``base_ref`` doesn't exist, or git itself isn't
    installed. The message always includes the underlying git stderr
    (trimmed) when applicable so the CLI can surface a useful error
    without further introspection.
    """


@dataclass(frozen=True)
class Snapshot:
    """Frozen record of the repo state at the moment a syncade run started.

    Captures exactly what's needed to (a) reproduce the worktrees the
    reviewers see and (b) populate the reviewer prompt's diff section
    when a base ref was supplied.

    Attributes:
        repo_root: Absolute path to the repo the snapshot was taken in.
        commit_sha: Full HEAD object ID at snapshot time. Always the
            canonical form, even if the caller supplied a short SHA
            elsewhere — ``git rev-parse HEAD`` is the source of truth.
        branch: The branch name, or ``None`` for detached HEAD. A
            detached HEAD is a legitimate state for a CI-style run
            (e.g. reviewing a tag); the orchestrator handles both.
        base_ref: The ``--base`` value the caller supplied, or ``None``
            if no diff was requested. Preserved verbatim so it can be
            echoed back in the run manifest.
        diff_text: ``git diff <base_ref>..HEAD`` stdout when
            ``base_ref`` was supplied; the empty string otherwise.
            Running without a diff is supported — reviewers fall back
            to reviewing the full HEAD state.
        dirty_state: Four-state classification of the working tree. See
            :data:`DirtyState` for the semantics. The orchestrator branches on
            this to choose between a strong
            warning (tracked-modified — the actually-dangerous case)
            and a soft note (untracked-only — usually intentional),
            instead of conflating both via a single warning string.
        untracked_count: Number of untracked files at snapshot time.
            The soft dirty-tree note includes this count ("working
            tree has untracked files (not reviewed): <count>
            file(s)..."). Captured here once so callers don't have
            to re-parse porcelain to compute it. ``0`` when
            ``dirty_state`` is ``"clean"`` or ``"tracked"``.
    """

    repo_root: Path
    commit_sha: str
    branch: str | None
    base_ref: str | None
    diff_text: str
    dirty_state: DirtyState
    untracked_count: int = 0


def _git(repo_root: Path, *args: str) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``repo_root`` and return (rc, stdout, stderr).

    Raises :class:`SnapshotError` if the ``git`` binary itself is
    missing — the orchestrator can't snapshot anything without it.
    """
    try:
        result = run_subprocess(
            ["git", *args],
            cwd=repo_root,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except SubprocessNotFoundError as exc:
        raise SnapshotError("git binary not found on PATH — install git to use syncade") from exc
    return result.returncode, result.stdout, result.stderr


def discover_repo_root(start_path: Path) -> Path:
    """Resolve ``start_path`` to the root of the git repo it lives in.

    Runs ``git rev-parse --show-toplevel`` from ``start_path`` and returns
    the resolved repo root. This is the canonical way for the rest of the
    orchestrator to convert a user-supplied path — which may point at any
    subdirectory of the repo (e.g. the user ran ``syncade`` from
    ``repo/docs/reviews/``) — into the repo-root path that ``.syncade/``
    artifacts and worktree operations must be anchored to. The
    user-supplied value is a *starting hint*, not the canonical root.

    Args:
        start_path: Any path inside the git working tree. Typically the
            user's cwd or the ``--repo-root`` value. Must be an existing
            directory.

    Returns:
        The absolute, resolved path of the repo root — the directory
        ``git rev-parse --show-toplevel`` reports.

    Raises:
        SnapshotError: If ``start_path`` does not exist, is not a
            directory, is not inside a git working tree, or git itself
            isn't installed. The message includes git's own stderr
            where available.
    """
    if not start_path.exists():
        raise SnapshotError(f"start_path does not exist: {start_path}")
    if not start_path.is_dir():
        raise SnapshotError(f"start_path is not a directory: {start_path}")

    rc, toplevel_stdout, toplevel_stderr = _git(start_path, "rev-parse", "--show-toplevel")
    if rc != 0:
        # `git rev-parse --show-toplevel` outside a repo emits
        # "fatal: not a git repository (or any of the parent ...)".
        # Surface git's own message so the user can disambiguate.
        raise SnapshotError(
            f"{start_path} is not inside a git repository: {toplevel_stderr.strip()}"
        )
    return Path(toplevel_stdout.strip()).resolve()


def take_snapshot(repo_root: Path, *, base_ref: str | None = None) -> Snapshot:
    """Capture a :class:`Snapshot` of ``repo_root`` at HEAD.

    Args:
        repo_root: Path to the repo to snapshot. Must be an existing
            directory and a git working tree.
        base_ref: Optional ref the diff is rendered against (e.g.
            ``"main"``, ``"HEAD~3"``, a tag, a commit SHA). When
            ``None`` (the default), no diff is captured — the
            ``diff_text`` field is the empty string and the reviewer
            prompt will use a "no diff provided" sentinel instead.

    Returns:
        A :class:`Snapshot` populated with the resolved HEAD SHA,
        branch name (or ``None`` for detached HEAD), the supplied
        ``base_ref`` (or ``None``), and either the full diff text or
        the empty string.

    Raises:
        SnapshotError: If ``repo_root`` isn't a git working tree, HEAD
            is unresolvable (empty repo), ``base_ref`` doesn't resolve,
            or git itself isn't installed. The message includes the
            underlying git stderr where available.

    The snapshot is a value object — no mutable state, no lazy
    evaluation. The orchestrator takes it once at the top of a run and
    passes it to everything else.

    **Dirty working tree:** This does NOT refuse to run on a dirty
    working tree. The reviewers see whatever's at HEAD; uncommitted
    changes are invisible. A user who runs ``syncade`` against a
    half-committed branch will not see their unstaged work reviewed,
    by design. Refusing dirty trees would couple this library module
    to a UX decision better made at the CLI surface.

    The dirty signal is the four-state :data:`DirtyState` classification on
    :attr:`Snapshot.dirty_state`, distinguishing tracked-modified
    (the actually-dangerous case —
    operator has local code changes the reviewers cannot see) from
    untracked-only (usually intentional — operator has scratch
    files they keep out of git on purpose). The orchestrator
    branches on ``dirty_state`` to emit a strong warning vs. a soft
    note vs. both vs. silence.
    """
    # repo_root must exist and be a directory before we even ask git
    # anything. The downstream "not a git repository" error from git
    # is fine but less actionable than naming the bad path here.
    if not repo_root.exists():
        raise SnapshotError(f"repo_root does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise SnapshotError(f"repo_root is not a directory: {repo_root}")

    # HEAD SHA — also doubles as the "is this a git repo?" probe.
    rc, sha_stdout, sha_stderr = _git(repo_root, "rev-parse", "HEAD")
    if rc != 0:
        # `git rev-parse HEAD` in a non-repo emits:
        #   "fatal: not a git repository (or any of the parent ...)".
        # In an empty repo: "fatal: ambiguous argument 'HEAD' ..."
        # Surface git's own message so the user can disambiguate.
        raise SnapshotError(f"could not resolve HEAD in {repo_root}: {sha_stderr.strip()}")
    commit_sha = sha_stdout.strip()
    if not is_full_git_object_id(commit_sha):
        raise SnapshotError(
            f"git rev-parse HEAD returned unexpected value {commit_sha!r} "
            f"(expected a full SHA-1/SHA-256 object ID)"
        )

    # Branch name. `--abbrev-ref HEAD` returns the branch name OR the
    # literal "HEAD" when detached.
    rc, branch_stdout, branch_stderr = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        # Extremely unlikely once HEAD resolves, but surface it cleanly.
        raise SnapshotError(
            f"could not resolve branch name in {repo_root}: {branch_stderr.strip()}"
        )
    branch_raw = branch_stdout.strip()
    branch: str | None = None if branch_raw == "HEAD" else branch_raw

    # Diff capture — only when base_ref was supplied. An empty
    # base_ref string is treated as "not supplied" to avoid the
    # ambiguous case where the CLI's `--base ""` would otherwise pass
    # through and confuse git.
    diff_text = ""
    if base_ref:
        # Verify the ref resolves before asking for a diff. This gives
        # us a clean SnapshotError naming the bad ref rather than a
        # confusing diff error.
        rc, _, ref_stderr = _git(repo_root, "rev-parse", "--verify", "--quiet", base_ref)
        if rc != 0:
            raise SnapshotError(
                f"base_ref {base_ref!r} does not resolve in {repo_root}: "
                f"{ref_stderr.strip() or 'unknown ref'}"
            )
        # `git diff <base>..HEAD` is the form documented in the brief.
        # On large repos this can be several seconds; the _GIT_TIMEOUT
        # ceiling above handles runaway cases.
        rc, diff_stdout, diff_stderr = _git(repo_root, *_NORMALIZED_DIFF_ARGS, f"{base_ref}..HEAD")
        if rc != 0:
            raise SnapshotError(
                f"git diff {base_ref}..HEAD failed in {repo_root}: {diff_stderr.strip()}"
            )
        diff_text = diff_stdout

    # Working-tree cleanliness probe. `git status --porcelain` returns
    # a stable, machine-parseable list (one line per affected path)
    # with empty stdout on a clean tree. Gitignored paths are
    # excluded by default — the user's `node_modules/` shouldn't
    # make every snapshot dirty.
    #
    # classify by line prefix rather than a flat "any output
    # = dirty" boolean. `??` prefix means untracked; everything else
    # (" M", "M ", "MM", "A ", "D ", "R ", "C ", etc.) means
    # tracked-modified or staged. The two cases have different
    # operator-fix paths.
    rc, status_stdout, status_stderr = _git(repo_root, "status", "--porcelain")
    if rc != 0:
        raise SnapshotError(
            f"could not check working-tree state in {repo_root}: {status_stderr.strip()}"
        )
    dirty_state, untracked_count = _classify_porcelain_with_counts(status_stdout)

    return Snapshot(
        repo_root=repo_root.resolve(),
        commit_sha=commit_sha,
        branch=branch,
        base_ref=base_ref,
        diff_text=diff_text,
        dirty_state=dirty_state,
        untracked_count=untracked_count,
    )


def _classify_porcelain(porcelain_output: str) -> DirtyState:
    """Classify ``git status --porcelain`` output into a :data:`DirtyState`.

    Parses line-by-line. A line starts with ``??`` iff
    ``line[:2] == "??"``. Anything else with non-empty content is
    tracked-modified or staged. Empty output → ``"clean"``.

    Examples of tracked codes that should map to ``"tracked"``:
    ``" M file.txt"`` (modified, not staged), ``"M  file.txt"``
    (staged modification), ``"MM file.txt"`` (staged + further
    modified), ``"A  file.txt"`` (added), ``"D  file.txt"``
    (deleted), ``"R  old -> new"`` (renamed), ``"C  old -> new"``
    (copied).

    Examples of untracked codes that should map to ``"untracked"``:
    ``"?? scratch.txt"``, ``"?? path/with spaces.txt"``,
    ``"?? .file-with-leading-dot"``.

    Empty / whitespace-only output → ``"clean"`` even though the
    function is called only when ``git status`` returned 0 — git's
    own output may have trailing newlines we need to ignore.
    """
    stripped = porcelain_output.strip()
    if not stripped:
        return "clean"

    has_tracked = False
    has_untracked = False
    for line in porcelain_output.splitlines():
        if not line.strip():
            # Defensive: blank line in the middle of git output is
            # extremely unlikely but should not vote either way.
            continue
        if line[:2] == "??":
            has_untracked = True
        else:
            # All other two-character prefixes encode tracked-file
            # state. "Anything but ??" is the safe rule — new git
            # versions may introduce additional codes (e.g. for new
            # merge conflict states) and the strong-warning bias
            # is the correct one for unfamiliar codes.
            has_tracked = True

    if has_tracked and has_untracked:
        return "both"
    if has_tracked:
        return "tracked"
    return "untracked"


def _classify_porcelain_with_counts(porcelain_output: str) -> tuple[DirtyState, int]:
    """Classify porcelain output and count untracked files.

    Counting happens here (once at snapshot time) rather than at
    warning-emit time, so the orchestrator's soft note can include
    ``<count> file(s)`` without re-running ``git status``. ``0``
    for the clean and tracked-only states (no untracked files
    even possible in those).

    Returns ``(dirty_state, untracked_count)``. The single-pass
    parser keeps the two values in lockstep — a future update to
    the state-detection rule that adds new untracked codes would
    need to update both classifications atomically.
    """
    stripped = porcelain_output.strip()
    if not stripped:
        return ("clean", 0)

    has_tracked = False
    has_untracked = False
    untracked_count = 0
    for line in porcelain_output.splitlines():
        if not line.strip():
            continue
        if line[:2] == "??":
            has_untracked = True
            untracked_count += 1
        else:
            has_tracked = True

    if has_tracked and has_untracked:
        return ("both", untracked_count)
    if has_tracked:
        return ("tracked", untracked_count)
    return ("untracked", untracked_count)
