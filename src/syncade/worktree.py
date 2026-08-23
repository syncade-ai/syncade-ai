"""Linked Git worktree management for trusted test/check legs.

Provides the worktree error type, default actor-workspace base, run-id helper,
and ``WorktreeManager``. Model reviewers use
:class:`syncade.reviewer_workspace.ReviewerWorkspaceManager` instead.

This module is intentionally decoupled from :mod:`syncade.config`.
Callers pull values out of :class:`syncade.config.SyncadeConfig`
themselves and pass them in as plain types, which keeps the worktree
code narrowly scoped and trivially testable without a config object.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType

from syncade.process import SubprocessError, SubprocessResult, run_subprocess

from .worktree_paths import (
    WorktreeError,
    _registered_path_differs_from_actual,
    _strip_files,
    _validate_reviewer_name,
)

DEFAULT_WORKTREE_BASE: Path = Path("/tmp/syncade")
"""Filesystem location under which per-run actor workspaces are created.

Configurable via :class:`WorktreeManager`'s constructor argument; the
default keeps worktrees out of the user's repo and on a tmpfs where the
OS will clear them on reboot.
"""


TEST_WORKTREE_NAME: str = "tests"
"""Reserved reviewer-name basename used by the test re-run leg.

Single source of truth — the orchestrator uses it for
the actual provisioning, and :mod:`syncade.config` uses it (via
``casefold()`` comparison) to reject reviewer configs
that would collide with it when ``[loop] test_command`` is set.

Keeping the name in this module gives the orchestrator and config
validator a neutral import point without creating an import cycle:
config → worktree is one-way; worktree imports no syncade modules."""


_PREFIX_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")
"""Inverse character class — anything that is *not* an ASCII
alphanumeric, hyphen, or underscore. Used to strip junk from prefixes."""

_GIT_TIMEOUT_SECONDS = 30.0


def _run_git(argv: list[str], *, cwd: Path, error_prefix: str) -> SubprocessResult:
    try:
        return run_subprocess(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)
    except SubprocessError as exc:
        raise WorktreeError(f"{error_prefix}: {exc}") from exc


def _run_git_best_effort(argv: list[str], *, cwd: Path) -> None:
    try:
        run_subprocess(argv, cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)
    except SubprocessError:
        return


def generate_run_id(prefix: str | None = None) -> str:
    """Return a stable, human-readable run identifier.

    Format: ``YYYY-MM-DDTHH-MM-SS[-prefix]`` (e.g. ``2026-05-11T17-23-04``
    or ``2026-05-11T17-23-04-pr2`` if a prefix is supplied). The
    colon-free form is intentional so the id can be used as a filesystem
    path component on all platforms without quoting.

    The prefix is sanitized in two passes: first, only ASCII
    alphanumerics, hyphens, and underscores are kept; everything else
    is stripped. Then leading and trailing hyphens and underscores are
    trimmed so the joined id reads cleanly (no ``...-T--leading`` or
    ``...-trailing-`` shapes). A prefix that is empty after both
    passes (all junk, or only separator characters) yields a bare
    timestamp with no suffix. An empty string is treated the same as
    ``None``.

    Resolution is one second. Two calls inside the same wall-clock
    second produce identical ids — callers that need uniqueness across
    rapid invocations must pass distinct prefixes.
    """
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if not prefix:
        return timestamp
    cleaned = _PREFIX_DISALLOWED.sub("", prefix).strip("-_")
    if not cleaned:
        return timestamp
    return f"{timestamp}-{cleaned}"


@dataclass(frozen=True)
class Worktree:
    """A single managed worktree.

    Immutable record produced by :meth:`WorktreeManager.create`; the
    manager owns lifecycle (creation and cleanup).

    Attributes:
        path: Absolute path to the worktree on disk.
        reviewer_name: Identifier passed at creation time. Used as the
            directory name under ``run_dir``.
        commit_sha: Full object ID the worktree is checked out
            at. The :meth:`WorktreeManager.create` method canonicalizes whatever
            commit-ish the caller supplied (short SHA, branch name,
            tag, ...) to the underlying commit, so this field is
            always the authoritative identifier.
    """

    path: Path
    reviewer_name: str
    commit_sha: str


class WorktreeManager:
    """Provision and clean up linked Git worktrees for trusted legs.

    Use as a context manager — on clean exit, all worktrees created
    through this manager are removed. On exception, worktrees are
    intentionally left in place so the user can inspect what the leg saw.

    Example::

        run_id = generate_run_id("pr-3")
        with WorktreeManager(repo_root, run_id) as mgr:
            wt = mgr.create(
                "tests",
                commit_sha,
                strip_files=["CLAUDE.md", "AGENTS.md"],
            )
            # run the configured test command in wt.path ...
            # cleanup happens automatically on context exit
    """

    def __init__(
        self,
        repo_root: Path,
        run_id: str,
        base_dir: Path = DEFAULT_WORKTREE_BASE,
        *,
        defer_cleanup: bool = False,
    ) -> None:
        """Construct the manager. No filesystem side effects yet.

        Args:
            repo_root: The git repo whose history we're worktreeing.
            run_id: Identifier for this run (use :func:`generate_run_id`).
            base_dir: Where to place worktrees on disk. Defaults to
                :data:`DEFAULT_WORKTREE_BASE`.
            defer_cleanup: When ``True``, the context
                manager's ``__exit__`` does NOT auto-cleanup on
                clean exit. The caller (orchestrator) is responsible
                for calling :meth:`cleanup_all` explicitly later.
                Used by the loop orchestrator to defer the
                cleanup-vs-preserve decision until the loop's final
                exit code is known: the PRD specifies that exits
                10/20/30 keep worktrees for inspection.
                Default ``False`` auto-cleans on clean exit.
        """
        self._repo_root = Path(repo_root)
        self._run_id = run_id
        self._base_dir = Path(base_dir)
        self._worktrees: list[Worktree] = []
        self._defer_cleanup = defer_cleanup

    @property
    def run_dir(self) -> Path:
        """``base_dir / run_id`` — the parent directory containing all
        worktrees for this run."""
        return self._base_dir / self._run_id

    def create(
        self,
        reviewer_name: str,
        commit_sha: str,
        strip_files: list[str] | None = None,
    ) -> Worktree:
        """Create a worktree at ``run_dir / reviewer_name`` checked out
        at ``commit_sha``.

        ``reviewer_name`` must be a plain basename — empty strings,
        ``"."``, ``".."``, names containing a path separator, and
        absolute paths are rejected before any filesystem or git side
        effects so a malformed name cannot make the worktree escape
        :attr:`run_dir`.

        If ``strip_files`` is provided, those filenames (matched by
        basename recursively) are deleted after worktree creation;
        missing files are not an error. Returns the resulting
        :class:`Worktree` record.

        Raises:
            WorktreeError: On invalid ``reviewer_name``, on
                ``git worktree add`` failure (message includes git
                stderr), or if the target worktree directory already
                exists.
        """
        _validate_reviewer_name(reviewer_name)
        target = (self.run_dir / reviewer_name).absolute()
        if target.exists():
            raise WorktreeError(
                f"git worktree add failed: target directory already exists: {target}"
            )
        # `git worktree add` requires the parent of the target to exist
        # but does not create intermediate directories itself. If
        # `run_dir` already exists as a (non-directory) file, mkdir
        # raises FileExistsError; surface that as a typed
        # provisioning error rather than a raw OSError so the CLI can
        # map it to exit code 60.
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorktreeError(
                f"git worktree add failed: could not create run_dir {self.run_dir}: {exc}"
            ) from exc
        # Without --no-replace-objects, `git worktree add <sha>` checks out a
        # replacement object while `Snapshot.commit_sha` names the original.
        result = _run_git(
            ["git", "--no-replace-objects", "worktree", "add", str(target), commit_sha],
            cwd=self._repo_root,
            error_prefix="git worktree add failed",
        )
        if result.returncode != 0:
            raise WorktreeError(f"git worktree add failed: {result.stderr.strip()}")
        # `git worktree add` succeeded — from here until we append to
        # self._worktrees, any failure must roll back the worktree so
        # we don't orphan the directory or the git registration. The
        # try / except BaseException covers normal exceptions plus
        # KeyboardInterrupt and SystemExit so a poorly-timed Ctrl-C
        # also leaves the manager's invariants intact.
        try:
            # Canonicalize commit_sha to the full object ID. Caller
            # may have passed a short SHA, branch name, or tag —
            # `git rev-parse HEAD` inside the new worktree is the
            # authoritative source.
            rev_parse = _run_git(
                ["git", "rev-parse", "HEAD"],
                cwd=target,
                error_prefix="git rev-parse HEAD failed",
            )
            if rev_parse.returncode != 0:
                raise WorktreeError(f"git rev-parse HEAD failed: {rev_parse.stderr.strip()}")
            worktree = Worktree(
                path=target,
                reviewer_name=reviewer_name,
                commit_sha=rev_parse.stdout.strip(),
            )
            if strip_files:
                _strip_files(target, strip_files)
            self._worktrees.append(worktree)
            return worktree
        except BaseException:
            self._rollback_add(target)
            raise

    def _rollback_add(self, target: Path) -> None:
        """Undo a partially-completed :meth:`create` after
        ``git worktree add`` has already succeeded but a later step
        failed. Best-effort — runs ``git worktree remove --force`` and
        ``shutil.rmtree`` and swallows everything, because we're
        already in the middle of unwinding the caller's exception and
        masking it would be worse than leaving stray state.
        """
        _run_git_best_effort(
            ["git", "worktree", "remove", "--force", str(target)],
            cwd=self._repo_root,
        )
        shutil.rmtree(target, ignore_errors=True)

    def cleanup(self, worktree: Worktree) -> None:
        """Remove a single worktree.

        Runs ``git worktree remove --force`` on the path, then
        ``shutil.rmtree`` as a belt-and-braces filesystem clean,
        then ``git worktree prune`` to drop any stale
        ``.git/worktrees/<name>/`` metadata git might still hold.

        Safe to call multiple times: if the worktree path no
        longer exists when called, runs ``git worktree prune``
        anyway (to clear stale metadata from a prior
        non-git-mediated deletion) and returns. If the path
        still exists but ``git worktree remove`` fails, the
        recovery path detects stale-metadata drift by comparing the
        registered gitdir-file path to the actual path. This is
        locale-independent and avoids English-substring matching
        against git's stderr. The recovery path either succeeds via a
        prune-then-rmtree fallback or raises
        :class:`WorktreeError` with the diagnostics.

        The recovery path is structured around three goals:

        1. Leave git registration AND filesystem in a consistent
           state — never end in "git no longer recognizes the
           path, but the directory still exists".
        2. Tolerate metadata drift from external rm / symlink-
           resolution changes / interrupted prior cleanups
           without surfacing English-substring matching to the
           caller.
        3. Stay best-effort idempotent: cleanup of an already-gone
           worktree never raises; the only raise path is a hard
           failure where the directory genuinely still exists on
           disk AND git failed to remove it AND our fallback
           rmtree also failed.

        :meth:`__exit__` catches and warns on any such failure so
        an original in-flight exception is never masked.
        """
        if not worktree.path.exists():
            # Path is gone but git's metadata might still point at
            # it. Prune now so a future provisioning attempt with
            # the same name doesn't hit the stale-metadata
            # validation error.
            self._git_worktree_prune()
            return
        result = _run_git(
            ["git", "worktree", "remove", "--force", str(worktree.path)],
            cwd=self._repo_root,
            error_prefix="git worktree remove failed",
        )
        if result.returncode == 0:
            shutil.rmtree(worktree.path, ignore_errors=True)
            # Prune is cheap and idempotent; run it even on the
            # happy path in case the remove succeeded but left
            # metadata fragments (defensive — git's own remove
            # generally cleans this up, but edge cases exist).
            self._git_worktree_prune()
            return

        # remove failed. If the path disappeared mid-call (a
        # concurrent actor), that's still idempotent "nothing to
        # do" — prune any partial metadata git may have left and
        # exit cleanly.
        if not worktree.path.exists():
            self._git_worktree_prune()
            return

        # remove failed AND the path still exists. Detect the
        # stale-metadata-drift case structurally: the
        # registered ``.git/worktrees/<name>/gitdir`` file
        # records the worktree path git thinks the registration
        # points at. If it disagrees with the actual on-disk
        # path (symlink-resolution drift, prior partial cleanup),
        # prune + try again. Even if prune + retry fails, fall
        # through to rmtree the on-disk directory — at that
        # point git has dropped its registration so rmtree is
        # safe and doesn't leave inconsistent state.
        original_stderr = result.stderr.strip()
        if _registered_path_differs_from_actual(self._repo_root, worktree):
            self._git_worktree_prune()
            retry = _run_git(
                ["git", "worktree", "remove", "--force", str(worktree.path)],
                cwd=self._repo_root,
                error_prefix="git worktree remove failed",
            )
            if retry.returncode == 0:
                shutil.rmtree(worktree.path, ignore_errors=True)
                self._git_worktree_prune()
                return
            # prune cleared git's registration, but the
            # remove retry failed (typically because git no
            # longer recognizes the path as a worktree). Without
            # this fallback the directory was left on disk →
            # next worktree-add with the same name would hit
            # "target directory already exists". rmtree the
            # directory directly now that git has nothing
            # registered to inconsistently affect.
            try:
                shutil.rmtree(worktree.path)
            except OSError:
                # rmtree failed too — at this point we genuinely
                # can't clean up. Raise with the original git
                # diagnostic so the operator sees the first cause.
                raise WorktreeError(
                    f"git worktree remove failed AND structural-recovery "
                    f"rmtree of {worktree.path} also failed: "
                    f"{original_stderr}"
                ) from None
            self._git_worktree_prune()
            return

        # Not a stale-metadata case — surface git's original
        # diagnostic verbatim.
        raise WorktreeError(f"git worktree remove failed: {original_stderr}")

    def _git_worktree_prune(self) -> None:
        """Run ``git worktree prune`` to clean stale registered
        worktree metadata. Best-effort: silently no-op on git
        error (this is a cleanup path; we'd already be losing the
        original failure context if we raised here).

        Git's ``.git/worktrees/<name>/`` directories record the registered
        worktree path. When the actual worktree directory is deleted
        out-of-band (not via ``git worktree remove``), the metadata persists
        and pollutes subsequent worktree-add attempts. ``git worktree prune``
        is the canonical cleanup: it walks the metadata and drops any entry
        whose ``gitdir`` file points at a nonexistent path.
        """
        _run_git_best_effort(
            ["git", "worktree", "prune"],
            cwd=self._repo_root,
        )

    def cleanup_all(self) -> None:
        """Remove every worktree created through this manager.

        Each worktree is cleaned independently. A per-entry failure
        (e.g. a git lock or NFS stall on ``git worktree remove``) is
        warned to stderr and the loop continues, so one stuck worktree
        can never orphan the rest. After every entry has been attempted
        the accumulated failures are re-raised as a single
        :class:`WorktreeError`, preserving the existing caller contract
        (:meth:`__exit__` warns; ``loop_finalize`` swallows).

        Also attempts to remove :attr:`run_dir` itself if it is empty
        after cleanup. A non-empty ``run_dir`` is left alone — likely
        another concurrent run, or stray reviewer scratch state worth
        preserving for inspection.
        """
        errors: list[str] = []
        for worktree in self._worktrees:
            try:
                self.cleanup(worktree)
            except WorktreeError as exc:
                # Isolate the failure: warn and keep going so a single
                # stuck entry never strands the worktrees behind it.
                print(
                    f"[syncade] warning: failed to clean up worktree "
                    f"{worktree.reviewer_name!r} at {worktree.path}: {exc}",
                    file=sys.stderr,
                )
                errors.append(str(exc))
        if self.run_dir.exists():
            try:
                self.run_dir.rmdir()
            except OSError:
                # Not empty (or otherwise un-removable) — leave it.
                pass
        if errors:
            raise WorktreeError(
                f"cleanup_all: {len(errors)} of {len(self._worktrees)} "
                "worktree(s) failed to clean up: " + "; ".join(errors)
            )

    def __enter__(self) -> WorktreeManager:
        """Enter the context manager. No side effects beyond returning
        self; worktrees are only created via explicit :meth:`create`
        calls."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager.

        On clean exit (``exc_type is None``) AND ``defer_cleanup``
        is ``False`` (the default), calls :meth:`cleanup_all`. On
        exception exit, worktrees are intentionally preserved so
        the user can inspect what each reviewer saw. When
        ``defer_cleanup=True``, cleanup is skipped on clean exit
        too — the caller (orchestrator) calls
        :meth:`cleanup_all` explicitly later based on the loop's
        final exit code.

        Either way, this method must not raise — failures during
        cleanup are warned to stderr rather than propagated, so the
        original exception is not masked.
        """
        if exc_type is not None:
            return
        if self._defer_cleanup:
            return
        try:
            self.cleanup_all()
        except Exception as exc:
            print(
                f"[syncade] warning: worktree cleanup failed: {exc}",
                file=sys.stderr,
            )
