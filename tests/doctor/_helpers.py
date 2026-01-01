"""Shared helpers for the ``syncade --doctor`` test split (test_doctor + test_doctor_preview).

doctor's probes are monkeypatched to a known-green baseline via the ``healthy_env`` fixture
(``conftest.py``); these functions build the fakes and the git repos those tests point at,
so no test depends on installed CLIs, real ``/tmp`` free space, or a real provider call.
"""

from __future__ import annotations

import subprocess

from syncade.auth_check import AuthCheckResult
from tests.cli._helpers import _fake_origin_head, _init_git_repo


def _which_all_present(binary: str) -> str:
    return f"/usr/local/bin/{binary}"


def _which_none(binary: str) -> None:
    return None


def _which_missing(*missing: str):
    def which(binary: str) -> str | None:
        return None if binary in missing else f"/usr/local/bin/{binary}"

    return which


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _repo_on(path, branch: str):
    """A committed repo whose authoritative default (origin/HEAD) is ``main``, checked out
    on ``branch`` (which may BE ``main``)."""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=path, check=True)
    (path / "x").write_text("1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    _fake_origin_head(path, "main")
    if branch != "main":
        subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=path, check=True)
    return path


def _repo_no_default(path):
    """A committed repo on branch ``solo`` with NO origin/HEAD and NO main/master, so scope
    resolution (which needs a default branch for its fallback) raises BaseResolutionError."""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q", "-b", "solo"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=path, check=True)
    (path / "x").write_text("1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path


def _repo_unborn(path):
    """A git repo with an UNBORN HEAD — ``git init`` but no commits. The real review path's
    snapshot fails here (``could not resolve HEAD`` -> exit 60), so doctor must red it."""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", *cmd], cwd=path, check=True)
    return path  # no commit


def _ok_auth(provider: str = "openai") -> AuthCheckResult:
    return AuthCheckResult(
        provider=provider, model="m", ok=True, duration_seconds=0.1, detail="OK (0.1s)"
    )


def _fail_auth(provider: str = "openai") -> AuthCheckResult:
    return AuthCheckResult(
        provider=provider, model="m", ok=False, duration_seconds=0.1, detail="401 — token expired"
    )


def _init_git_repo_returning(path):
    _init_git_repo(path)  # on branch "work", origin/HEAD -> main
    return path
