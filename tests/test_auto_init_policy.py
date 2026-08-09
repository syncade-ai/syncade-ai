"""Auto-init only touches a directory the operator did not fill — PR-h-04 item B.

Split out of ``test_git_preconditions.py``, which these tests pushed past the 500-LOC cap.
The dogfood producer responded by adding a SECOND exemption to ``scripts/check-loc.sh``;
that exemption is reverted and the file is split instead, because exempting a file from the
size gate so new tests fit into it is the exact outcome the gate exists to prevent.

The seam is real: ``test_git_preconditions.py`` covers what auto-init WRITES (ignore
patterns, the baseline commit, what stays untracked); this file covers WHETHER it may run at
all, and what ``--allow-auto-init`` does and does not buy.
"""

import subprocess
from pathlib import Path

import pytest

from syncade.git_preconditions import (
    AutoInitRefusedError,
    ensure_repo_initialized,
)  # noqa: F401


@pytest.fixture(autouse=True)
def _deterministic_git_identity(monkeypatch):
    """Pin a committer identity for the baseline commit so these tests are
    hermetic regardless of the runner's ambient git config.

    Production ``ensure_repo_initialized`` relies on git's auto-detected
    identity (``user@host``), which works on interactive macOS/Linux — the
    target platforms; tests must not depend on the developer's / CI's
    ``git config`` being set, so they pin one via the ``GIT_*`` env vars
    (inherited by every ``git commit`` subprocess).
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "syncade-test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@syncade.local")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "syncade-test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@syncade.local")


def _init_repo(path: Path) -> str:
    """Init a git repo at ``path`` with a deterministic identity + one
    commit. Returns the HEAD SHA.

    Mirrors ``tests/cli/_helpers.py::_init_git_repo`` — the explicit identity
    keeps the fixture commit reproducible regardless of the test runner's
    ambient git config.
    """
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _head_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_auto_init_refuses_a_populated_directory_by_default(tmp_path):
    """The three measured leaks all needed a POPULATED directory.

    A key in `deploy-notes.txt` (a name no denylist knows), the operator's own `.gitignore`
    re-including `.env` with `!`, and a symlinked `.git` that sent `git init` outside the
    directory entirely. Refusing here closes all three without a denylist to maintain or a
    content scanner to keep ahead of.
    """
    work = tmp_path / "populated"
    work.mkdir()
    (work / "deploy-notes.txt").write_text("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI\n")

    with pytest.raises(AutoInitRefusedError) as exc:
        ensure_repo_initialized(work)

    assert "--allow-auto-init" in str(exc.value), "the refusal must name the way forward"
    assert not (work / ".git").exists(), "refused, but a repo was created anyway"
    assert {p.name for p in work.iterdir()} == {"deploy-notes.txt"}


def test_auto_init_still_initializes_an_empty_directory(tmp_path):
    """Empty is always safe: there is nothing to capture. Onboarding must not need a flag."""
    work = tmp_path / "empty"
    work.mkdir()

    root = ensure_repo_initialized(work)

    assert (root / ".git").is_dir()


def test_the_opt_in_proceeds_and_keeps_every_exclusion_guarantee(tmp_path):
    """`--allow-auto-init` is an override, not a bypass of the secret exclusions.

    The exclusion machinery is the reason the opt-in is offered at all rather than the
    feature being deleted; if the flag skipped it, the flag would be worse than no flag.
    """
    work = tmp_path / "opted"
    work.mkdir()
    (work / ".env").write_text("SECRET=leak-me\n")
    (work / "app.py").write_text("x = 1\n")

    ensure_repo_initialized(work, allow_populated=True)

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=work, capture_output=True, text=True, check=True
    ).stdout.split()
    assert "app.py" in tracked
    assert ".env" not in tracked, "the opt-in must not disable the secret exclusions"


@pytest.mark.parametrize(
    "shape",
    [
        "symlinked-git",
        "nested-symlink",
        "control-symlink",
        "malformed-file",
        "malformed-dir",
    ],
)
def test_any_preexisting_git_entry_refuses_auto_init(tmp_path, shape):
    """One rule for every shape a broken `.git` can take — including shapes not listed here.

    This replaces TWELVE tests, one per symlink location (`.git` itself, `objects`, `config`,
    `hooks`, `info`, `refs`, `logs`, `COMMIT_EDITMSG`, `info/exclude`, and three nested
    paths). Each was added after a dogfood round found the previous scan too narrow, and the
    terminal round still found malformed REGULAR control paths that no symlink scan catches.

    `ensure_repo_initialized` is reached ONLY when `discover_repo_root` has already failed —
    so a `.git` present here is broken, partial, or hostile, and syncade never needs to work
    out which. Refusing the whole category is one condition instead of a scan that cannot be
    completed, and it covers shapes nobody has thought of yet.
    """
    work = tmp_path / "w"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    git = work / ".git"

    if shape == "symlinked-git":
        git.symlink_to(outside, target_is_directory=True)
    elif shape == "nested-symlink":
        (git / "refs").mkdir(parents=True)
        (git / "refs" / "heads").symlink_to(outside, target_is_directory=True)
    elif shape == "control-symlink":
        git.mkdir()
        (git / "objects").symlink_to(outside, target_is_directory=True)
    elif shape == "malformed-file":
        git.write_text("not a gitdir pointer\n")
    else:
        git.mkdir()
        (git / "HEAD").write_text("garbage\n")

    for allow in (False, True):
        with pytest.raises(AutoInitRefusedError):
            ensure_repo_initialized(work, allow_populated=allow)

    assert list(outside.iterdir()) == [], "auto-init wrote outside the directory it was given"
