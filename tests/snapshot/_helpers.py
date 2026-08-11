"""Shared helpers for the :mod:`syncade.snapshot` test package.

Copied verbatim from the original ``tests/test_snapshot.py`` module-level
helpers so the split test files (and the package ``conftest.py`` fixture)
share a single source of truth.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitCommandError(subprocess.CalledProcessError):
    """``CalledProcessError`` that actually shows what git said.

    ``check=True`` with piped output discards exactly the text explaining WHY: a CI-only
    failure surfaced as a bare ``returned non-zero exit status 1``, with no way to tell
    "nothing to commit" from a protocol refusal. That cost a diagnosis round (PR-h-10 item 3).

    Subclassing rather than raising something new keeps ``except subprocess.CalledProcessError``
    working — ``test_snapshot.py`` relies on it to skip when git lacks sha256 support.
    """

    def __str__(self) -> str:
        return f"{super().__str__()}\n--- stdout ---\n{self.stdout}--- stderr ---\n{self.stderr}"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``repo`` and return the completed process."""
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitCommandError(proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc


def _init_repo(repo_path: Path) -> None:
    """Initialize a git repo with the throwaway-config syncade tests use."""
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test")
    _git(repo_path, "config", "commit.gpgsign", "false")


def _commit(repo_path: Path, files: dict[str, str], message: str = "commit") -> str:
    """Write/overwrite ``files`` and commit them. Returns the new SHA."""
    for name, content in files.items():
        (repo_path / name).write_text(content)
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-m", message)
    return _git(repo_path, "rev-parse", "HEAD").stdout.strip()
