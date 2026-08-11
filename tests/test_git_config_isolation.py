"""The suite's git behaviour does not depend on the developer's git config — PR-h-10 item 3.

Fixtures ``git init`` throwaway repositories and then refer to ``main``. That branch exists
only because a developer's ``init.defaultBranch`` says so; a runner has no such setting, so
git's built-in default applies and ``git rev-parse main`` exits 128. It cost 81 failures and
287 errors on every CI push from the day the repository went public — one cause, invisible
locally, because the rig was cleaner than reality.

``tests/conftest.py`` pins a hermetic config for the whole run. These tests assert the pin is
actually in force for git CHILD PROCESSES, which is the only place it matters: a fixture that
merely sets an environment variable proves nothing if git does not read it.

Deliberately NOT tested here: that the ambient value is overridden. A test cannot know what the
ambient value was — by the time it runs, the fixture has already replaced it. That claim is
proven by running the suite under a hostile ``GIT_CONFIG_GLOBAL``, which is recorded in the
PR-h-10 brief rather than asserted in-process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not found")


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    ).stdout.strip()


def test_a_fresh_repo_is_on_main(tmp_path):
    """The exact operation 14 fixtures perform, and the exact assumption they then make."""
    _git("init", "-q", ".", cwd=tmp_path)
    assert _git("branch", "--show-current", cwd=tmp_path) == "main"


def test_rev_parse_main_resolves_in_a_fresh_repo(tmp_path):
    """`git rev-parse main` exiting 128 here is the literal CI failure this item closes."""
    _git("init", "-q", ".", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "init", cwd=tmp_path)
    assert len(_git("rev-parse", "main", cwd=tmp_path)) == 40


def test_committing_needs_no_ambient_identity(tmp_path):
    """Replacing the global config strips the developer's user.email too.

    The hermetic config supplies one, so committing fixtures keep working on a runner that has
    no identity configured — the failure that would otherwise replace the one being fixed.
    """
    _git("init", "-q", ".", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "init", cwd=tmp_path)
    assert _git("log", "-1", "--pretty=%ae", cwd=tmp_path) == "tests@syncade.invalid"


def test_the_isolation_env_is_exported_to_children():
    """Both halves: the global replacement, and dropping /etc/gitconfig."""
    assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"
    global_cfg = os.environ.get("GIT_CONFIG_GLOBAL")
    assert global_cfg and Path(global_cfg).is_file()
    assert "defaultBranch = main" in Path(global_cfg).read_text(encoding="utf-8")
