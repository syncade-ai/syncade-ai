"""Adversarial integration tests for Git-less reviewer exports."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

import syncade.reviewer_workspace as reviewer_workspace_module
from syncade.reviewer_workspace import (
    ReviewerWorkspace,
    ReviewerWorkspaceManager,
    stage_reviewer_file,
)
from syncade.workspace_owner import OWNER_RECORD_NAME
from syncade.worktree import WorktreeError
from tests.worktree._helpers import _git

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not found on PATH",
)


def _snapshot_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")

    (repo / ".gitattributes").write_text(
        "archive-only.txt export-ignore\n"
        "crlf.txt text eol=crlf\n"
        "ident.txt ident\n"
        "template.txt export-subst\n"
        "utf16.txt text working-tree-encoding=UTF-16LE\n"
    )
    (repo / "archive-only.txt").write_text("must survive export-ignore\n")
    (repo / "binary.bin").write_bytes(b"\x00\xffpinned\r\n")
    (repo / "crlf.txt").write_bytes(b"line 1\nline 2\n")
    (repo / "deleted-later.txt").write_text("present at pinned commit\n")
    (repo / "ident.txt").write_text("literal $Id$ marker\n")
    (repo / "old-name.txt").write_text("old path at pinned commit\n")
    (repo / "script.sh").write_text("#!/bin/sh\necho pinned\n")
    (repo / "script.sh").chmod(0o755)
    (repo / "target.txt").write_text("link target\n")
    (repo / "template.txt").write_text("literal $Format:%H$ marker\n")
    (repo / "utf16.txt").write_bytes("encoded line\n".encode("utf-16-le"))
    (repo / "CLAUDE.md").write_text("root secret\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "CLAUDE.md").write_text("nested secret\n")
    os.symlink("target.txt", repo / "target-link")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pinned")
    pinned = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "mv", "old-name.txt", "new-name.txt")
    (repo / "deleted-later.txt").unlink()
    (repo / "binary.bin").write_bytes(b"newer")
    (repo / "script.sh").write_text("#!/bin/sh\necho newer\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "moving head")
    moving_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # A replacement ref makes an unguarded lookup of ``pinned`` resolve to
    # the newer tree. The exporter must still materialize the named object.
    _git(repo, "replace", "-f", pinned, moving_head)
    (repo / "operator-only.txt").write_text("untracked\n")
    return repo, pinned


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlink and mode semantics")
def test_export_is_exact_pinned_tree_then_stripped_and_has_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, pinned = _snapshot_repo(tmp_path)
    ident_oid = _git(repo, "rev-parse", f"{pinned}:ident.txt").stdout.strip()
    reference = tmp_path / "reference"
    _git(
        repo,
        "--no-replace-objects",
        "worktree",
        "add",
        "--quiet",
        "--detach",
        str(reference),
        pinned,
    )

    # None of these inherited routing/config variables may steer the trusted
    # export Git calls away from ``repo`` or its private temporary index.
    poison = tmp_path / "poison"
    poison.mkdir()
    routed_keys = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    )
    for key in routed_keys:
        monkeypatch.setenv(key, str(poison))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(poison))

    manager = ReviewerWorkspaceManager(repo, "run", base_dir=tmp_path / "exports")
    workspace = manager.create("reviewer", pinned, strip_files=["CLAUDE.md"])

    assert isinstance(workspace, ReviewerWorkspace)
    assert workspace.commit_sha == pinned
    assert (workspace.path / "binary.bin").read_bytes() == b"\x00\xffpinned\r\n"
    assert (workspace.path / "binary.bin").read_bytes() == (reference / "binary.bin").read_bytes()
    assert (workspace.path / "crlf.txt").read_bytes() == b"line 1\r\nline 2\r\n"
    assert (workspace.path / "crlf.txt").read_bytes() == (reference / "crlf.txt").read_bytes()
    assert (workspace.path / "ident.txt").read_text() == (f"literal $Id: {ident_oid} $ marker\n")
    assert (workspace.path / "ident.txt").read_bytes() == (reference / "ident.txt").read_bytes()
    assert (workspace.path / "script.sh").read_text() == "#!/bin/sh\necho pinned\n"
    assert stat.S_IMODE((workspace.path / "script.sh").stat().st_mode) & stat.S_IXUSR
    assert (workspace.path / "target-link").is_symlink()
    assert os.readlink(workspace.path / "target-link") == "target.txt"
    assert (workspace.path / "deleted-later.txt").read_text() == ("present at pinned commit\n")
    assert (workspace.path / "old-name.txt").exists()
    assert not (workspace.path / "new-name.txt").exists()
    assert (workspace.path / "archive-only.txt").read_text() == ("must survive export-ignore\n")
    assert (workspace.path / "template.txt").read_text() == ("literal $Format:%H$ marker\n")
    assert (workspace.path / "utf16.txt").read_bytes() == "encoded line\n".encode("utf-16-le")
    assert (workspace.path / "utf16.txt").read_bytes() == (reference / "utf16.txt").read_bytes()
    assert not (workspace.path / "operator-only.txt").exists()
    assert not (workspace.path / "CLAUDE.md").exists()
    assert not (workspace.path / "docs" / "CLAUDE.md").exists()
    assert not os.path.lexists(workspace.path / ".git")
    assert {path.name for path in manager.run_dir.iterdir()} == {"reviewer", OWNER_RECORD_NAME}

    # Now prove ordinary local Git recovery fails without relying on the
    # deliberately broken inherited environment above.
    for key in (*routed_keys, "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(key)
    assert _git(workspace.path, "rev-parse", "--git-dir", check=False).returncode != 0
    assert _git(workspace.path, "show", "HEAD:CLAUDE.md", check=False).returncode != 0


@pytest.mark.skipif(os.name == "nt", reason="adversarial smudge command uses a POSIX shell")
@pytest.mark.parametrize("filter_scope", ["local", "global", "system"])
def test_export_blocks_external_smudge_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filter_scope: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    secret = repo / "operator-secret"
    filter_command = f"sh -c 'cat; cat \"$1\"' - {secret}"
    if filter_scope == "global":
        home = tmp_path / "host-home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
        _git(repo, "config", "--global", "filter.leak.smudge", filter_command)
    elif filter_scope == "system":
        system_config = tmp_path / "system.gitconfig"
        _git(repo, "config", "--file", str(system_config), "filter.leak.smudge", filter_command)
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))
    else:
        _git(repo, "config", "--local", "filter.leak.smudge", filter_command)
    (repo / ".gitattributes").write_text("filtered.txt filter=leak\n", encoding="utf-8")
    (repo / "filtered.txt").write_text("clean-content\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "filtered")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    secret.write_text("SMUDGE-SECRET\n", encoding="utf-8")

    # Control: the checkout-index mechanism this replaced executes the smudge
    # driver and folds the untracked secret into reviewer-visible bytes.
    control = tmp_path / "checkout-control"
    control.mkdir()
    _git(repo, "checkout-index", "--all", f"--prefix={control}{os.sep}")
    assert (control / "filtered.txt").read_text(encoding="utf-8") == (
        "clean-content\nSMUDGE-SECRET\n"
    )

    manager = ReviewerWorkspaceManager(repo, "run", base_dir=tmp_path / "exports")
    workspace = manager.create("reviewer", sha)

    assert (workspace.path / "filtered.txt").read_text(encoding="utf-8") == "clean-content\n"
    assert not (workspace.path / "operator-secret").exists()


def test_export_materializes_gitlinks_like_a_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("tracked\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "base")
    nested_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{nested_sha},vendor/sub")
    _git(repo, "commit", "-qm", "gitlink")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    reference = tmp_path / "reference"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(reference), sha)
    manager = ReviewerWorkspaceManager(repo, "run", base_dir=tmp_path / "exports")
    workspace = manager.create("reviewer", sha)

    exported = workspace.path / "vendor" / "sub"
    checked_out = reference / "vendor" / "sub"
    assert exported.is_dir() and checked_out.is_dir()
    assert list(exported.iterdir()) == list(checked_out.iterdir()) == []


def test_export_supports_sha256_repositories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    initialized = _git(repo, "init", "-q", "--object-format=sha256", check=False)
    if initialized.returncode != 0:
        pytest.skip(f"git does not support sha256 object format: {initialized.stderr}")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("sha256 content\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "sha256")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    manager = ReviewerWorkspaceManager(repo, "run", base_dir=tmp_path / "exports")
    workspace = manager.create("reviewer", sha)

    assert len(sha) == 64
    assert workspace.commit_sha == sha
    assert (workspace.path / "tracked.txt").read_text() == "sha256 content\n"
    assert not os.path.lexists(workspace.path / ".git")


def test_create_requires_full_sha_and_rolls_back_failed_exports(
    repo: tuple[Path, str], base_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path, sha = repo
    manager = ReviewerWorkspaceManager(repo_path, "run", base_dir=base_dir)

    with pytest.raises(WorktreeError, match="full commit object ID"):
        manager.create("short", sha[:8])
    assert not manager.run_dir.exists()

    def fail_export(*args, **kwargs) -> None:
        raise WorktreeError("simulated export failure")

    monkeypatch.setattr(reviewer_workspace_module, "_export_checkout", fail_export)
    target = (manager.run_dir / "reviewer").absolute()
    with pytest.raises(WorktreeError, match="simulated export failure"):
        manager.create("reviewer", sha)

    assert not os.path.lexists(target)
    assert manager._workspaces == []
    # The rollback leaves no partial WORKSPACE. The ownership record is not one:
    # it names the run root this manager created, so a directory that survives a
    # failed export is reclaimable by GC instead of sitting there unowned.
    assert [p.name for p in manager.run_dir.iterdir()] == [OWNER_RECORD_NAME]


def test_create_maps_filesystem_setup_failure_to_worktree_error(
    repo: tuple[Path, str], base_dir: Path
) -> None:
    repo_path, sha = repo
    manager = ReviewerWorkspaceManager(repo_path, "blocked", base_dir=base_dir)
    manager.run_dir.parent.mkdir(parents=True)
    manager.run_dir.write_text("not a directory\n")

    with pytest.raises(WorktreeError, match="reviewer export failed"):
        manager.create("reviewer", sha)


def test_create_refuses_repo_local_export_root_without_side_effects(
    repo: tuple[Path, str],
) -> None:
    repo_path, sha = repo
    base_dir = repo_path / ".syncade" / "reviewers"
    manager = ReviewerWorkspaceManager(repo_path, "run", base_dir=base_dir)

    with pytest.raises(WorktreeError, match="inside the operator repository"):
        manager.create("reviewer", sha)

    assert not base_dir.exists()
    assert manager._workspaces == []


@pytest.mark.skipif(os.pathsep != ":", reason="colon path separator attack is POSIX-specific")
def test_create_refuses_path_list_separator_in_git_ceiling(
    repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    repo_path, sha = repo
    base_dir = tmp_path / "colon:name" / "exports"
    manager = ReviewerWorkspaceManager(repo_path, "run", base_dir=base_dir)

    with pytest.raises(WorktreeError, match="path-list separator"):
        manager.create("reviewer", sha)

    assert not base_dir.exists()
    assert manager._workspaces == []


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_create_refuses_existing_target_without_mutating_it(
    repo: tuple[Path, str], base_dir: Path, kind: str
) -> None:
    repo_path, sha = repo
    manager = ReviewerWorkspaceManager(repo_path, f"run-{kind}", base_dir=base_dir)
    target = manager.run_dir / "reviewer"
    target.parent.mkdir(parents=True)
    if kind == "directory":
        target.mkdir()
        sentinel = target / "sentinel"
    else:
        sentinel = target
    sentinel.write_text("keep\n")

    with pytest.raises(WorktreeError, match="target already exists"):
        manager.create("reviewer", sha)

    assert sentinel.read_text() == "keep\n"
    assert manager._workspaces == []


@pytest.mark.skipif(os.name == "nt", reason="requires symlinks")
def test_create_refuses_dangling_symlink_target(
    repo: tuple[Path, str], base_dir: Path, tmp_path: Path
) -> None:
    repo_path, sha = repo
    manager = ReviewerWorkspaceManager(repo_path, "run-link", base_dir=base_dir)
    target = manager.run_dir / "reviewer"
    target.parent.mkdir(parents=True)
    missing = tmp_path / "must-stay-missing"
    os.symlink(missing, target)

    with pytest.raises(WorktreeError, match="target already exists"):
        manager.create("reviewer", sha)

    assert target.is_symlink()
    assert os.readlink(target) == str(missing)
    assert not missing.exists()


def test_workspace_lifecycle_cleans_success_and_preserves_deferred(
    repo: tuple[Path, str], base_dir: Path
) -> None:
    repo_path, sha = repo
    automatic = ReviewerWorkspaceManager(repo_path, "automatic", base_dir=base_dir)
    with automatic as manager:
        auto_path = manager.create("reviewer", sha).path
        assert auto_path.is_dir()
    assert not auto_path.exists()

    deferred = ReviewerWorkspaceManager(
        repo_path, "deferred", base_dir=base_dir, defer_cleanup=True
    )
    with deferred as manager:
        deferred_path = manager.create("reviewer", sha).path
    assert deferred_path.is_dir()
    deferred.cleanup_all()
    assert not deferred_path.exists()


def test_workspace_lifecycle_preserves_failure_for_inspection(
    repo: tuple[Path, str], base_dir: Path
) -> None:
    repo_path, sha = repo
    manager = ReviewerWorkspaceManager(repo_path, "failed", base_dir=base_dir)
    with pytest.raises(RuntimeError, match="review failed"):
        with manager:
            path = manager.create("reviewer", sha).path
            raise RuntimeError("review failed")
    assert path.is_dir()
    manager.cleanup_all()


@pytest.mark.skipif(os.name == "nt", reason="requires symlinks")
def test_cleanup_unlinks_replacement_symlink_without_following_it(
    repo: tuple[Path, str], base_dir: Path, tmp_path: Path
) -> None:
    repo_path, sha = repo
    manager = ReviewerWorkspaceManager(repo_path, "replaced", base_dir=base_dir)
    workspace = manager.create("reviewer", sha)
    outside = tmp_path / "outside-cleanup"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep\n")

    shutil.rmtree(workspace.path)
    os.symlink(outside, workspace.path)
    manager.cleanup(workspace)

    assert sentinel.read_text() == "keep\n"
    assert not os.path.lexists(workspace.path)


def test_stage_reviewer_file_replaces_only_regular_destination(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "brief.md"
    source.write_bytes(b"first\x00\xff")

    stage_reviewer_file(workspace, source, ".syncade-inputs/brief.md")
    destination = workspace / ".syncade-inputs" / "brief.md"
    assert destination.read_bytes() == b"first\x00\xff"

    source.write_bytes(b"second")
    stage_reviewer_file(workspace, source, ".syncade-inputs/brief.md")
    assert destination.read_bytes() == b"second"


@pytest.mark.skipif(os.name == "nt", reason="requires symlinks")
def test_stage_reviewer_file_refuses_symlink_collisions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "brief.md"
    source.write_text("authoritative\n")
    outside = tmp_path / "outside"
    outside.mkdir()

    parent_workspace = tmp_path / "parent-workspace"
    parent_workspace.mkdir()
    os.symlink(outside, parent_workspace / ".syncade-inputs")
    with pytest.raises(WorktreeError, match="parent is not a directory"):
        stage_reviewer_file(parent_workspace, source, ".syncade-inputs/brief.md")
    assert not (outside / "brief.md").exists()

    leaf_workspace = tmp_path / "leaf-workspace"
    (leaf_workspace / ".syncade-inputs").mkdir(parents=True)
    outside_leaf = outside / "leaf.md"
    outside_leaf.write_text("keep\n")
    os.symlink(outside_leaf, leaf_workspace / ".syncade-inputs" / "brief.md")
    with pytest.raises(WorktreeError, match="destination is not a regular file"):
        stage_reviewer_file(leaf_workspace, source, ".syncade-inputs/brief.md")
    assert outside_leaf.read_text() == "keep\n"


@pytest.mark.parametrize("relative_ref", ["../brief.md", "/brief.md", "."])
def test_stage_reviewer_file_rejects_paths_outside_workspace(
    tmp_path: Path, relative_ref: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "brief.md"
    source.write_text("brief\n")

    with pytest.raises(WorktreeError, match="invalid reviewer input path"):
        stage_reviewer_file(workspace, source, relative_ref)
