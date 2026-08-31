"""Tests for :mod:`syncade.git_preconditions` — the init-if-missing
precondition on the main ``syncade <pr-doc>`` run path (PR-18).

These tests exercise the real ``git`` binary via the production
``run_subprocess`` path; only the *fixtures* shell out with
``subprocess.run`` directly (production code routes every git call
through ``run_subprocess``).
"""

import subprocess
from pathlib import Path

import pytest

from syncade.git_preconditions import (
    GitUnavailableError,
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


def test_existing_repo_returns_root_with_no_side_effects(tmp_path):
    repo = tmp_path / "repo"
    head_before = _init_repo(repo)
    assert not (repo / ".gitignore").exists()

    result = ensure_repo_initialized(repo)

    assert result == repo.resolve()
    # No new commit was created on the existing-repo path.
    assert _head_sha(repo) == head_before
    # No .gitignore written when an enclosing repo already exists.
    assert not (repo / ".gitignore").exists()


def test_repo_found_in_parent_returns_parent_root_no_nested_init(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    subdir = repo / "nested" / "deep"
    subdir.mkdir(parents=True)

    result = ensure_repo_initialized(subdir)

    # Parent-repo discovery preserved: returns the parent root, not the subdir.
    assert result == repo.resolve()
    # No spurious nested repo created in the subdirectory.
    assert not (subdir / ".git").exists()


def test_no_repo_with_git_present_initializes_and_returns_new_root(tmp_path):
    work = tmp_path / "fresh"
    work.mkdir()
    assert not (work / ".git").exists()

    result = ensure_repo_initialized(work, allow_populated=True)

    assert result == work.resolve()
    assert (work / ".git").is_dir()
    # PR-v2-26: auto-init leaves HEAD on a feature branch so a first run isn't refused by
    # the default-branch guard; `main` remains as the clean baseline.
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "syncade-work"
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "main"], cwd=work, capture_output=True
        ).returncode
        == 0
    ), "main should exist as the baseline"


def test_init_sets_main_branch_via_symbolic_ref_not_initial_branch_flag(tmp_path, monkeypatch):
    """N6: auto-init must not pass ``git init --initial-branch=main`` (git
    >= 2.28 only, fails on e.g. Ubuntu 20.04's git 2.25). The branch is set
    via ``git symbolic-ref`` (git 1.7+) instead, with identical HEAD ->
    refs/heads/main behavior on modern git."""
    from syncade import git_preconditions
    from syncade.process import run_subprocess as real_run_subprocess

    calls: list[list[str]] = []

    def spy(argv, **kwargs):
        calls.append(list(argv))
        return real_run_subprocess(argv, **kwargs)

    monkeypatch.setattr(git_preconditions, "run_subprocess", spy)

    work = tmp_path / "fresh"
    work.mkdir()
    ensure_repo_initialized(work)

    # `main` was created via symbolic-ref (the baseline commit landed on it); HEAD then
    # moved to the `syncade-work` feature branch (PR-v2-26).
    head_ref = subprocess.run(
        ["git", "symbolic-ref", "HEAD"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_ref == "refs/heads/syncade-work"
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "main"], cwd=work, capture_output=True
        ).returncode
        == 0
    ), "main should exist (set via symbolic-ref, baseline committed there)"

    # --initial-branch is no longer passed to git init (the git<2.28 break).
    init_calls = [c for c in calls if c[:2] == ["git", "init"]]
    assert init_calls, "expected a `git init` invocation"
    for call in init_calls:
        assert not any(tok.startswith("--initial-branch") for tok in call), (
            f"--initial-branch must not be passed: {call!r}"
        )
    # The branch is set via symbolic-ref instead.
    assert ["git", "symbolic-ref", "HEAD", "refs/heads/main"] in calls


def test_missing_git_binary_raises_git_unavailable(tmp_path, monkeypatch):
    work = tmp_path / "fresh"
    work.mkdir()
    # Point PATH at an empty directory so the `git` binary cannot be found
    # by the version probe (or by discover_repo_root's rev-parse).
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    with pytest.raises(GitUnavailableError):
        ensure_repo_initialized(work, allow_populated=True)


# --- T2: conservative .gitignore + safe baseline commit ---------------------


def _tracked_files(path: Path) -> list[str]:
    return subprocess.run(
        ["git", "ls-files"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


def test_gitignore_written_when_absent_matches_documented_patterns(tmp_path):
    work = tmp_path / "fresh"
    work.mkdir()

    ensure_repo_initialized(work)

    gi = work / ".gitignore"
    assert gi.exists()
    content = gi.read_text(encoding="utf-8")
    for pattern in (
        ".env",
        ".env.*",
        "credentials.json",
        ".aws/",
        "*.key",
        "*.pem",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "*.p12",
        "*.pfx",
        "node_modules/",
        "__pycache__/",
        "*.pyc",
        ".venv/",
        "venv/",
        "dist/",
        "build/",
        ".DS_Store",
    ):
        assert pattern in content, f"missing documented pattern: {pattern!r}"
    # The .gitignore is itself committed in the baseline.
    assert ".gitignore" in _tracked_files(work)


def test_existing_gitignore_preserved_verbatim(tmp_path):
    work = tmp_path / "fresh"
    work.mkdir()
    custom = "# my own rules\n*.log\nsecret-stuff/\n"
    (work / ".gitignore").write_text(custom, encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    # Never clobbered — byte-for-byte the operator's file.
    assert (work / ".gitignore").read_text(encoding="utf-8") == custom


def test_secrets_and_junk_excluded_from_baseline_commit(tmp_path):
    work = tmp_path / "fresh"
    work.mkdir()
    (work / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    (work / "credentials.json").write_text('{"token":"secret"}\n', encoding="utf-8")
    (work / ".aws").mkdir()
    (work / ".aws" / "credentials").write_text("[default]\nsecret=x\n", encoding="utf-8")
    (work / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    (work / "id_ed25519").write_text("PRIVATE KEY\n", encoding="utf-8")
    (work / "client.p12").write_text("KEYSTORE\n", encoding="utf-8")
    (work / "node_modules").mkdir()
    (work / "node_modules" / "pkg.js").write_text("module.exports = {}\n", encoding="utf-8")
    # A real source file that SHOULD be tracked.
    (work / "app.py").write_text("print('hi')\n", encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    tracked = _tracked_files(work)
    assert ".env" not in tracked
    assert "credentials.json" not in tracked
    assert ".aws/credentials" not in tracked
    assert "id_rsa" not in tracked
    assert "id_ed25519" not in tracked
    assert "client.p12" not in tracked
    assert not any(t.startswith("node_modules/") for t in tracked)
    # The real source file is committed; the safe-add ordering worked.
    assert "app.py" in tracked
    assert _head_sha(work)  # a baseline commit exists (rev-parse HEAD succeeds)


def test_syncade_secrets_file_excluded_from_baseline_commit(tmp_path):
    """H1: a pre-existing ``.syncade/secrets.toml`` must never be swept into
    the auto-init baseline commit. The _DEFAULT_GITIGNORE block (the
    secret-exclusion guarantee, written to .git/info/exclude) now covers
    syncade's own secrets file, matching the docstrings and what-this-is.md."""
    work = tmp_path / "fresh"
    work.mkdir()
    secrets = work / ".syncade" / "secrets.toml"
    secrets.parent.mkdir(parents=True)
    secrets.write_text('[producer]\napi_key = "sk-secret"\n', encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    # (a) git check-ignore matches (rc 0) and names the rule with -v.
    check = subprocess.run(
        ["git", "check-ignore", "-v", ".syncade/secrets.toml"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, (
        f"expected .syncade/secrets.toml to be ignored; "
        f"stdout={check.stdout!r} stderr={check.stderr!r}"
    )
    assert check.stdout.strip(), "check-ignore -v should print the matching rule"

    # (b) the secrets file is NOT tracked by the baseline commit.
    assert ".syncade/secrets.toml" not in _tracked_files(work)


def test_syncade_run_state_excluded_but_config_tracked(tmp_path):
    """H1 hygiene: auto-init must not baseline-commit pre-existing syncade run
    state (.syncade/runs/..., .syncade/workspace-claims/...,
    .syncade/last-reviewed.json), but
    .syncade/config.toml stays trackable — the floor ignores generated/run
    state, not all of .syncade/."""
    work = tmp_path / "fresh"
    work.mkdir()
    artifact = work / ".syncade" / "runs" / "2026-01-01T00-00-00" / "round-0" / "manifest.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")
    claim = work / ".syncade" / "workspace-claims" / "2026-01-01T00-00-00"
    claim.parent.mkdir(parents=True)
    claim.write_text("{}\n", encoding="utf-8")
    (work / ".syncade" / "last-reviewed.json").write_text("{}\n", encoding="utf-8")
    (work / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 2\n", encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    tracked = _tracked_files(work)
    assert not any(t.startswith(".syncade/runs/") for t in tracked)
    assert not any(t.startswith(".syncade/workspace-claims/") for t in tracked)
    assert ".syncade/last-reviewed.json" not in tracked
    # Not over-ignored: the user's config is still trackable.
    assert ".syncade/config.toml" in tracked


def test_existing_gitignore_without_secret_patterns_still_excludes_syncade_secrets(tmp_path):
    """H1 (occupied-.gitignore variant): a user's .gitignore that omits the
    safety patterns is preserved verbatim, but .git/info/exclude still keeps
    .syncade/secrets.toml out of the baseline. Pins the case the original H1
    fix only hand-verified."""
    work = tmp_path / "fresh"
    work.mkdir()
    user_ignore = "# mine\n*.log\n"
    (work / ".gitignore").write_text(user_ignore, encoding="utf-8")
    secrets = work / ".syncade" / "secrets.toml"
    secrets.parent.mkdir(parents=True)
    secrets.write_text('[producer]\napi_key = "sk-secret"\n', encoding="utf-8")
    (work / "app.py").write_text("print('hi')\n", encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    # Excluded via .git/info/exclude even though .gitignore omits the pattern.
    check = subprocess.run(
        ["git", "check-ignore", "-v", ".syncade/secrets.toml"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, f"stdout={check.stdout!r} stderr={check.stderr!r}"
    tracked = _tracked_files(work)
    assert ".syncade/secrets.toml" not in tracked
    assert "app.py" in tracked
    # The user's .gitignore is preserved byte-for-byte.
    assert (work / ".gitignore").read_text(encoding="utf-8") == user_ignore


def test_empty_directory_creates_baseline_commit(tmp_path):
    work = tmp_path / "empty"
    work.mkdir()

    ensure_repo_initialized(work)

    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "syncade: baseline commit" in log
    # An empty directory yields a commit containing only the auto-written ignore.
    assert _tracked_files(work) == [".gitignore"]


def test_allow_empty_when_existing_gitignore_ignores_everything(tmp_path):
    """A pre-existing ``.gitignore`` of ``*`` leaves nothing to stage;
    ``--allow-empty`` keeps the baseline commit from erroring with
    "nothing to commit". This is the case that actually exercises the flag."""
    work = tmp_path / "fresh"
    work.mkdir()
    (work / ".gitignore").write_text("*\n", encoding="utf-8")
    (work / "whatever.txt").write_text("ignored\n", encoding="utf-8")

    # Must not raise — the staged set is empty, so a plain commit would fail.
    ensure_repo_initialized(work, allow_populated=True)

    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "syncade: baseline commit" in log
    # `*` ignores .gitignore itself too → nothing tracked.
    assert _tracked_files(work) == []


# --- PR-18 dogfood regression: secret exclusion via .git/info/exclude --------
#
# The blind review loop (run 2026-05-29T11-16-03) found that secret exclusion
# implemented through the .gitignore write conflicts with "never clobber an
# existing .gitignore": an existing .gitignore that omits the safety patterns
# (or one that is a directory / symlink) let `git add -A` sweep .env into the
# baseline. The guarantee now lives in .git/info/exclude, independent of the
# .gitignore situation. These tests pin every variant the reviewers raised.


def _info_exclude_text(repo: Path) -> str:
    p = repo / ".git" / "info" / "exclude"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def test_info_exclude_contains_patterns_after_init(tmp_path):
    work = tmp_path / "fresh"
    work.mkdir()

    ensure_repo_initialized(work)

    content = _info_exclude_text(work)
    for pattern in (
        ".env",
        "credentials.json",
        ".aws/",
        "*.key",
        "*.pem",
        "id_rsa",
        "id_ed25519",
        "*.p12",
        "*.pfx",
        "node_modules/",
        "__pycache__/",
        ".DS_Store",
    ):
        assert pattern in content, f"missing pattern in .git/info/exclude: {pattern!r}"
    # info/exclude lives inside .git/ and is never staged.
    assert ".git/info/exclude" not in _tracked_files(work)


def test_existing_gitignore_without_secret_patterns_still_excludes_secrets(tmp_path):
    """The headline dogfood blocker: a user's .gitignore that omits the safety
    patterns is preserved verbatim, but .git/info/exclude still keeps .env out
    of the baseline."""
    work = tmp_path / "fresh"
    work.mkdir()
    user_ignore = "# mine\n*.log\n"
    (work / ".gitignore").write_text(user_ignore, encoding="utf-8")
    (work / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    (work / "credentials.json").write_text('{"token":"secret"}\n', encoding="utf-8")
    (work / ".aws").mkdir()
    (work / ".aws" / "credentials").write_text("[default]\nsecret=x\n", encoding="utf-8")
    (work / "app.py").write_text("print('hi')\n", encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    tracked = _tracked_files(work)
    assert ".env" not in tracked  # excluded by .git/info/exclude, not .gitignore
    assert "credentials.json" not in tracked
    assert ".aws/credentials" not in tracked
    assert "app.py" in tracked
    # The user's .gitignore is preserved byte-for-byte (never clobbered).
    assert (work / ".gitignore").read_text(encoding="utf-8") == user_ignore


def test_gitignore_directory_does_not_leak_secrets(tmp_path):
    """A .gitignore that is a DIRECTORY must neither silently sweep secrets nor
    error: info/exclude excludes .env and the directory is left untouched."""
    work = tmp_path / "fresh"
    work.mkdir()
    (work / ".gitignore").mkdir()  # a directory occupying the path
    (work / ".env").write_text("SECRET=x\n", encoding="utf-8")
    (work / "app.py").write_text("x\n", encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)  # must not raise

    tracked = _tracked_files(work)
    assert ".env" not in tracked
    assert "app.py" in tracked
    # Left in place — not clobbered into a file.
    assert (work / ".gitignore").is_dir()


def test_gitignore_symlink_to_regular_file_does_not_leak_secrets(tmp_path):
    work = tmp_path / "fresh"
    work.mkdir()
    target = work / "myignore.txt"
    target.write_text("*.log\n", encoding="utf-8")
    (work / ".gitignore").symlink_to(target.name)  # symlink -> existing file
    (work / ".env").write_text("SECRET=x\n", encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    # git refuses to honor a symlinked .gitignore; info/exclude covers .env.
    assert ".env" not in _tracked_files(work)
    # The symlink is left untouched (not clobbered into a regular file).
    assert (work / ".gitignore").is_symlink()


def test_gitignore_dangling_symlink_not_followed_and_no_leak(tmp_path):
    work = tmp_path / "fresh"
    work.mkdir()
    (work / ".gitignore").symlink_to("nonexistent-target")  # dangling
    (work / ".env").write_text("SECRET=x\n", encoding="utf-8")

    ensure_repo_initialized(work, allow_populated=True)

    assert ".env" not in _tracked_files(work)
    # The dangling symlink is left untouched; write_text must NOT have followed
    # it to materialize the target.
    assert (work / ".gitignore").is_symlink()
    assert not (work / "nonexistent-target").exists()


def test_undo_auto_init_preserves_operator_files_under_syncade(tmp_path):
    """Operator bytes placed under .syncade/ between auto-init and refusal must survive undo.

    Emptiness settles who *created* something, not who last wrote it. If an operator drops
    .syncade/config.toml while a refusal is in progress, deleting the whole .syncade/ tree
    would destroy their work. undo_auto_init does not touch .syncade/ AT ALL, so this holds
    for anything under it — no carve-out, and no rule about which entries are syncade's.
    """
    from syncade.git_preconditions import undo_auto_init

    work = tmp_path / "w"
    work.mkdir()

    # Simulate what auto-init + a partial run creates: .git plus .syncade/runs/<id>/
    (work / ".git").mkdir()
    (work / ".syncade" / "runs" / "2026-01-01T00-00-00").mkdir(parents=True)

    # Operator places a config file under .syncade/ AFTER auto-init
    operator_config = work / ".syncade" / "config.toml"
    operator_config.write_text("[producer]\n")

    failed = undo_auto_init(work)

    assert failed == [], f"undo_auto_init reported failures: {failed}"
    assert not (work / ".git").exists(), ".git was not removed"
    assert (work / ".syncade" / "runs" / "2026-01-01T00-00-00").is_dir(), (
        "undo deleted a run directory — GC is forbidden from doing that (metrics rebuild "
        "from the runs tree), and undo has no more licence than GC"
    )
    assert operator_config.exists(), "undo deleted an operator-created file under .syncade/"
    assert operator_config.read_text() == "[producer]\n", "operator file content was altered"
