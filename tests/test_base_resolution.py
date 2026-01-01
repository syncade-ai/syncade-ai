"""PR-B Task 1: base/scope resolution (`syncade.base_resolution`).

Scope token → concrete base ref, fed into the existing `take_snapshot(base_ref)`
→ `git diff <base>..HEAD` path. Real git temp repos (no mocks) per TDD: the
resolver is git-shaped, so the tests exercise real merge-base / upstream /
default-branch behavior. The four operator-locked decisions are pinned here:

- `everything` → merge-base HEAD <default-branch>.
- `local` → merge-base HEAD @{upstream}; no upstream → fall back to the
  branch point (= everything) with a note.
- `since-last-review` → the recorded SHA; None → fall back to everything + note.
- truly unresolvable (no default branch) → BaseResolutionError.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.base_resolution import (
    BaseResolutionError,
    ResolvedBase,
    detect_default_branch,
    resolve_scope,
)

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> str:
    import os

    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_ENV},
    ).stdout.strip()


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", f"{ref}^{{commit}}")


def _commit(repo: Path, name: str, content: str = "x") -> str:
    (repo / name).write_text(content + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    return _rev(repo, "HEAD")


def _make_repo(tmp_path: Path, default: str = "main", *, object_format: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_args = ["init", "-q", "-b", default]
    if object_format is not None:
        init_args.append(f"--object-format={object_format}")
    _git(repo, *init_args)
    _git(repo, "config", "commit.gpgsign", "false")
    _commit(repo, "a.txt", "1")
    return repo


def _feature_branch(repo: Path, name: str = "feature") -> None:
    _git(repo, "checkout", "-q", "-b", name)


class TestDetectDefaultBranch:
    def test_main_when_present_no_remote(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        assert detect_default_branch(repo) == "main"

    def test_master_when_no_main(self, tmp_path):
        repo = _make_repo(tmp_path, default="master")
        assert detect_default_branch(repo) == "master"

    def test_origin_head_wins(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        # Simulate a remote-tracking default without a live remote: create the
        # refs/remotes/origin/* refs + origin/HEAD symbolic-ref pointing at them.
        head = _rev(repo, "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/trunk", head)
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        assert detect_default_branch(repo) == "origin/trunk"

    def test_none_when_no_default(self, tmp_path):
        repo = _make_repo(tmp_path, default="wip")  # neither main nor master
        assert detect_default_branch(repo) is None


class TestResolveScope:
    def test_everything_is_branch_point_off_default(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        branch_point = _rev(repo, "HEAD")  # main tip before feature
        _feature_branch(repo)
        _commit(repo, "b.txt")
        _commit(repo, "c.txt")
        rb = resolve_scope(repo, "everything")
        assert isinstance(rb, ResolvedBase)
        assert rb.base_sha == branch_point
        assert rb.note is None

    def test_everything_supports_sha256_object_ids(self, tmp_path):
        try:
            repo = _make_repo(tmp_path, default="main", object_format="sha256")
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"git does not support sha256 object-format: {exc.stderr}")
        branch_point = _rev(repo, "HEAD")
        assert len(branch_point) == 64

        _feature_branch(repo)
        _commit(repo, "b.txt")

        rb = resolve_scope(repo, "everything")
        assert rb.base_sha == branch_point

    def test_local_is_merge_base_with_upstream(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        _feature_branch(repo)
        upstream_tip = _commit(repo, "b.txt")  # pushed → becomes @{upstream}
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-q", "-u", "origin", "feature")
        _commit(repo, "c.txt")  # 1 local-ahead commit
        _commit(repo, "d.txt")  # 2 local-ahead commits
        rb = resolve_scope(repo, "local")
        assert rb.base_sha == upstream_tip
        assert rb.note is None

    def test_local_without_upstream_falls_back_to_branch_point(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        branch_point = _rev(repo, "HEAD")
        _feature_branch(repo)
        _commit(repo, "b.txt")
        rb = resolve_scope(repo, "local")  # no upstream configured
        assert rb.base_sha == branch_point
        assert rb.note is not None and "upstream" in rb.note.lower()

    def test_since_last_review_uses_recorded_sha(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        recorded = _rev(repo, "HEAD")
        _commit(repo, "b.txt")
        rb = resolve_scope(repo, "since-last-review", last_reviewed_sha=recorded)
        assert rb.base_sha == recorded
        assert rb.note is None

    def test_since_last_review_non_ancestor_falls_back_to_branch_point(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        branch_point = _rev(repo, "HEAD")
        _feature_branch(repo, "already-reviewed")
        recorded_on_other_branch = _commit(repo, "reviewed.txt")
        _git(repo, "checkout", "-q", "main")
        _feature_branch(repo, "current-work")
        _commit(repo, "current.txt")

        rb = resolve_scope(
            repo,
            "since-last-review",
            last_reviewed_sha=recorded_on_other_branch,
        )

        assert rb.base_sha == branch_point
        assert rb.note is not None and "not an ancestor" in rb.note.lower()

    def test_since_last_review_without_record_falls_back_to_everything(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        branch_point = _rev(repo, "HEAD")
        _feature_branch(repo)
        _commit(repo, "b.txt")
        rb = resolve_scope(repo, "since-last-review", last_reviewed_sha=None)
        assert rb.base_sha == branch_point
        assert rb.note is not None and "no prior review" in rb.note.lower()

    def test_unresolvable_default_raises(self, tmp_path):
        repo = _make_repo(tmp_path, default="wip")  # no main/master, no remote
        with pytest.raises(BaseResolutionError):
            resolve_scope(repo, "everything")

    def test_local_with_unrelated_upstream_raises(self, tmp_path):
        """@{upstream} resolves but shares no merge-base with HEAD → BaseResolutionError."""
        # Build a bare remote whose "feature" branch has an unrelated root commit.
        bare = tmp_path / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        src = tmp_path / "src"
        src.mkdir()
        _git(src, "init", "-q", "-b", "feature")
        _git(src, "config", "commit.gpgsign", "false")
        _commit(src, "z.txt", "remote work")
        _git(src, "remote", "add", "origin", str(bare))
        _git(src, "push", "-q", "origin", "feature:feature")

        # Main repo: orphan "feature" branch tracking origin/feature (unrelated).
        repo = _make_repo(tmp_path, default="main")
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "fetch", "-q", "origin")
        _git(repo, "checkout", "--orphan", "feature")
        _git(repo, "rm", "-rf", "--cached", ".")
        _commit(repo, "local.txt", "local work")
        _git(repo, "branch", "--set-upstream-to", "origin/feature", "feature")

        with pytest.raises(BaseResolutionError, match="unrelated"):
            resolve_scope(repo, "local")

    def test_invalid_scope_raises(self, tmp_path):
        repo = _make_repo(tmp_path, default="main")
        with pytest.raises(ValueError):
            resolve_scope(repo, "bogus")
