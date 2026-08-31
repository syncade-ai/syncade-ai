"""Standalone Git repositories for untrusted producer subprocesses."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from syncade.env_scrub import path_is_relative_to
from syncade.git_object_id import is_full_git_object_id
from syncade.process import SubprocessError, run_subprocess
from syncade.workspace_owner import create_run_dir
from syncade.worktree import DEFAULT_WORKTREE_BASE, WorktreeError

_GIT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ProducerWorkspace:
    """One producer-owned repository pinned to a round-start commit."""

    path: Path
    commit_sha: str


def _git_env() -> dict[str, str]:
    """Return a Git environment without inherited routing or config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _run_git(argv: list[str], *, cwd: Path, operation: str) -> str:
    try:
        result = run_subprocess(
            argv,
            cwd=cwd,
            env=_git_env(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except SubprocessError as exc:
        raise WorktreeError(f"producer repository {operation} failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise WorktreeError(f"producer repository {operation} failed: {detail}")
    return result.stdout


def _discard(path: Path) -> None:
    """Remove only ``path`` itself; never follow a replacement symlink."""
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _file_inodes(root: Path) -> set[tuple[int, int]]:
    return {
        (entry.stat(follow_symlinks=False).st_dev, entry.stat(follow_symlinks=False).st_ino)
        for entry in root.rglob("*")
        if entry.is_file() and not entry.is_symlink()
    }


def _git_path(repo: Path, name: str, operation: str) -> Path:
    text = _run_git(["git", "rev-parse", "--git-path", name], cwd=repo, operation=operation).strip()
    path = Path(text)
    return (
        (repo / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
    )


def _common_dir(repo: Path, operation: str) -> Path:
    text = _run_git(["git", "rev-parse", "--git-common-dir"], cwd=repo, operation=operation).strip()
    path = Path(text)
    return (
        (repo / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
    )


def _assert_standalone(repo_root: Path, target: Path, commit_sha: str) -> None:
    git_dir = (target / ".git").resolve(strict=True)
    if not (target / ".git").is_dir() or git_dir != (target / ".git").resolve():
        raise WorktreeError("producer repository .git is not a real directory")

    common_text = _run_git(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=target,
        operation="common-directory verification",
    ).strip()
    common = Path(common_text)
    if not common.is_absolute():
        common = target / common
    if common.resolve(strict=True) != git_dir:
        raise WorktreeError("producer repository shares an external Git common directory")

    if _run_git(["git", "remote"], cwd=target, operation="remote verification").strip():
        raise WorktreeError("producer repository unexpectedly contains a remote")
    if (git_dir / "objects" / "info" / "alternates").exists():
        raise WorktreeError("producer repository unexpectedly contains object alternates")

    forbidden = {str(repo_root.absolute()).encode(), str(repo_root.resolve()).encode()}
    for entry in git_dir.rglob("*"):
        if (
            not entry.is_file()
            or entry.is_symlink()
            or "objects" in entry.relative_to(git_dir).parts
        ):
            continue
        try:
            data = entry.read_bytes()
        except OSError as exc:
            raise WorktreeError(
                f"producer repository metadata is unreadable: {entry}: {exc}"
            ) from exc
        if any(path in data for path in forbidden):
            raise WorktreeError(
                f"producer repository metadata names the operator repository: {entry}"
            )

    actor_inodes = _file_inodes(git_dir / "objects")
    operator_objects = _git_path(repo_root, "objects", "operator object-directory lookup")
    if actor_inodes and actor_inodes & _file_inodes(operator_objects):
        raise WorktreeError("producer and operator repositories share object-file inodes")
    head = _run_git(["git", "rev-parse", "HEAD"], cwd=target, operation="HEAD verification").strip()
    if head.lower() != commit_sha.lower():
        raise WorktreeError("producer repository HEAD differs from the round-start commit")


def _seed_repository(repo_root: Path, target: Path, commit_sha: str, temp_root: Path) -> None:
    object_type = _run_git(
        ["git", "--no-replace-objects", "cat-file", "-t", commit_sha],
        cwd=repo_root,
        operation="commit lookup",
    ).strip()
    if object_type != "commit":
        raise WorktreeError("producer repository requires a commit object ID")
    object_format = _run_git(
        ["git", "rev-parse", "--show-object-format"],
        cwd=repo_root,
        operation="object-format lookup",
    ).strip()
    if object_format not in {"sha1", "sha256"}:
        raise WorktreeError(f"producer repository unsupported object format: {object_format!r}")

    template = temp_root / "template"
    template.mkdir()
    init = ["git", "init", "--quiet", f"--template={template}"]
    if object_format == "sha256":
        init.append("--object-format=sha256")
    init.append(str(target))
    _run_git(init, cwd=temp_root, operation="init")
    _run_git(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            str(repo_root),
            commit_sha,
        ],
        cwd=target,
        operation="seed transfer",
    )
    _run_git(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "checkout",
            "--quiet",
            "--detach",
            commit_sha,
        ],
        cwd=target,
        operation="checkout",
    )
    for key, value in (
        ("user.name", "Syncade Producer"),
        ("user.email", "producer@syncade.invalid"),
        ("commit.gpgSign", "false"),
        ("tag.gpgSign", "false"),
    ):
        _run_git(["git", "config", "--local", key, value], cwd=target, operation="config")


class ProducerWorkspaceManager:
    """Provision and clean up standalone producer repositories."""

    def __init__(
        self,
        repo_root: Path,
        run_id: str,
        base_dir: Path = DEFAULT_WORKTREE_BASE,
        *,
        defer_cleanup: bool = False,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._run_id = run_id
        self._base_dir = Path(base_dir)
        self._workspaces: list[ProducerWorkspace] = []
        self._defer_cleanup = defer_cleanup

    @property
    def run_dir(self) -> Path:
        return self._base_dir / self._run_id

    def create(self, commit_sha: str) -> ProducerWorkspace:
        if not is_full_git_object_id(commit_sha):
            raise WorktreeError("producer repository requires a full commit object ID")
        target = (self.run_dir / "producer").absolute()
        if path_is_relative_to(target, self._repo_root) or path_is_relative_to(
            self._repo_root, target
        ):
            raise WorktreeError("producer repository must be separate from the operator repository")
        operator_git_dir = _common_dir(self._repo_root, "operator common-directory lookup")
        if path_is_relative_to(target, operator_git_dir) or path_is_relative_to(
            operator_git_dir, target
        ):
            raise WorktreeError("producer repository must be separate from operator Git storage")
        if os.path.lexists(target):
            raise WorktreeError(f"producer repository target already exists: {target}")

        try:
            create_run_dir(self._base_dir, self._run_id, self._repo_root)
            with tempfile.TemporaryDirectory(prefix=".producer-init-", dir=self.run_dir) as raw:
                _seed_repository(self._repo_root, target, commit_sha, Path(raw))
            _assert_standalone(self._repo_root, target, commit_sha)
            workspace = ProducerWorkspace(target, commit_sha.lower())
            self._workspaces.append(workspace)
            return workspace
        except OSError as exc:
            try:
                _discard(target)
            except OSError:
                pass
            raise WorktreeError(f"producer repository provisioning failed: {exc}") from exc
        except BaseException:
            try:
                _discard(target)
            except OSError:
                pass
            raise

    def cleanup(self, workspace: ProducerWorkspace) -> None:
        try:
            _discard(workspace.path)
        except OSError as exc:
            raise WorktreeError(f"producer repository cleanup failed: {exc}") from exc

    def cleanup_all(self) -> None:
        errors: list[str] = []
        for workspace in self._workspaces:
            try:
                self.cleanup(workspace)
            except WorktreeError as exc:
                print(
                    f"[syncade] warning: failed to clean up producer repository "
                    f"at {workspace.path}: {exc}",
                    file=sys.stderr,
                )
                errors.append(str(exc))
        try:
            self.run_dir.rmdir()
        except OSError:
            pass
        if errors:
            raise WorktreeError(
                f"cleanup_all: {len(errors)} of {len(self._workspaces)} producer "
                "repository(s) failed to clean up: " + "; ".join(errors)
            )

    def __enter__(self) -> ProducerWorkspaceManager:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is not None or self._defer_cleanup:
            return
        try:
            self.cleanup_all()
        except Exception as exc:
            print(f"[syncade] warning: producer repository cleanup failed: {exc}", file=sys.stderr)
