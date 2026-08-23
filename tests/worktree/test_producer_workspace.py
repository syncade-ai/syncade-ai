"""Adversarial integration tests for standalone producer repositories."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

import syncade.producer_workspace as producer_workspace_module
from syncade.producer_workspace import ProducerWorkspace, ProducerWorkspaceManager
from syncade.worktree import WorktreeError
from tests.worktree._helpers import _git

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not found on PATH",
)


def _repo(root: Path) -> tuple[Path, str]:
    repo = root / "operator"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "operator@example.invalid")
    _git(repo, "config", "user.name", "Operator")
    (repo / "tracked.txt").write_text("pinned\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "pinned")
    pinned = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "tracked.txt").write_text("moving\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "moving")
    return repo, pinned


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for entry in sorted(root.rglob("*")):
        relative = entry.relative_to(root).as_posix().encode()
        if entry.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + os.readlink(entry).encode())
        elif entry.is_file():
            digest.update(b"F\0" + relative + b"\0" + entry.read_bytes())
    return digest.hexdigest()


def _inodes(root: Path) -> set[tuple[int, int]]:
    return {
        (entry.stat().st_dev, entry.stat().st_ino)
        for entry in root.rglob("*")
        if entry.is_file() and not entry.is_symlink()
    }


def test_create_is_pinned_physically_standalone_and_has_no_backpointer(tmp_path: Path) -> None:
    repo, pinned = _repo(tmp_path)
    _git(repo, "gc", "--quiet")
    manager = ProducerWorkspaceManager(repo, "run", base_dir=tmp_path / "stores")
    workspace = manager.create(pinned)

    assert isinstance(workspace, ProducerWorkspace)
    assert workspace.commit_sha == pinned
    assert (workspace.path / "tracked.txt").read_text(encoding="utf-8") == "pinned\n"
    assert (workspace.path / ".git").is_dir() and not (workspace.path / ".git").is_symlink()
    common = Path(_git(workspace.path, "rev-parse", "--git-common-dir").stdout.strip())
    assert (workspace.path / common).resolve() == (workspace.path / ".git").resolve()
    assert _git(workspace.path, "remote").stdout == ""
    assert not (workspace.path / ".git" / "objects" / "info" / "alternates").exists()
    assert not (_inodes(repo / ".git" / "objects") & _inodes(workspace.path / ".git" / "objects"))

    forbidden = {str(repo.absolute()).encode(), str(repo.resolve()).encode()}
    for entry in (workspace.path / ".git").rglob("*"):
        if not entry.is_file() or "objects" in entry.relative_to(workspace.path / ".git").parts:
            continue
        assert not any(path in entry.read_bytes() for path in forbidden), entry


def test_linked_operator_worktree_still_seeds_standalone_actor(tmp_path: Path) -> None:
    repo, pinned = _repo(tmp_path)
    linked = tmp_path / "operator-worktree"
    _git(repo, "worktree", "add", "--detach", str(linked), pinned)

    workspace = ProducerWorkspaceManager(linked, "run", base_dir=tmp_path / "stores").create(pinned)

    assert (linked / ".git").is_file()
    assert (workspace.path / ".git").is_dir()
    assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == pinned
    assert _git(workspace.path, "remote").stdout == ""


def test_actor_ref_mutation_cannot_change_operator_git_state(tmp_path: Path) -> None:
    repo, pinned = _repo(tmp_path)
    control = repo / ".git" / "syncade-control-sentinel"
    control.write_bytes(b"operator-control\x00bytes\n")
    operator_git_before = _tree_digest(repo / ".git")
    operator_main_before = _git(repo, "rev-parse", "refs/heads/main").stdout.strip()

    manager = ProducerWorkspaceManager(repo, "run", base_dir=tmp_path / "stores")
    workspace = manager.create(pinned)
    operator_after_provision = _tree_digest(repo / ".git")
    _git(workspace.path, "update-ref", "refs/heads/main", pinned)

    assert _git(workspace.path, "rev-parse", "refs/heads/main").stdout.strip() == pinned
    assert _git(repo, "rev-parse", "refs/heads/main").stdout.strip() == operator_main_before
    assert control.read_bytes() == b"operator-control\x00bytes\n"
    assert operator_after_provision == operator_git_before
    assert _tree_digest(repo / ".git") == operator_git_before


def test_inherited_git_routing_and_hostile_config_cannot_steer_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, pinned = _repo(tmp_path)
    poison = tmp_path / "poison"
    poison.mkdir()
    _git(poison, "init", "-q")
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_TEMPLATE_DIR",
    ):
        monkeypatch.setenv(key, str(poison))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(poison))

    manager = ProducerWorkspaceManager(repo, "run", base_dir=tmp_path / "stores")
    workspace = manager.create(pinned)

    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_TEMPLATE_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        monkeypatch.delenv(key)
    assert _git(workspace.path, "rev-parse", "HEAD").stdout.strip() == pinned
    assert not (workspace.path / ".git" / "hooks").exists()
    assert _git(workspace.path, "remote").stdout == ""


def test_create_rejects_unsafe_paths_short_ids_and_rolls_back_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, pinned = _repo(tmp_path)
    inside = ProducerWorkspaceManager(repo, "run", base_dir=repo / "stores")
    with pytest.raises(WorktreeError, match="separate"):
        inside.create(pinned)

    manager = ProducerWorkspaceManager(repo, "run", base_dir=tmp_path / "stores")
    with pytest.raises(WorktreeError, match="full commit object ID"):
        manager.create(pinned[:12])

    def fail_seed(*args, **kwargs) -> None:
        raise WorktreeError("simulated seed failure")

    monkeypatch.setattr(producer_workspace_module, "_seed_repository", fail_seed)
    with pytest.raises(WorktreeError, match="simulated seed failure"):
        manager.create(pinned)
    assert not (manager.run_dir / "producer").exists()


def test_cleanup_is_idempotent_and_context_preserves_on_error(tmp_path: Path) -> None:
    repo, pinned = _repo(tmp_path)
    manager = ProducerWorkspaceManager(repo, "run", base_dir=tmp_path / "stores")
    workspace = manager.create(pinned)
    manager.cleanup(workspace)
    manager.cleanup(workspace)
    assert not workspace.path.exists()

    preserved = ProducerWorkspaceManager(repo, "preserve", base_dir=tmp_path / "stores")
    with pytest.raises(RuntimeError, match="boom"):
        with preserved as entered:
            kept = entered.create(pinned)
            raise RuntimeError("boom")
    assert kept.path.is_dir()
