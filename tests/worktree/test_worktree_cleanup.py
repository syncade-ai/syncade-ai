"""Tests for :mod:`syncade.worktree` — cleanup / context-manager surface.

Uses real ``git`` subprocess calls against an ephemeral repo built under
``tmp_path`` — no mocking. The point is to know if the integration
actually works, not whether our wrapper happens to call subprocess with
the right arguments.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from syncade.worktree import (
    Worktree,
    WorktreeError,
    WorktreeManager,
)
from tests.worktree._helpers import _git

# Skip everything in this module if git isn't on PATH — equivalent to the
# brief's "pytest.importorskip" hint for a non-Python dependency.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not found on PATH",
)


# ---------------------------------------------------------------------------
# WorktreeManager.cleanup / cleanup_all
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_removes_dir_and_git_registration(self, repo, base_dir):
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt = mgr.create("claude-reviewer", sha)
        target_path = wt.path
        assert target_path.exists()

        mgr.cleanup(wt)

        assert not target_path.exists()
        # `git worktree list` must no longer show the worktree
        listing = _git(repo_path, "worktree", "list").stdout
        assert str(target_path) not in listing

    def test_cleanup_raises_worktree_error_when_git_remove_fails(self, repo, base_dir, tmp_path):
        """If `git worktree remove --force` returns non-zero against a
        path that still exists, cleanup() must raise WorktreeError
        with the documented `git worktree remove failed:` prefix.

        We construct a Worktree record pointing at a real directory
        git doesn't know about — git's response (`not a working tree`)
        is the real failure path; no monkeypatching needed."""
        repo_path, _ = repo
        unknown_path = tmp_path / "not-a-worktree"
        unknown_path.mkdir()
        (unknown_path / "marker").write_text("x")
        fake_wt = Worktree(
            path=unknown_path,
            reviewer_name="phantom",
            commit_sha="0" * 40,
        )
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        with pytest.raises(WorktreeError) as exc_info:
            mgr.cleanup(fake_wt)
        assert str(exc_info.value).startswith("git worktree remove failed:")
        # The unrecognized directory is preserved (we only own things
        # we created — cleanup raising means we did NOT delete it).
        assert unknown_path.exists()

    def test_cleanup_is_silent_when_path_missing_even_if_git_fails(self, repo, base_dir, tmp_path):
        """Idempotence wins over surfacing failure: if the worktree
        path doesn't exist (because something already cleaned it),
        cleanup() returns silently regardless of what git would say."""
        repo_path, _ = repo
        gone_path = tmp_path / "vanished"
        # Path does NOT exist on disk
        fake_wt = Worktree(
            path=gone_path,
            reviewer_name="phantom",
            commit_sha="0" * 40,
        )
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        # Must not raise — even though git wouldn't know about the path
        mgr.cleanup(fake_wt)

    def test_context_manager_swallows_cleanup_error_on_clean_exit(
        self, repo, base_dir, tmp_path, capsys
    ):
        """__exit__ on clean exit must never let a cleanup_all failure
        propagate — the original (None) exception path must complete.
        Failure is warned to stderr instead."""
        repo_path, sha = repo
        with WorktreeManager(repo_path, "run-1", base_dir=base_dir) as mgr:
            mgr.create("rv", sha)
            # Inject a fake worktree record pointing at an unrelated
            # directory git doesn't know about. cleanup() on it would
            # naturally raise WorktreeError, but __exit__ must catch it.
            unknown_path = tmp_path / "decoy-worktree"
            unknown_path.mkdir()
            (unknown_path / "x").write_text("y")
            mgr._worktrees.append(
                Worktree(
                    path=unknown_path,
                    reviewer_name="decoy",
                    commit_sha="0" * 40,
                )
            )
        # No exception propagated from the `with` block.
        captured = capsys.readouterr()
        assert "worktree cleanup failed" in captured.err
        assert "git worktree remove failed:" in captured.err

    def test_cleanup_is_idempotent(self, repo, base_dir):
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt = mgr.create("claude-reviewer", sha)
        mgr.cleanup(wt)
        # Second cleanup must not raise
        mgr.cleanup(wt)

    def test_cleanup_all_removes_every_managed_worktree(self, repo, base_dir):
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-1", base_dir=base_dir)
        wt1 = mgr.create("claude-reviewer", sha)
        wt2 = mgr.create("codex-reviewer", sha)
        assert wt1.path.exists() and wt2.path.exists()

        mgr.cleanup_all()

        assert not wt1.path.exists()
        assert not wt2.path.exists()

    def test_cleanup_all_continues_after_first_entry_fails(self, repo, base_dir, tmp_path):
        """N7: a failure on the FIRST managed worktree must not orphan
        the rest. ``cleanup_all`` isolates each entry — a phantom that
        raises ``WorktreeError`` (the git-lock / NFS-stall class of
        failure) is warned and the loop continues, so the real
        worktrees behind it are still removed. The accumulated failure
        is re-raised once, after every entry has been attempted.

        Pre-fix: the loop had no per-entry guard, so the phantom's
        raise aborted ``cleanup_all`` and left ``wt1``/``wt2`` on disk.
        """
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-n7", base_dir=base_dir)
        wt1 = mgr.create("claude-reviewer", sha)
        wt2 = mgr.create("codex-reviewer", sha)
        assert wt1.path.exists() and wt2.path.exists()

        # A phantom FIRST entry: a real directory git knows nothing
        # about, so cleanup() raises "git worktree remove failed:".
        phantom_dir = tmp_path / "phantom-worktree"
        phantom_dir.mkdir()
        (phantom_dir / "marker").write_text("x")
        mgr._worktrees.insert(
            0,
            Worktree(path=phantom_dir, reviewer_name="phantom", commit_sha="0" * 40),
        )

        with pytest.raises(WorktreeError) as exc_info:
            mgr.cleanup_all()

        # The real worktrees behind the failing entry were still cleaned.
        assert not wt1.path.exists(), "wt1 orphaned after first-entry cleanup failure"
        assert not wt2.path.exists(), "wt2 orphaned after first-entry cleanup failure"
        # The unrecognized directory we did not create is preserved.
        assert phantom_dir.exists()
        # The aggregate error carries the original git diagnostic.
        assert "git worktree remove failed:" in str(exc_info.value)


class TestCleanupHandlesStaleMetadata:
    """T3.13: ``cleanup`` runs ``git worktree prune`` even when the
    worktree directory was deleted out-of-band, AND can recover
    from the validation-mismatch error when registered path and
    on-disk path drift.

    The pre-T3.13 behavior: ``cleanup`` short-circuited on
    ``not path.exists()`` without running prune. Stale metadata
    persisted and pollluted the next ``git worktree add`` with the
    same name — failing with "validation failed, cannot remove
    working tree: <path> does not point back to .git/worktrees/<n>".
    """

    def test_cleanup_prunes_metadata_after_external_directory_removal(self, repo, base_dir):
        """Reproduce the leak: create a worktree, delete its
        directory externally (not via git), then run cleanup.
        Pre-T3.13: metadata persists. Post-T3.13: prune runs and
        the metadata is dropped."""
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-stale", base_dir=base_dir)
        wt = mgr.create("rv-stale", sha)
        # Simulate an external rm -rf (interrupted test, external
        # process). Git's .git/worktrees/rv-stale/ metadata is now
        # an orphan.
        shutil.rmtree(wt.path)
        # Sanity: metadata still exists.
        assert (repo_path / ".git" / "worktrees" / "rv-stale").is_dir()

        # cleanup() runs prune even though the path is gone.
        mgr.cleanup(wt)

        # Metadata directory is gone post-prune.
        assert not (repo_path / ".git" / "worktrees" / "rv-stale").exists(), (
            "stale .git/worktrees/rv-stale/ metadata not pruned by cleanup; "
            "a future worktree add with the same name will hit "
            "the validation-mismatch error"
        )

    def test_cleanup_recovers_when_metadata_drifts_path_still_exists(self, repo, base_dir):
        """R2.T1.5 + R2.T3.14: structural recovery from
        metadata drift. Simulate the drift by mutating the
        ``.git/worktrees/<name>/gitdir`` file to point at a
        bogus path while keeping the actual worktree on disk.
        cleanup should:
        - detect the drift via the structural check (gitdir
          contents vs. actual path)
        - prune the stale metadata
        - rmtree the on-disk directory (since git no longer
          tracks it after prune)
        - end in a consistent state: directory gone, metadata
          gone, no exception.

        Pre-R2.T1.5: the rmtree fallback was missing — git's
        prune dropped the registration, but the directory was
        left on disk. The next provisioning attempt with the
        same name failed with "target directory already
        exists".
        """
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-drift", base_dir=base_dir)
        wt = mgr.create("rv-drift", sha)

        # Force the drift: rewrite gitdir to point at a path
        # that doesn't match the actual on-disk worktree.
        gitdir_file = repo_path / ".git" / "worktrees" / "rv-drift" / "gitdir"
        assert gitdir_file.is_file()
        gitdir_file.write_text("/totally/bogus/path/.git\n")

        # cleanup should recover, not raise. After cleanup the
        # on-disk path AND the metadata are both gone.
        mgr.cleanup(wt)

        assert not wt.path.exists(), (
            "cleanup left the on-disk worktree path after drift "
            "recovery — R2.T1.5 fallback rmtree didn't fire"
        )
        assert not (repo_path / ".git" / "worktrees" / "rv-drift").exists(), (
            "stale metadata not cleared after drift recovery"
        )

    def test_cleanup_failure_raises_with_original_stderr(self, repo, base_dir):
        """R2.T1.5: when both git remove AND the structural-
        recovery rmtree fail, cleanup raises WorktreeError
        carrying git's ORIGINAL stderr (not the retry's), so the
        operator sees the first-cause diagnostic.

        Synthesize the case by:
        1. Creating a real worktree.
        2. Replacing the on-disk path with a file (not a
           directory) so rmtree fails with NotADirectoryError.
        3. Mutating the gitdir to drift (triggering the
           structural recovery path).
        4. Asserting WorktreeError mentions the original git
           failure.
        """
        repo_path, sha = repo
        mgr = WorktreeManager(repo_path, "run-bothfail", base_dir=base_dir)
        wt = mgr.create("rv-bothfail", sha)

        # Replace the worktree directory with a regular file —
        # git's remove will fail (not a worktree dir), AND
        # shutil.rmtree on a file path will fail.
        shutil.rmtree(wt.path)
        wt.path.write_text("not a directory\n")

        # Drift the metadata too so we enter the recovery path
        # (otherwise it surfaces the original git error directly,
        # without trying the rmtree fallback).
        gitdir_file = repo_path / ".git" / "worktrees" / "rv-bothfail" / "gitdir"
        if gitdir_file.is_file():
            gitdir_file.write_text("/bogus/.git\n")

        # NotADirectoryError or PermissionError can fire from
        # rmtree on a file path. Just assert WorktreeError fires
        # with some diagnostic from git's original output.
        with pytest.raises(WorktreeError, match="git worktree remove failed"):
            mgr.cleanup(wt)

        # Cleanup attempted: file path may remain (rmtree
        # failed), but the test asserts the error path is
        # reported clearly without losing the original diagnostic.

    def test_provisioning_after_external_removal_succeeds(self, repo, base_dir):
        """End-to-end: after the external-removal scenario above,
        a fresh provisioning with the SAME name in a fresh run
        succeeds. Pre-T3.13 this fails with the validation error;
        post-T3.13 the prune in cleanup clears the metadata so
        the new worktree creates cleanly."""
        repo_path, sha = repo
        mgr1 = WorktreeManager(repo_path, "run-A", base_dir=base_dir)
        wt = mgr1.create("rv-recycle", sha)
        shutil.rmtree(wt.path)  # external deletion
        mgr1.cleanup(wt)  # prune should run

        # New manager, new run id, but the worktree NAME collides
        # — the relevant metadata dir is by NAME inside the repo's
        # .git/worktrees/, not by run-id. The earlier metadata
        # leak would have made this fail with validation mismatch.
        mgr2 = WorktreeManager(repo_path, "run-B", base_dir=base_dir)
        wt2 = mgr2.create("rv-recycle", sha)
        assert wt2.path.exists(), "fresh worktree provisioning failed after stale-metadata cleanup"
        mgr2.cleanup(wt2)


# ---------------------------------------------------------------------------
# WorktreeManager — context manager semantics
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_clean_exit_triggers_cleanup_all(self, repo, base_dir):
        repo_path, sha = repo
        with WorktreeManager(repo_path, "run-1", base_dir=base_dir) as mgr:
            wt = mgr.create("claude-reviewer", sha)
            target = wt.path
            assert target.exists()
        # After clean exit, worktree is gone
        assert not target.exists()

    def test_exception_exit_preserves_worktrees_for_debugging(self, repo, base_dir):
        repo_path, sha = repo
        captured: dict[str, Path] = {}
        with pytest.raises(RuntimeError, match="oops"):
            with WorktreeManager(repo_path, "run-1", base_dir=base_dir) as mgr:
                wt = mgr.create("claude-reviewer", sha)
                captured["path"] = wt.path
                raise RuntimeError("oops")
        # After exception exit, the worktree is intentionally preserved
        assert captured["path"].exists()
