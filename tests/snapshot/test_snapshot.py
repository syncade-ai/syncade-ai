"""Tests for :mod:`syncade.snapshot`.

Uses real ``git`` subprocess calls against an ephemeral repo built
under ``tmp_path`` — no mocking. The point is to verify the
integration actually works against real git, the same discipline as
:mod:`tests.test_worktree`.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from syncade.git_object_id import is_full_git_object_id
from syncade.snapshot import Snapshot, SnapshotError, discover_repo_root, take_snapshot
from tests.snapshot._helpers import _commit, _git

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not found on PATH",
)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestSingleCommitRepo:
    def test_returns_snapshot_with_full_head_sha(self, single_commit_repo):
        repo_path, sha = single_commit_repo
        snap = take_snapshot(repo_path)
        assert isinstance(snap, Snapshot)
        assert snap.commit_sha == sha
        assert len(snap.commit_sha) == 40

    def test_sha256_repo_returns_snapshot_with_full_head_object_id(self, tmp_path):
        repo_path = tmp_path / "sha256-repo"
        repo_path.mkdir()
        try:
            _git(repo_path, "init", "-q", "--object-format=sha256")
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"git does not support sha256 object-format: {exc.stderr}")
        _git(repo_path, "config", "user.email", "test@example.com")
        _git(repo_path, "config", "user.name", "Test")
        _git(repo_path, "config", "commit.gpgsign", "false")
        sha = _commit(repo_path, {"README.md": "sha256\n"})

        snap = take_snapshot(repo_path)

        assert len(sha) == 64
        assert snap.commit_sha == sha
        assert is_full_git_object_id(snap.commit_sha)

    def test_repo_root_is_resolved_absolute_path(self, single_commit_repo, tmp_path):
        repo_path, _ = single_commit_repo
        # Pass a relative path; the snapshot should still record the
        # absolute resolved form so downstream consumers don't have to
        # worry about cwd drift.
        relative = repo_path.relative_to(tmp_path)
        snap = take_snapshot(tmp_path / relative)
        assert snap.repo_root.is_absolute()
        assert snap.repo_root == repo_path.resolve()

    def test_branch_field_reflects_default_branch(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        snap = take_snapshot(repo_path)
        # `git init` produces either "main" or "master" depending on
        # the user's git defaults. Either is acceptable — just confirm
        # the field was populated with a real string.
        assert isinstance(snap.branch, str)
        assert snap.branch  # non-empty
        assert "/" not in snap.branch  # not a ref path

    def test_no_base_ref_produces_empty_diff(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        snap = take_snapshot(repo_path)
        assert snap.base_ref is None
        assert snap.diff_text == ""

    def test_empty_string_base_ref_is_treated_as_none(self, single_commit_repo):
        """A CLI that does `--base ""` (or a config that produces an
        empty string) must not be passed through to git — that would
        produce a confusing diff error. Treated the same as None."""
        repo_path, _ = single_commit_repo
        snap = take_snapshot(repo_path, base_ref="")
        assert snap.base_ref == ""  # preserved verbatim for the manifest
        assert snap.diff_text == ""


class TestBranchOtherThanDefault:
    def test_branch_field_reflects_named_branch(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        _git(repo_path, "checkout", "-q", "-b", "feature/x")
        snap = take_snapshot(repo_path)
        assert snap.branch == "feature/x"


class TestDetachedHead:
    def test_detached_head_branch_is_none(self, single_commit_repo):
        """A detached HEAD is a legitimate state (CI reviewing a tag,
        or `git checkout <sha>` for local inspection). The snapshot
        records branch=None rather than the literal "HEAD" string git
        reports, so downstream code can branch on it cleanly."""
        repo_path, sha = single_commit_repo
        _git(repo_path, "checkout", "-q", sha)
        snap = take_snapshot(repo_path)
        assert snap.branch is None
        assert snap.commit_sha == sha


class TestWithBaseRef:
    def test_base_ref_head_tilde_captures_last_commits_diff(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        # Make a second commit so HEAD~1 resolves and the diff is non-empty.
        (repo_path / "second.txt").write_text("added in second commit\n")
        _git(repo_path, "add", "-A")
        _git(repo_path, "commit", "-m", "second")

        snap = take_snapshot(repo_path, base_ref="HEAD~1")
        assert snap.base_ref == "HEAD~1"
        # The diff should mention the file we just added.
        assert "second.txt" in snap.diff_text
        assert "added in second commit" in snap.diff_text
        # And should be a real git-format diff (unified format header).
        assert snap.diff_text.startswith("diff --git")

    def test_base_ref_matching_head_produces_empty_diff(self, single_commit_repo):
        """HEAD..HEAD is a valid range that produces no diff. Should
        succeed with an empty string, not raise."""
        repo_path, _ = single_commit_repo
        snap = take_snapshot(repo_path, base_ref="HEAD")
        assert snap.base_ref == "HEAD"
        assert snap.diff_text == ""

    def test_base_ref_diff_ignores_operator_diff_format_config(self, single_commit_repo):
        repo_path, _ = single_commit_repo

        # Given: repo-local operator config that would remove a/ b/ prefixes
        # and force ANSI color into ordinary captured stdout.
        _git(repo_path, "config", "diff.noprefix", "true")
        _git(repo_path, "config", "diff.mnemonicPrefix", "true")
        _git(repo_path, "config", "color.ui", "always")
        (repo_path / "second.txt").write_text("added in second commit\n")
        _git(repo_path, "add", "-A")
        _git(repo_path, "commit", "-m", "second")

        # When: snapshot captures a base diff.
        snap = take_snapshot(repo_path, base_ref="HEAD~1")

        # Then: reviewer-facing diff filtering still receives canonical git
        # headers and color-free text regardless of that operator config.
        assert snap.diff_text.startswith("diff --git a/second.txt b/second.txt")
        assert "\x1b[" not in snap.diff_text


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestBogusBaseRef:
    def test_bogus_base_ref_raises_snapshot_error_naming_the_ref(self, single_commit_repo):
        repo_path, _ = single_commit_repo
        with pytest.raises(SnapshotError) as exc_info:
            take_snapshot(repo_path, base_ref="not-a-real-ref-xyz123")
        msg = str(exc_info.value)
        assert "not-a-real-ref-xyz123" in msg

    def test_typo_in_existing_branch_name_raises(self, single_commit_repo):
        """A near-miss like `mai` (instead of `main`) is a typo, not
        a valid ref. Should fail loudly rather than silently producing
        a stale empty diff."""
        repo_path, _ = single_commit_repo
        with pytest.raises(SnapshotError) as exc_info:
            take_snapshot(repo_path, base_ref="mai")
        assert "mai" in str(exc_info.value)


class TestNotAGitRepo:
    def test_plain_tmp_path_raises_snapshot_error(self, tmp_path):
        """take_snapshot on a directory that isn't a git working tree
        must raise SnapshotError with git's own 'not a git repository'
        message preserved."""
        with pytest.raises(SnapshotError) as exc_info:
            take_snapshot(tmp_path)
        msg = str(exc_info.value).lower()
        assert "not a git repository" in msg or "could not resolve head" in msg


class TestMissingRepoRoot:
    def test_nonexistent_path_raises_with_clear_message(self, tmp_path):
        bogus = tmp_path / "does-not-exist"
        with pytest.raises(SnapshotError) as exc_info:
            take_snapshot(bogus)
        assert "does not exist" in str(exc_info.value)

    def test_repo_root_pointing_at_a_file_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(SnapshotError) as exc_info:
            take_snapshot(f)
        assert "not a directory" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Repo-root discovery (PR-5.5: the canonical user-path -> repo-root resolver)
# ---------------------------------------------------------------------------


class TestDiscoverRepoRoot:
    def test_from_repo_root_is_a_no_op(self, single_commit_repo):
        """Invoking from the repo root itself returns the repo root —
        discovery is a no-op when the hint already IS the root."""
        repo_path, _ = single_commit_repo
        assert discover_repo_root(repo_path) == repo_path.resolve()

    def test_from_subdirectory_returns_repo_root(self, single_commit_repo):
        """The Acme field bug: a user who runs syncade from
        repo/your repo must still have the repo root discovered."""
        repo_path, _ = single_commit_repo
        subdir = repo_path / "docs" / "prs"
        subdir.mkdir(parents=True)
        assert discover_repo_root(subdir) == repo_path.resolve()

    def test_non_git_directory_raises_snapshot_error(self, tmp_path):
        non_git = tmp_path / "not-a-repo"
        non_git.mkdir()
        with pytest.raises(SnapshotError) as exc_info:
            discover_repo_root(non_git)
        assert "not inside a git repository" in str(exc_info.value).lower()

    def test_nonexistent_path_raises_snapshot_error(self, tmp_path):
        with pytest.raises(SnapshotError) as exc_info:
            discover_repo_root(tmp_path / "does-not-exist")
        assert "does not exist" in str(exc_info.value)

    def test_path_pointing_at_a_file_raises_snapshot_error(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(SnapshotError) as exc_info:
            discover_repo_root(f)
        assert "not a directory" in str(exc_info.value)
