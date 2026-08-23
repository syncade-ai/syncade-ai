"""Git-less snapshot workspaces for blind reviewers."""

from __future__ import annotations

import hashlib
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
from syncade.worktree import DEFAULT_WORKTREE_BASE, WorktreeError
from syncade.worktree_env import reviewer_git_ceiling
from syncade.worktree_paths import _strip_files, _validate_reviewer_name

_GIT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ReviewerWorkspace:
    path: Path
    reviewer_name: str
    commit_sha: str


def reviewer_input_ref(repo_root: Path, source: Path, strip_files: list[str]) -> str:
    """Return the workspace-local path for one authoritative review brief."""
    try:
        relative = source.relative_to(repo_root)
    except ValueError:
        relative = None
    stripped = {
        name
        for name in strip_files
        if name and name not in {".", ".."} and "/" not in name and not Path(name).is_absolute()
    }
    if relative is not None and source.name not in stripped:
        return str(relative)
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    return f".syncade-inputs/pr-doc-{digest}-{source.name}"


def _discard(path: Path) -> None:
    """Remove only ``path`` itself; never follow a replacement symlink."""
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _export_git_env() -> dict[str, str]:
    """Run export plumbing against cwd, not inherited Git routing."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _run_git(argv: list[str], *, cwd: Path, env: dict[str, str], operation: str) -> str:
    try:
        result = run_subprocess(argv, cwd=cwd, env=env, timeout=_GIT_TIMEOUT_SECONDS)
    except SubprocessError as exc:
        raise WorktreeError(f"reviewer export {operation} failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise WorktreeError(f"reviewer export {operation} failed: {detail}")
    return result.stdout


def _isolated_git_env(temp_root: Path) -> dict[str, str]:
    """Return a Git environment with no user, system, or inherited config."""
    home = temp_root / "home"
    xdg = temp_root / "xdg"
    home.mkdir()
    xdg.mkdir()
    env = _export_git_env()
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg),
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    return env


def _export_checkout(repo_root: Path, target: Path, commit_sha: str, temp_root: Path) -> None:
    source_env = _export_git_env()
    obj_type = _run_git(
        ["git", "--no-replace-objects", "cat-file", "-t", commit_sha],
        cwd=repo_root,
        env=source_env,
        operation="commit lookup",
    ).strip()
    if obj_type != "commit":
        raise WorktreeError("reviewer export requires commit object ID")

    object_format = _run_git(
        ["git", "--no-replace-objects", "rev-parse", "--show-object-format"],
        cwd=repo_root,
        env=source_env,
        operation="object-format lookup",
    ).strip()
    if object_format not in {"sha1", "sha256"}:
        raise WorktreeError(f"reviewer export unsupported object format: {object_format!r}")
    object_dir = Path(
        _run_git(
            ["git", "--no-replace-objects", "rev-parse", "--git-path", "objects"],
            cwd=repo_root,
            env=source_env,
            operation="object-directory lookup",
        ).strip()
    )
    if not object_dir.is_absolute():
        object_dir = repo_root / object_dir
    object_dir = object_dir.resolve(strict=True)
    if not object_dir.is_dir():
        raise WorktreeError("reviewer export object path is not a directory")

    env = _isolated_git_env(temp_root)
    control_dir = temp_root / "control.git"
    template_dir = temp_root / "template"
    template_dir.mkdir()
    init_argv = ["git", "init", "--quiet", "--bare", f"--template={template_dir}"]
    if object_format == "sha256":
        init_argv.append("--object-format=sha256")
    init_argv.append(str(control_dir))
    _run_git(init_argv, cwd=temp_root, env=env, operation="control repository init")

    env.update(
        {
            "GIT_DIR": str(control_dir),
            "GIT_INDEX_FILE": str(temp_root / "index"),
            "GIT_OBJECT_DIRECTORY": str(object_dir),
            "GIT_WORK_TREE": str(target),
        }
    )
    _run_git(
        ["git", "--no-replace-objects", "read-tree", "--reset", commit_sha],
        cwd=target,
        env=env,
        operation="private index read",
    )
    _run_git(
        [
            "git",
            "--no-replace-objects",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.fileMode=true",
            "-c",
            "core.symlinks=true",
            "checkout-index",
            "--all",
            "--force",
            f"--prefix={target}{os.sep}",
        ],
        cwd=target,
        env=env,
        operation="private index checkout",
    )


def _assert_stripped(root: Path, names: list[str]) -> None:
    eligible = {
        name
        for name in names
        if name and name not in {".", ".."} and "/" not in name and not Path(name).is_absolute()
    }
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        for name in (*dirnames, *filenames):
            candidate = current_path / name
            if name in eligible and (candidate.is_file() or candidate.is_symlink()):
                relative = candidate.relative_to(root)
                raise WorktreeError(f"reviewer export could not strip {relative}")


def stage_reviewer_file(
    workspace_root: Path,
    source: Path,
    relative_ref: str,
) -> None:
    """Copy one authoritative input without following workspace symlinks."""
    relative = Path(relative_ref)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise WorktreeError(f"invalid reviewer input path: {relative_ref!r}")

    parent = workspace_root
    for part in relative.parts[:-1]:
        parent /= part
        if os.path.lexists(parent):
            if parent.is_symlink() or not parent.is_dir():
                raise WorktreeError(f"reviewer input parent is not a directory: {parent}")
        else:
            parent.mkdir()

    destination = workspace_root / relative
    if os.path.lexists(destination):
        if destination.is_symlink() or not destination.is_file():
            raise WorktreeError(f"reviewer input destination is not a regular file: {destination}")
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise WorktreeError(f"could not stage reviewer input {relative_ref!r}: {exc}") from exc


class ReviewerWorkspaceManager:
    """Export pinned trees for reviewers and own their filesystem lifecycle."""

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
        self._workspaces: list[ReviewerWorkspace] = []
        self._defer_cleanup = defer_cleanup

    @property
    def run_dir(self) -> Path:
        return self._base_dir / self._run_id

    def create(
        self,
        reviewer_name: str,
        commit_sha: str,
        strip_files: list[str] | None = None,
    ) -> ReviewerWorkspace:
        _validate_reviewer_name(reviewer_name)
        if not is_full_git_object_id(commit_sha):
            raise WorktreeError("reviewer export requires a full commit object ID")

        target = (self.run_dir / reviewer_name).absolute()
        try:
            if path_is_relative_to(target, self._repo_root):
                raise WorktreeError(
                    "reviewer export target must not be inside the operator repository"
                )
            reviewer_git_ceiling(target)
        except ValueError as exc:
            raise WorktreeError(f"reviewer export target is not safe: {exc}") from exc
        if os.path.lexists(target):
            raise WorktreeError(f"reviewer export target already exists: {target}")
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            target.mkdir()
            with tempfile.TemporaryDirectory(prefix=".reviewer-export-", dir=self.run_dir) as raw:
                _export_checkout(self._repo_root, target, commit_sha, Path(raw))
            if os.path.lexists(target / ".git"):
                raise WorktreeError("reviewer export unexpectedly contains .git")
            if strip_files:
                _strip_files(target, strip_files)
                _assert_stripped(target, strip_files)
            workspace = ReviewerWorkspace(target, reviewer_name, commit_sha.lower())
            self._workspaces.append(workspace)
            return workspace
        except OSError as exc:
            try:
                _discard(target)
            except OSError:
                pass
            raise WorktreeError(f"reviewer export failed: {exc}") from exc
        except BaseException:
            try:
                _discard(target)
            except OSError:
                pass
            raise

    def cleanup(self, workspace: ReviewerWorkspace) -> None:
        try:
            _discard(workspace.path)
        except OSError as exc:
            raise WorktreeError(f"reviewer workspace cleanup failed: {exc}") from exc

    def cleanup_all(self) -> None:
        errors: list[str] = []
        for workspace in self._workspaces:
            try:
                self.cleanup(workspace)
            except WorktreeError as exc:
                print(
                    f"[syncade] warning: failed to clean up reviewer workspace "
                    f"{workspace.reviewer_name!r} at {workspace.path}: {exc}",
                    file=sys.stderr,
                )
                errors.append(str(exc))
        try:
            self.run_dir.rmdir()
        except OSError:
            pass
        if errors:
            raise WorktreeError(
                f"cleanup_all: {len(errors)} of {len(self._workspaces)} reviewer "
                "workspace(s) failed to clean up: " + "; ".join(errors)
            )

    def __enter__(self) -> ReviewerWorkspaceManager:
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
            print(f"[syncade] warning: reviewer workspace cleanup failed: {exc}", file=sys.stderr)
