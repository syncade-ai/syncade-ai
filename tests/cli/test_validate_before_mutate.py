"""A refused command changes nothing on disk — PR-h-04 item A (audit rank 7).

`syncade ./typo.md` used to reach its "that file does not exist" check only AFTER
`ensure_repo_initialized` had run, so a single mistyped path left behind a `.git`, a
baseline commit, a tracked `.gitignore`, and 33 exclude rules. In an EXISTING repo it was
worse in a different way: `guard_default_branch` refused first, so the operator was told to
switch branches when the real mistake was a filename.

Both are the same defect — syncade acted before it knew what it had been asked to do.

The assertion here is deliberately stronger than "no `.git` was created": it snapshots the
whole directory tree (paths + bytes) and requires EQUALITY. A weaker check passes while some
future mutation slips in beside the one we fixed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from syncade.cli import main

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under `root`, by relative path -> content. Directories included as markers
    so an empty dir being created is caught too."""
    out: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = p.read_bytes() if p.is_file() else b"<dir>"
    return out


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        # Git's OWN housekeeping, disabled at the source. `_tree()` compares every path under
        # `.git` byte-for-byte, and git may fire auto-maintenance after an ordinary command and
        # leave `.git/objects/maintenance.lock` behind — which public CI hit on py3.14 while
        # py3.11 passed on the same commit, because the window is a timing race.
        #
        # Removing the CAUSE rather than filtering the symptom keeps the assertion at full
        # strength. Excluding `*.lock` from the comparison would have been the easy fix and a bad
        # one: a real syncade defect that abandoned `.git/refs/heads/main.lock` is exactly what
        # this test exists to catch, and that is a `.lock` too.
        ["git", "config", "gc.auto", "0"],
        ["git", "config", "maintenance.auto", "false"],
    ):
        subprocess.run(argv, cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "seed", "--allow-empty"], cwd=path, capture_output=True, check=True
    )


def test_a_typo_in_a_fresh_directory_leaves_it_untouched(tmp_path, capsys):
    """The reproduction: this used to create a repo and a baseline commit."""
    work = tmp_path / "fresh"
    work.mkdir()
    before = _tree(work)

    rc = main(["--repo-root", str(work), str(work / "typo.md")])

    assert rc == 2, "a missing brief is a CLI-input problem"
    assert "does not exist" in capsys.readouterr().err
    assert _tree(work) == before, "a refused command mutated the operator's directory"
    assert not (work / ".git").exists()


def test_a_typo_in_an_existing_repo_reports_the_filename_not_the_branch(tmp_path, capsys):
    """The second, sharper instance: the default-branch guard used to answer first.

    On `main` with a real commit, `syncade ./typo.md` exited 60 telling the operator to move
    to a feature branch. The brief was never the problem the message described.
    """
    repo = tmp_path / "repo"
    _git_repo(repo)
    before = _tree(repo)

    rc = main(["--repo-root", str(repo), str(repo / "typo.md")])

    err = capsys.readouterr().err
    assert rc == 2, f"expected the input-error code, not a branch refusal: {err}"
    assert "does not exist" in err
    assert "default/integration branch" not in err, (
        "a mistyped filename is still being reported as a branch problem"
    )
    assert _tree(repo) == before


def test_a_directory_passed_as_the_brief_is_refused_inertly(tmp_path, capsys):
    """`pr_doc_path is not a file` is the same class and must also refuse before mutating."""
    work = tmp_path / "fresh2"
    (work / "adir.md").mkdir(parents=True)
    before = _tree(work)

    rc = main(["--repo-root", str(work), str(work / "adir.md")])

    assert rc == 2
    assert "not a file" in capsys.readouterr().err
    assert _tree(work) == before


def test_bad_openspec_change_id_in_fresh_dir_leaves_it_untouched(tmp_path, capsys):
    """Regression: a bad --openspec change-id used to initialize .git before being refused.

    _resolve_openspec_pr_doc ran after ensure_repo_initialized, so a typo in the change-id
    caused auto-init to create .git, a baseline commit, and .gitignore before reporting
    that the OpenSpec change folder did not exist.
    """
    work = tmp_path / "fresh"
    work.mkdir()
    before = _tree(work)

    rc = main(["--repo-root", str(work), "--openspec", "nonexistent-change-id"])

    assert rc == 60
    assert _tree(work) == before, ".git or other files were created before the refusal"


def test_the_cli_uses_the_same_predicate_as_run_review():
    """Not a duplicated condition — the SAME function.

    PR-h-02d.5 burned four rounds on a CLI pre-flight that drifted from what `run_review`
    accepts. The pre-flight here calls `validate_run_inputs`, and so does `run_review`; this
    pins that, so re-introducing a private copy in the CLI fails.
    """
    import inspect

    from syncade.orchestrator import loop

    assert "validate_run_inputs(repo_root, pr_doc_path)" in inspect.getsource(loop.run_review)

    from syncade import cli

    assert "validate_run_inputs(" in inspect.getsource(cli._run)


def test_subdirectory_repo_root_hint_does_not_reject_pr_doc_at_repo_root(
    tmp_path, capsys, monkeypatch
):
    """A --repo-root pointing at a subdirectory must not reject a PR doc that exists at the
    real repo root.

    The pre-flight used to resolve PR_DOC against the raw --repo-root hint instead of the
    discovered git root. `path/to/pr.md` exists under the repo root but NOT under the
    subdirectory, so the CLI rejected it with "does not exist" before the review could start.
    """
    repo = tmp_path / "repo"
    _git_repo(repo)
    # ONE source of truth for the path, used for both the file and the argv. A literal
    # "path/to/pr.md" here would be rewritten by scripts/oss-scrub.py (which redacts that
    # internal directory from the public snapshot) while the `repo / "docs" / "prs"` form
    # above it would NOT — so the scrubbed test asked for a file it had never created, and
    # the release gate caught it. Deriving one from the other makes them undivergeable.
    rel_pr_doc = Path("docs") / "prs" / "pr.md"
    pr_doc = repo / rel_pr_doc
    pr_doc.parent.mkdir(parents=True)
    pr_doc.write_text("# PR\n")

    # Commit the doc so discover_repo_root can find it.
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "add pr doc"], cwd=repo, capture_output=True, check=True
    )

    subdir = repo / "subdir"
    subdir.mkdir()

    # Monkeypatch run_review so the test stops after pre-flight.
    import syncade.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run_review", lambda *a, **kw: 0)

    rc = main(["--repo-root", str(subdir), str(rel_pr_doc)])

    err = capsys.readouterr().err
    assert rc != 2 or "does not exist" not in err, (
        "subdirectory --repo-root rejected a PR doc that exists at the real repo root"
    )


def test_bad_config_in_fresh_dir_leaves_it_untouched(tmp_path, capsys):
    """Config load and CLI override validation happen BEFORE auto-init.

    A bad .syncade/config.toml used to let ensure_repo_initialized create .git and
    a baseline commit before returning exit 50. Now config is loaded from the preflight
    root (before mutation) so a config error refuses without touching the directory.
    """
    work = tmp_path / "fresh"
    work.mkdir()
    pr_doc = work / "pr.md"
    pr_doc.write_text("# PR\n")
    syncade_dir = work / ".syncade"
    syncade_dir.mkdir()
    (syncade_dir / "config.toml").write_text('[loop]\nmax_rounds = "not_a_number"\n')
    before = _tree(work)

    rc = main(["--repo-root", str(work), "--allow-auto-init", str(pr_doc)])

    assert rc == 50, f"expected CONFIG_ERROR (50), got {rc}"
    assert "config error" in capsys.readouterr().err
    assert _tree(work) == before, "config error mutated the operator's directory before refusing"
    assert not (work / ".git").exists()


def test_bad_cli_override_in_fresh_dir_leaves_it_untouched(tmp_path, capsys):
    """A bad --reviewer-model override refuses before auto-init creates .git.

    apply_cli_overrides is pure and now runs before ensure_repo_initialized, so
    an unknown reviewer name is caught without touching the filesystem.
    """
    work = tmp_path / "fresh2"
    work.mkdir()
    pr_doc = work / "pr.md"
    pr_doc.write_text("# PR\n")
    before = _tree(work)

    rc = main(
        [
            "--repo-root",
            str(work),
            "--allow-auto-init",
            str(pr_doc),
            "--reviewer-model",
            "TOTALLY-UNKNOWN-REVIEWER=gpt-5.5",
        ]
    )

    assert rc == 50, f"expected CONFIG_ERROR (50), got {rc}"
    assert _tree(work) == before, "override error mutated the operator's directory before refusing"
    assert not (work / ".git").exists()


def test_auth_gate_failure_in_fresh_dir_leaves_it_untouched(tmp_path, capsys, monkeypatch):
    """auth_gate now runs BEFORE ensure_repo_initialized.

    An auth mismatch previously created .git and a baseline commit before refusing with
    exit 50. Now auth_gate is the first check inside the outer try, so a simulated auth
    failure leaves the fresh directory completely untouched.
    """
    import syncade.cli as cli_mod

    work = tmp_path / "fresh"
    work.mkdir()
    pr_doc = work / "pr.md"
    pr_doc.write_text("# PR\n")
    before = _tree(work)

    # Simulate auth_gate returning a failure code.
    monkeypatch.setattr(cli_mod, "auth_gate", lambda config, blocks: 50)

    rc = main(["--repo-root", str(work), "--allow-auto-init", str(pr_doc)])

    assert rc == 50
    assert _tree(work) == before, "auth failure mutated the operator's directory before refusing"
    assert not (work / ".git").exists()


def test_bad_config_with_openspec_does_not_leak_tempfile(tmp_path, capsys, monkeypatch):
    """Config load failures must not leave an OpenSpec tempfile on disk.

    The OpenSpec preflight used to create a delete=False tempfile BEFORE config load.
    A bad config returned CONFIG_ERROR after the tempfile was written but before the
    cleanup finally started, leaking private spec content. Config is now loaded first.
    """
    import syncade.cli as cli_mod

    # Track whether _resolve_openspec_pr_doc was called (it creates the tempfile).
    called = []

    def _mock_resolve(root, change_id, logger):
        called.append(True)
        return None  # would return a Path in real use

    monkeypatch.setattr(cli_mod, "_resolve_openspec_pr_doc", _mock_resolve)

    work = tmp_path / "fresh"
    work.mkdir()
    syncade_dir = work / ".syncade"
    syncade_dir.mkdir()
    (syncade_dir / "config.toml").write_text('[loop]\nmax_rounds = "not_a_number"\n')

    rc = main(["--repo-root", str(work), "--openspec", "some-change-id"])

    assert rc == 50, f"expected CONFIG_ERROR (50), got {rc}"
    assert not called, "_resolve_openspec_pr_doc was called before config load — tempfile risk"


def test_worktree_base_parent_is_file_refused_before_auto_init(tmp_path, capsys):
    """A --worktree-base whose parent is a file is caught BEFORE auto-init."""
    work = tmp_path / "fresh"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    file_parent = tmp_path / "not_a_dir"
    file_parent.write_text("I am a file, not a directory\n")
    before = _tree(work)

    rc = main(
        [
            "--repo-root",
            str(work),
            str(work / "brief.md"),
            "--worktree-base",
            str(file_parent / "wt"),
            "--allow-auto-init",
        ]
    )

    assert rc == 50, f"expected CONFIG_ERROR (50), got {rc}"
    err = capsys.readouterr().err
    assert "not a directory" in err
    assert _tree(work) == before, ".git was created before the worktree-base refusal"
    assert not (work / ".git").exists()


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permission checks")
def test_unwritable_worktree_base_refused_before_auto_init(tmp_path, capsys):
    """An unwritable --worktree-base is caught BEFORE auto-init.

    Previously the loop's mkdir ran after ensure_repo_initialized, so an unwritable
    worktree-base returned exit 60 after .git, .gitignore, and a baseline commit
    had already been created.
    """
    work = tmp_path / "fresh"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    wt_base = tmp_path / "wt"
    wt_base.mkdir()
    wt_base.chmod(0o555)
    before = _tree(work)
    try:
        rc = main(
            [
                "--repo-root",
                str(work),
                str(work / "brief.md"),
                "--worktree-base",
                str(wt_base),
                "--allow-auto-init",
            ]
        )
    finally:
        wt_base.chmod(0o755)
    assert rc == 50, f"expected CONFIG_ERROR (50), got {rc}"
    assert "not writable" in capsys.readouterr().err
    assert _tree(work) == before, ".git was created before the worktree-base refusal"
    assert not (work / ".git").exists()


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permission checks")
def test_unwritable_worktree_base_parent_refused_before_auto_init(tmp_path, capsys):
    """An unwritable parent for a missing --worktree-base is caught BEFORE auto-init."""
    work = tmp_path / "fresh2"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    parent = tmp_path / "wt_parent"
    parent.mkdir()
    parent.chmod(0o555)
    before = _tree(work)
    try:
        rc = main(
            [
                "--repo-root",
                str(work),
                str(work / "brief.md"),
                "--worktree-base",
                str(parent / "wt"),
                "--allow-auto-init",
            ]
        )
    finally:
        parent.chmod(0o755)
    assert rc == 50, f"expected CONFIG_ERROR (50), got {rc}"
    assert "not writable" in capsys.readouterr().err
    assert _tree(work) == before, ".git was created before the worktree-base refusal"
    assert not (work / ".git").exists()


def test_syncade_dir_as_file_refused_before_auto_init(tmp_path, capsys):
    """.syncade existing as a file is caught BEFORE auto-init.

    Previously this returned exit 2 (NotADirectoryError from the loop's mkdir)
    AFTER auto-init had already created .git and a baseline commit.
    """
    work = tmp_path / "fresh3"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    (work / ".syncade").write_text("not a directory\n")
    before = _tree(work)

    rc = main(
        [
            "--repo-root",
            str(work),
            str(work / "brief.md"),
            "--allow-auto-init",
        ]
    )

    assert rc == 60, f"expected WORKTREE_ERROR (60), got {rc}"
    assert "not a directory" in capsys.readouterr().err
    assert _tree(work) == before, ".git was created before the .syncade refusal"
    assert not (work / ".git").exists()


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses permission checks")
def test_unwritable_syncade_dir_refused_before_auto_init(tmp_path, capsys):
    """An unwritable .syncade dir is caught BEFORE auto-init.

    Previously this raised an uncaught PermissionError (exit 1) AFTER auto-init
    had already created .git and a baseline commit.
    """
    work = tmp_path / "fresh4"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    syncade_dir = work / ".syncade"
    syncade_dir.mkdir()
    syncade_dir.chmod(0o555)
    before = _tree(work)
    try:
        rc = main(
            [
                "--repo-root",
                str(work),
                str(work / "brief.md"),
                "--allow-auto-init",
            ]
        )
    finally:
        syncade_dir.chmod(0o755)
    assert rc == 60, f"expected WORKTREE_ERROR (60), got {rc}"
    assert "not writable" in capsys.readouterr().err
    assert _tree(work) == before, ".git was created before the .syncade refusal"
    assert not (work / ".git").exists()


@pytest.mark.parametrize(
    "empty_argv",
    [
        ["--resume", "", "brief.md"],
        ["brief.md", "--spec-audit", ""],
    ],
    ids=["--resume-empty", "--spec-audit-empty"],
)
def test_empty_string_one_shot_operand_refused_before_auto_init(tmp_path, capsys, empty_argv):
    """An explicit empty string for --resume or --spec-audit is refused BEFORE auto-init.

    Previously the truthiness dispatch treated '' as absent, so --resume '' dispatched
    a normal review (creating .git in a fresh dir) and --install-skill with --resume ''
    ran the installer without error.
    """
    work = tmp_path / "fresh"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    before = _tree(work)

    argv = ["--repo-root", str(work), "--allow-auto-init"] + [
        str(work / p) if p == "brief.md" else p for p in empty_argv
    ]
    rc = main(argv)

    assert rc == 2, f"expected CLI_USAGE_ERROR (2), got {rc}"
    assert _tree(work) == before, ".git was created before the empty-operand refusal"
    assert not (work / ".git").exists()


def test_dangling_worktree_base_symlink_refused_before_auto_init(tmp_path, capsys):
    """A dangling --worktree-base symlink is refused BEFORE auto-init.

    Path.exists() follows symlinks and returns False for dangling links, which the
    earlier preflight treated as 'absent but creatable', allowing auto-init to proceed.
    """
    work = tmp_path / "fresh"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    dangling = tmp_path / "dangling_wt"
    dangling.symlink_to(tmp_path / "nonexistent_target")
    before = _tree(work)

    rc = main(
        [
            "--repo-root",
            str(work),
            str(work / "brief.md"),
            "--worktree-base",
            str(dangling),
            "--allow-auto-init",
        ]
    )

    assert rc == 50, f"expected CONFIG_ERROR (50), got {rc}"
    assert "dangling symlink" in capsys.readouterr().err
    assert _tree(work) == before, ".git was created before the dangling-symlink refusal"
    assert not (work / ".git").exists()


def test_dangling_syncade_dir_symlink_refused_before_auto_init(tmp_path, capsys):
    """A dangling .syncade symlink is refused BEFORE auto-init."""
    work = tmp_path / "fresh"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    dangling = work / ".syncade"
    dangling.symlink_to(tmp_path / "nonexistent_target")
    before = _tree(work)

    rc = main(
        [
            "--repo-root",
            str(work),
            str(work / "brief.md"),
            "--allow-auto-init",
        ]
    )

    assert rc == 60, f"expected WORKTREE_ERROR (60), got {rc}"
    assert "dangling symlink" in capsys.readouterr().err
    assert _tree(work) == before, ".git was created before the dangling-.syncade refusal"
    assert not (work / ".git").exists()


def test_dangling_runs_dir_symlink_refused_before_auto_init(tmp_path, capsys):
    """A dangling .syncade/runs symlink is refused BEFORE auto-init."""
    work = tmp_path / "fresh"
    work.mkdir()
    (work / "brief.md").write_text("# PR\n")
    (work / ".syncade").mkdir()
    dangling = work / ".syncade" / "runs"
    dangling.symlink_to(tmp_path / "nonexistent_target")
    before = _tree(work)

    rc = main(
        [
            "--repo-root",
            str(work),
            str(work / "brief.md"),
            "--allow-auto-init",
        ]
    )

    assert rc == 60, f"expected WORKTREE_ERROR (60), got {rc}"
    assert "dangling symlink" in capsys.readouterr().err
    assert _tree(work) == before, ".git was created before the dangling-.syncade/runs refusal"
    assert not (work / ".git").exists()
