"""CLI-surface tests: run-path behavior, dirty-tree + deprecation
warnings, timeout flag, repo-init, and subdir config discovery.

Split out of the monolithic ``tests/test_cli.py`` (PR-R3).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from syncade.cli import main
from tests.cli._helpers import _init_git_repo


def test_repo_root_tilde_is_expanded(tmp_path, monkeypatch):
    """Pass --repo-root='~/myrepo' with HOME pointed at tmp_path, place
    a bad config at the *expanded* path, and confirm we exit 50. If
    expansion weren't happening, discover_repo_root would look for a
    literal '~/myrepo' directory, not find it, and exit 60 — so a 50
    here proves the tilde was expanded AND config loaded from the
    discovered repo root."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    monkeypatch.setenv("HOME", str(tmp_path))
    myrepo = tmp_path / "myrepo"
    _init_git_repo(myrepo)
    syncade_dir = myrepo / ".syncade"
    syncade_dir.mkdir()
    (syncade_dir / "config.toml").write_text('[producer]\ntypo = "x"\n')

    rc = main(["--repo-root", "~/myrepo", "some-pr.md"])
    assert rc == 50  # proves tilde was expanded and bad config was loaded


def test_dirty_tree_warning_reaches_stderr_end_to_end(tmp_path, capsys):
    """PR-5.6 + PR-7.5 end-to-end: a dirty working tree at snapshot
    time fires a stderr warning before the dispatch even starts.
    Test routes through the real CLI -> orchestrator path; the run
    is configured to fail fast at dispatch (bogus provider -> exit
    50) so the assertion targets ONLY the warning, not real
    reviewer output.

    PR-7.5 splits the warning into strong (tracked-modified) and
    soft (untracked-only) variants. This test uses tracked-
    modification to exercise the strong-warning path, which is the
    actually-dangerous case the warning was originally designed
    for. The untracked-only soft-note path is tested in
    test_orchestrator.py.
    """
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    # Dirty the tree by modifying the tracked README.md (init helper
    # creates one).
    (tmp_path / "README.md").write_text("locally modified\n")
    # Bogus-provider config so dispatch fails fast (no real CLI involved).
    # PR-8: ``max_rounds = 1`` keeps the warning-only behavior (default
    # max_rounds=3 would refuse to start on tracked-modified → exit 60
    # before reaching dispatch).
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        "[loop]\nmax_rounds = 1\n"
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 50  # bogus provider triggered CONFIG_ERROR at dispatch
    captured = capsys.readouterr()
    # The strong dirty-tree warning fired BEFORE the dispatch failure,
    # on stderr. PR-7.5 wording: "uncommitted modifications to tracked
    # files".
    assert "uncommitted modifications to tracked" in captured.err.lower()


def test_dirty_tree_warning_silenced_with_quiet_flag(tmp_path, capsys):
    """`--quiet` suppresses the dirty-tree warning along with other
    informational output. PR-7.5 keeps both variants (strong + soft)
    in the same severity tier so quiet suppresses both."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("locally modified\n")
    (tmp_path / ".syncade").mkdir()
    # PR-8: ``max_rounds = 1`` to preserve PR-7.5 warning-only path.
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        "[loop]\nmax_rounds = 1\n"
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--quiet", "--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 50
    captured = capsys.readouterr()
    assert "uncommitted" not in captured.err.lower()
    assert "untracked" not in captured.err.lower()


def test_removed_require_unanimous_ship_true_is_config_error(tmp_path, capsys):
    """Stale ``require_unanimous_ship = true`` is rejected as config drift."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        "[loop]\nrequire_unanimous_ship = true\n"
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 50
    captured = capsys.readouterr()
    assert "Invalid configuration" in captured.err
    assert "loop.require_unanimous_ship" in captured.err
    assert "DEPRECATED" not in captured.err


def test_removed_require_unanimous_ship_false_is_config_error(tmp_path, capsys):
    """Stale ``require_unanimous_ship = false`` is rejected too."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        "[loop]\nrequire_unanimous_ship = false\n"
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 50
    captured = capsys.readouterr()
    assert "Invalid configuration" in captured.err
    assert "loop.require_unanimous_ship" in captured.err
    assert "DEPRECATED" not in captured.err


def test_removed_config_error_still_emits_in_quiet_mode(tmp_path, capsys):
    """``--quiet`` does not suppress stale-config errors."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        "[loop]\nrequire_unanimous_ship = false\n"
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--quiet", "--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 50
    captured = capsys.readouterr()
    assert "Invalid configuration" in captured.err
    assert "loop.require_unanimous_ship" in captured.err
    assert "snapshotting repo" not in captured.err


def test_no_removed_config_error_when_field_omitted(tmp_path, capsys):
    """A config without stale fields reaches the normal provider-validation path."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[[reviewers]]\nname = "rv1"\nprovider = "not-a-real-provider"\nmodel = "x"\n'
        # PR-8: ``le=3`` caps max_rounds at 3 — pre-PR-8 this used
        # ``max_rounds = 5``. Use 3 (the boundary) so the test
        # actually reaches the registry-lookup failure path (exit 50)
        # rather than short-circuiting at schema validation (also
        # exit 50, but a different code path that doesn't exercise
        # the deprecation-callback semantics this test pins).
        "[loop]\nmax_rounds = 3\n"  # mention loop but NOT the deprecated key
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 50
    captured = capsys.readouterr()
    assert "require_unanimous_ship" not in captured.err
    assert "DEPRECATED" not in captured.err


def test_base_flag_is_parsed_and_appears_in_help(capsys):
    """--base shows up in --help text and accepts a value; behavioral
    coverage lives in test_run_with_bogus_base_ref above."""
    with pytest.raises(SystemExit):
        main(["--help"])
    captured = capsys.readouterr()
    assert "--base" in captured.out
    assert "REF" in captured.out  # the metavar


def _patch_run_review(monkeypatch, captured: dict, tmp_path: Path) -> None:
    """Replace ``cli.run_review`` with a recorder that captures the
    kwargs it was called with and returns a minimal RunResult-shaped
    object so ``_run`` can finish without a real orchestration."""
    import types

    from syncade import cli

    def fake_run_review(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            exit_code=0,
            artifacts=types.SimpleNamespace(run_dir=tmp_path / ".syncade" / "runs" / "x"),
            dispatch_result=types.SimpleNamespace(results=[]),
        )

    monkeypatch.setattr(cli, "run_review", fake_run_review)


def test_timeout_flag_is_plumbed_through_to_run_review(tmp_path, monkeypatch):
    """--timeout <SECONDS> must reach run_review's timeout_seconds
    parameter as a float (precedence: CLI flag > config > default)."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")
    captured: dict = {}
    _patch_run_review(monkeypatch, captured, tmp_path)

    rc = main(["--repo-root", str(tmp_path), "--timeout", "300", str(pr_doc)])
    assert rc == 0
    assert captured["timeout_seconds"] == 300.0


def test_timeout_omitted_passes_none_to_run_review(tmp_path, monkeypatch):
    """Without --timeout, run_review receives None and falls back to
    config.loop.timeout_seconds itself — the CLI doesn't pre-resolve."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")
    captured: dict = {}
    _patch_run_review(monkeypatch, captured, tmp_path)

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 0
    assert captured["timeout_seconds"] is None


def test_relative_pr_doc_resolves_from_repo_root_when_repo_root_flag_points_at_repo(
    tmp_path, monkeypatch
):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    other_cwd = tmp_path / "elsewhere"
    _init_git_repo(repo)
    other_cwd.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    pr_doc = docs / "pr.md"
    pr_doc.write_text("# PR\n")
    captured: dict = {}
    _patch_run_review(monkeypatch, captured, repo)
    monkeypatch.chdir(other_cwd)

    rc = main(["--repo-root", str(repo), "docs/pr.md"])

    assert rc == 0
    assert captured["pr_doc_path"] == pr_doc


def test_run_in_non_git_dir_initializes_repo_and_proceeds(tmp_path, monkeypatch):
    """PR-18: the main run path in a non-repo directory initializes a git
    repo (+ baseline commit) instead of hard-stopping at exit 60, then
    proceeds into run_review."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    # Hermetic committer identity for the baseline commit (production relies
    # on git's auto-detected identity; tests pin one so they don't depend on
    # the runner's ambient git config).
    for key, val in (
        ("GIT_AUTHOR_NAME", "t"),
        ("GIT_AUTHOR_EMAIL", "t@e.com"),
        ("GIT_COMMITTER_NAME", "t"),
        ("GIT_COMMITTER_EMAIL", "t@e.com"),
    ):
        monkeypatch.setenv(key, val)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")
    assert not (tmp_path / ".git").exists()
    captured: dict = {}
    _patch_run_review(monkeypatch, captured, tmp_path)

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])

    assert rc == 0  # proceeded into (stubbed) run_review — no exit 60
    assert (tmp_path / ".git").is_dir()  # repo was initialized
    assert captured  # run_review was actually called


def test_run_existing_repo_makes_no_spurious_baseline_commit(tmp_path, monkeypatch):
    """PR-18: in an existing repo the precondition is a no-op — HEAD does
    not move and no 'syncade: baseline commit' is created."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)  # existing repo with one 'initial' commit
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")
    captured: dict = {}
    _patch_run_review(monkeypatch, captured, tmp_path)

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])

    assert rc == 0
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == head_before  # no new commit
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "syncade: baseline commit" not in log


def test_timeout_flag_appears_in_help(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    captured = capsys.readouterr()
    assert "--timeout" in captured.out
    assert "SECONDS" in captured.out  # the metavar


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5"])
def test_timeout_rejects_non_positive_values(bad, capsys):
    """--timeout must be > 0, matching LoopConfig.timeout_seconds' gt=0.
    A zero or negative value is an argparse error (exit 2) — it never
    reaches the orchestrator."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--timeout", bad, "some-pr.md"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--timeout" in err
    assert "positive" in err


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_timeout_rejects_non_finite_values(bad, capsys):
    """float() happily parses nan / inf, but neither is a usable
    timeout — _positive_float's math.isfinite guard rejects them as an
    argparse error (exit 2), so they never reach the orchestrator.

    (``-inf`` is intercepted earlier still: argparse's negative-number
    regex matches ``-1`` / ``-0.5`` but not ``-inf``, so a bare ``-inf``
    token is treated as an unknown option — also exit 2, just not via
    this guard.)
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["--timeout", bad, "some-pr.md"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--timeout" in err
    assert "finite" in err


# ---------------------------------------------------------------------------
# Config discovery from a subdirectory invocation (PR-5.5 review fix)
# ---------------------------------------------------------------------------


def test_subdir_invocation_loads_repo_root_config(tmp_path, monkeypatch):
    """CLI invoked from a subdirectory must discover the git repo root
    and load .syncade/config.toml FROM THAT ROOT — not from the subdir
    (which usually has no config). Regression for the PR-5.5 QA finding."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    syncade_dir = tmp_path / ".syncade"
    syncade_dir.mkdir()
    # A non-default value at the repo-root config proves which file was read.
    (syncade_dir / "config.toml").write_text("[loop]\ntimeout_seconds = 3600\n")
    subdir = tmp_path / "docs" / "sub"
    subdir.mkdir(parents=True)
    pr_doc = subdir / "pr.md"
    pr_doc.write_text("# PR\n")

    captured: dict = {}
    _patch_run_review(monkeypatch, captured, tmp_path)

    rc = main(["--repo-root", str(subdir), str(pr_doc)])
    assert rc == 0
    # The config that reached run_review came from the REPO ROOT.
    assert captured["config"].loop.timeout_seconds == 3600
    # run_review was handed the discovered root, not the subdir hint.
    assert captured["repo_root"] == tmp_path.resolve()
    # The CLI created no .syncade under the subdir.
    assert not (subdir / ".syncade").exists()


def test_subdir_invocation_with_invalid_root_config_exits_50(tmp_path, capsys):
    """An invalid value in the repo-root config surfaces as exit 50 even
    when syncade is invoked from a subdirectory — config is loaded from
    the discovered root, so its schema validation still fires."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    syncade_dir = tmp_path / ".syncade"
    syncade_dir.mkdir()
    # timeout_seconds = 0 violates LoopConfig's gt=0.
    (syncade_dir / "config.toml").write_text("[loop]\ntimeout_seconds = 0\n")
    subdir = tmp_path / "nested"
    subdir.mkdir()
    pr_doc = subdir / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--repo-root", str(subdir), str(pr_doc)])
    assert rc == 50
    captured = capsys.readouterr()
    assert "timeout_seconds" in captured.err


def test_unreadable_config_exits_50_without_traceback(tmp_path, monkeypatch, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    syncade_dir = tmp_path / ".syncade"
    syncade_dir.mkdir()
    config_path = syncade_dir / "config.toml"
    config_path.write_text("[loop]\nmax_rounds = 1\n", encoding="utf-8")
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n", encoding="utf-8")
    original_open = type(config_path).open

    def deny_config_open(self, *args, **kwargs):
        if self == config_path:
            raise PermissionError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(type(config_path), "open", deny_config_open)

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])

    captured = capsys.readouterr()
    assert rc == 50
    assert "config error" in captured.err
    assert "Failed to read" in captured.err
    assert "Traceback" not in captured.err


def test_unreadable_config_parent_probe_exits_50_without_traceback(tmp_path, monkeypatch, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    syncade_dir = tmp_path / ".syncade"
    syncade_dir.mkdir()
    config_path = syncade_dir / "config.toml"
    config_path.write_text("[loop]\nmax_rounds = 1\n", encoding="utf-8")
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n", encoding="utf-8")
    original_is_file = type(config_path).is_file

    def deny_config_probe(self):
        if self == config_path:
            raise PermissionError("permission denied")
        return original_is_file(self)

    monkeypatch.setattr(type(config_path), "is_file", deny_config_probe)

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])

    captured = capsys.readouterr()
    assert rc == 50
    assert "config error" in captured.err
    assert "Failed to inspect" in captured.err
    assert "Traceback" not in captured.err
