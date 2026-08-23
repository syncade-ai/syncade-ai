"""Shared helpers for the CLI-surface test split (PR-R3)."""

import subprocess
from pathlib import Path


def _init_git_repo(repo: Path) -> None:
    """Initialize a git repo with the throwaway-config syncade tests use."""
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "README.md").write_text("syncade\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    # HEAD on a feature branch of a repo that HAS a remote (like a real run), so loop-mode
    # CLI tests satisfy the PR-v2-26 default-branch guard (which refuses remote-less repos).
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)
    _fake_origin_head(repo, "main")


def _outside_worktree_base(repo: Path) -> Path:
    """Return a test worktree base outside ``repo``."""
    return repo.parent / f"{repo.name}-syncade-worktrees"


def _fake_origin_head(repo: Path, branch: str) -> None:
    """Authoritative origin/HEAD -> <branch> without a real remote (see the orchestrator
    helper of the same name): satisfies the default-branch guard cheaply."""
    sha = subprocess.run(
        ["git", "rev-parse", branch], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/remotes/origin/{branch}", sha], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"],
        cwd=repo,
        check=True,
    )
