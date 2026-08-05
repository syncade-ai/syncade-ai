"""CLI-surface tests for :mod:`syncade.cli`.

Invokes ``main()`` directly with an explicit ``argv`` list rather than
going through ``subprocess`` — faster and gives readable assertion
failures, while still exercising argparse + config loading + dispatch
end-to-end inside the process.

PR-5 wired ``syncade <PR_DOC>`` into :func:`syncade.orchestrator.run_review`.
These tests cover argument parsing and CLI-side error mapping; the
orchestrator's own round-trip behavior is covered in
``tests/test_orchestrator.py`` (with FakeAdapter injection that the
CLI deliberately doesn't expose).
"""

import shutil
from pathlib import Path

import pytest

import syncade.cli as cli_module
from syncade import __version__
from syncade.cli import main
from tests.cli._helpers import _init_git_repo


def test_version_flag_prints_version_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert f"syncade {__version__}" in captured.out


def test_reported_version_matches_the_packaged_one():
    """`--version` must not report a version the package was not built with.

    The number lives in TWO places — `pyproject.toml` (what gets packaged) and
    `syncade.__version__` (what `--version` prints) — and nothing tied them together. Bumping
    one and forgetting the other makes the CLI state a fact that is false, which is the same
    class of defect as the documentation claims corrected in 0.3.0.
    """
    import tomllib

    root = Path(__file__).resolve().parents[2]
    packaged = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert packaged == __version__, (
        f"pyproject.toml says {packaged!r} but syncade.__version__ is {__version__!r} — "
        "`--version` would report a version that was never packaged"
    )


def test_help_flag_exits_zero_and_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "--resume" in out
    assert "--spec-audit" in out
    assert "--base" in out  # new in PR-5
    assert "PR_DOC" in out


def test_cli_public_surface_excludes_dead_stub():
    assert "_stub" not in cli_module.__all__
    assert not hasattr(cli_module, "_stub")


def test_run_missing_pr_doc_returns_2_with_clear_error(tmp_path, capsys):
    """PR-5: `run` is no longer a stub. Pointing at a non-existent
    PR_DOC must surface a real FileNotFoundError-shaped message and
    exit 2 (argparse convention for input-validation errors)."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    rc = main(["--repo-root", str(tmp_path), "does-not-exist.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "does-not-exist.md" in captured.err
    assert "does not exist" in captured.err


def test_run_with_bogus_base_ref_returns_worktree_error(tmp_path, capsys):
    """--base <bogus-ref> surfaces SnapshotError. The brief lets us
    pick a code; we map to WORKTREE_ERROR (60) to keep "couldn't even
    start the run" failures in the same bucket as worktree
    provisioning failures (PRD exit-code 60: 'Worktree provisioning
    or test env error')."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    rc = main(["--repo-root", str(tmp_path), "--base", "not-a-real-ref", str(pr_doc)])
    assert rc == 60
    captured = capsys.readouterr()
    assert "not-a-real-ref" in captured.err


def test_run_missing_git_binary_returns_worktree_error(tmp_path, monkeypatch, capsys):
    """PR-18: a non-git repo_root no longer hard-stops (the precondition
    inits a repo). The one case that still exits 60 on the main run path
    is a missing git *binary* — and it surfaces the actionable install
    message, not a cryptic traceback."""
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")
    # Point PATH at an empty directory so `git` cannot be found at all.
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])

    assert rc == 60
    err = capsys.readouterr().err.lower()
    assert "git is required" in err  # install-guidance message, not a traceback


def test_resume_unknown_run_id_exits_worktree_error(tmp_path, capsys):
    """PR-16: --resume <run-id> for a non-existent run-id exits 60
    (worktree/environment error class)."""
    import subprocess

    # Initialize a real git repo so --resume can resolve a repo_root.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    # Feature branch so the default-branch guard doesn't preempt the resume run-id check.
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)
    rc = main(["--repo-root", str(repo), "--resume", "2026-05-11-abc"])
    assert rc == 60
    captured = capsys.readouterr()
    assert "resume error" in captured.err.lower()
    assert "not found" in captured.err.lower()


def test_spec_audit_nonexistent_file_returns_worktree_error(tmp_path, capsys):
    """PR-10 Task 3: ``--spec-audit`` with a non-existent file exits 60
    (WORKTREE_ERROR) and prints an informative error message before
    attempting any git or config work."""
    rc = main(["--repo-root", str(tmp_path), "--spec-audit", "nonexistent-pr.md"])
    assert rc == 60
    captured = capsys.readouterr()
    assert "not a readable file" in captured.err or "--spec-audit" in captured.err


def test_no_command_prints_help_to_stderr_and_returns_nonzero(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path)])
    assert rc != 0
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()


def test_bad_config_returns_50_and_names_offending_field(tmp_path, capsys):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text('[producer]\ntypo_field = "x"\n')
    rc = main(["--repo-root", str(tmp_path), "some-pr.md"])
    assert rc == 50
    captured = capsys.readouterr()
    assert "typo_field" in captured.err


def test_no_command_with_bad_config_returns_help_not_50(tmp_path, capsys):
    """When no command is given, we short-circuit to help BEFORE loading
    config — a stale .syncade/config.toml shouldn't preempt a user trying
    to discover what the tool does."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text('[producer]\ntypo_field = "x"\n')
    rc = main(["--repo-root", str(tmp_path)])
    assert rc == 2  # not 50
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower()
    assert "typo_field" not in captured.err  # config wasn't even consulted


def test_spec_audit_with_positional_pr_doc_errors_with_mutex_message(tmp_path, capsys):
    """PR-10 Task 3: ``--spec-audit`` and the positional PR_DOC are
    mutually exclusive — the spec-audit takes its own PR_DOC path and
    the positional is the loop-mode PR_DOC."""
    rc = main(["--repo-root", str(tmp_path), "--spec-audit", "audit.md", "loop.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--spec-audit cannot be combined with a PR_DOC" in captured.err


def test_resume_with_pr_doc_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--resume", "run-id", "some-pr.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--resume cannot be combined with a PR_DOC" in captured.err


def test_selfcheck_with_pr_doc_errors_with_mutex_message(tmp_path, capsys):
    """PR-8.5 Task 3: ``--selfcheck`` and ``PR_DOC`` are mutually
    exclusive. Caught at the post-parse validation pass before any
    filesystem work."""
    rc = main(["--repo-root", str(tmp_path), "--selfcheck", "some-pr.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--selfcheck cannot be combined with a PR_DOC" in captured.err


def test_selfcheck_with_resume_errors_with_mutex_message(tmp_path, capsys):
    """PR-8.5 Task 3: ``--selfcheck`` and ``--resume`` are mutually
    exclusive (selfcheck is a one-shot smoke; resume is a loop-mode
    re-entry)."""
    rc = main(["--repo-root", str(tmp_path), "--selfcheck", "--resume", "abc"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--selfcheck cannot be combined with --resume" in captured.err


def test_selfcheck_with_spec_audit_errors_with_mutex_message(tmp_path, capsys):
    """PR-8.5 Task 3 / PR-10 Task 3: ``--selfcheck`` and ``--spec-audit``
    are mutually exclusive — selfcheck is a producer smoke, spec-audit
    is a brief auditor; pass them sequentially."""
    rc = main(["--repo-root", str(tmp_path), "--selfcheck", "--spec-audit", "pr.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--selfcheck cannot be combined with --spec-audit" in captured.err


def test_auth_check_with_pr_doc_errors_with_mutex_message(tmp_path, capsys):
    """PR-9 Task 4: ``--auth-check`` and ``PR_DOC`` are mutually
    exclusive. Caught at the post-parse validation pass before any
    filesystem work."""
    rc = main(["--repo-root", str(tmp_path), "--auth-check", "some-pr.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--auth-check cannot be combined with a PR_DOC" in captured.err


def test_auth_check_with_resume_errors_with_mutex_message(tmp_path, capsys):
    """PR-9 Task 4: ``--auth-check`` and ``--resume`` are mutually
    exclusive (auth-check is a one-shot probe; resume is a loop-mode
    re-entry)."""
    rc = main(["--repo-root", str(tmp_path), "--auth-check", "--resume", "abc"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--auth-check cannot be combined with --resume" in captured.err


def test_auth_check_with_selfcheck_errors_with_mutex_message(tmp_path, capsys):
    """PR-9 Task 4: ``--auth-check`` and ``--selfcheck`` are mutually
    exclusive. They overlap conceptually (both pre-flight) but
    differ in scope and cost — operators run ``--auth-check`` as
    the cheap probe, ``--selfcheck`` as the deeper smoke. Running
    both at once would be wasteful; pass them sequentially."""
    rc = main(["--repo-root", str(tmp_path), "--auth-check", "--selfcheck"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--auth-check cannot be combined with --selfcheck" in captured.err


def test_auth_check_with_spec_audit_errors_with_mutex_message(tmp_path, capsys):
    """PR-9 Task 4 / PR-10 Task 3: ``--auth-check`` and ``--spec-audit``
    are mutually exclusive."""
    rc = main(["--repo-root", str(tmp_path), "--auth-check", "--spec-audit", "pr.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--auth-check cannot be combined with --spec-audit" in captured.err


def test_spec_audit_mutex_with_resume(tmp_path, capsys):
    """PR-10 Task 3: ``--spec-audit`` and ``--resume`` are mutually exclusive."""
    rc = main(["--repo-root", str(tmp_path), "--spec-audit", "pr.md", "--resume", "run-id"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--spec-audit cannot be combined with --resume" in captured.err


def test_spec_audit_flag_dispatches_to_run_spec_audit(tmp_path, monkeypatch, capsys):
    """``--spec-audit`` routes to ``run_spec_audit``, not the main review loop."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    import syncade.spec_audit
    from syncade.spec_audit import SpecAuditOutput, SpecAuditResult

    called_with: dict = {}

    def fake_run(**kwargs):
        called_with.update(kwargs)
        return SpecAuditResult(
            outcome="ready",
            output=SpecAuditOutput(
                verdict="READY",
                findings=[],
                summary="ok",
                priority_order=[],
                coverage_gaps=[],
                dismissed_concerns=[],
            ),
            error=None,
            duration_seconds=0.1,
        )

    monkeypatch.setattr(syncade.spec_audit, "run_spec_audit", fake_run)
    rc = main(["--repo-root", str(tmp_path), "--spec-audit", str(pr_doc)])
    assert rc == 0
    assert "pr_doc_path" in called_with
    assert called_with["pr_doc_path"] == pr_doc


def test_spec_audit_relative_path_resolves_from_repo_root(tmp_path, monkeypatch):
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

    import syncade.spec_audit
    from syncade.spec_audit import SpecAuditOutput, SpecAuditResult

    called_with: dict = {}

    def fake_run(**kwargs):
        called_with.update(kwargs)
        return SpecAuditResult(
            outcome="ready",
            output=SpecAuditOutput(
                verdict="READY",
                findings=[],
                summary="ok",
                priority_order=[],
                coverage_gaps=[],
                dismissed_concerns=[],
            ),
            error=None,
            duration_seconds=0.1,
        )

    monkeypatch.setattr(syncade.spec_audit, "run_spec_audit", fake_run)
    monkeypatch.chdir(other_cwd)

    rc = main(["--repo-root", str(repo), "--spec-audit", "docs/pr.md"])

    assert rc == 0
    assert called_with["pr_doc_path"] == pr_doc


@pytest.mark.parametrize(
    "outcome,use_parse_error,expected_exit",
    [
        ("ready", False, 0),
        ("needs_clarification", False, 10),
        ("subprocess_error", False, 40),
        ("subprocess_error", True, 70),
    ],
)
def test_spec_audit_exit_codes_match_outcome(
    tmp_path, monkeypatch, capsys, outcome, use_parse_error, expected_exit
):
    """CLI exit codes for ``--spec-audit`` match the Task 3 spec (0/10/40/70)."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    _init_git_repo(tmp_path)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    import syncade.spec_audit
    from syncade.spec_audit import (
        SpecAuditFinding,
        SpecAuditOutput,
        SpecAuditOutputError,
        SpecAuditResult,
    )

    if outcome == "subprocess_error":
        err = (
            SpecAuditOutputError("no JSON found (0 candidates)")
            if use_parse_error
            else RuntimeError("codex died")
        )
        result = SpecAuditResult(
            outcome="subprocess_error", output=None, error=err, duration_seconds=0.1
        )
    elif outcome == "needs_clarification":
        result = SpecAuditResult(
            outcome="needs_clarification",
            output=SpecAuditOutput(
                verdict="NEEDS-CLARIFICATION",
                findings=[
                    SpecAuditFinding(
                        severity="blocker",
                        section="Tasks",
                        line=1,
                        issue_class="unverified_claim",
                        finding="unverified claim",
                        suggested_remediation=None,
                    )
                ],
                summary="one blocker",
                priority_order=[0],
                coverage_gaps=[],
                dismissed_concerns=[],
            ),
            error=None,
            duration_seconds=0.1,
        )
    else:
        result = SpecAuditResult(
            outcome="ready",
            output=SpecAuditOutput(
                verdict="READY",
                findings=[],
                summary="ok",
                priority_order=[],
                coverage_gaps=[],
                dismissed_concerns=[],
            ),
            error=None,
            duration_seconds=0.1,
        )

    monkeypatch.setattr(syncade.spec_audit, "run_spec_audit", lambda **_: result)
    rc = main(["--repo-root", str(tmp_path), "--spec-audit", str(pr_doc)])
    assert rc == expected_exit


def test_auth_check_help_appears(capsys):
    """``--auth-check`` is listed in ``--help`` so operators
    discover it via the standard path."""
    with pytest.raises(SystemExit):
        main(["--help"])
    captured = capsys.readouterr()
    assert "--auth-check" in captured.out
    assert "authenticate" in captured.out.lower()


def test_two_dot_with_resume_errors_with_mutex_message(tmp_path, capsys):
    """PR-h-02 increment B: ``--two-dot`` selects a diff RANGE, and a resumed
    run has none left to select — run-init.json records the base OID the
    original run already resolved, so the flag would silently do nothing."""
    rc = main(["--repo-root", str(tmp_path), "--two-dot", "--resume", "abc"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--two-dot cannot be combined with --resume" in captured.err


def test_two_dot_with_gc_errors_because_gc_renders_no_diff(tmp_path, capsys):
    """``--two-dot`` without ``--base``/``--scope`` is rejected before any diff-mode
    check runs, so the error fires on the missing base, not on the --gc combination."""
    rc = main(["--repo-root", str(tmp_path), "--two-dot", "--gc"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--two-dot" in captured.err and "--base" in captured.err


def test_two_dot_without_base_or_scope_is_rejected(tmp_path, capsys):
    """``--two-dot`` without ``--base`` or ``--scope`` has no range to switch;
    validate_command_shape must reject it rather than silently accepting a no-op."""
    rc = main(["--repo-root", str(tmp_path), "--two-dot", "pr.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "--two-dot requires --base or --scope" in captured.err


def test_two_dot_with_base_passes_validate_command_shape(tmp_path, capsys):
    """``--two-dot --base <ref> --doctor`` passes validate_command_shape; subsequent
    failure (no git repo) is exit 60, not 2."""
    rc = main(["--repo-root", str(tmp_path), "--two-dot", "--base", "abc123", "--doctor"])
    # exit 60 (no git repo) means validation passed; exit 2 means rejected
    assert rc != 2
    captured = capsys.readouterr()
    assert "--two-dot requires --base" not in captured.err


def test_empty_base_string_is_rejected(tmp_path, capsys):
    """``--base ""`` is always invalid: take_snapshot treats an empty base as
    absent (``if base_ref:``), silently producing the no-diff full-HEAD path
    instead of a usage error. validate_command_shape must catch it before any
    filesystem work so the operator gets a clear message."""
    rc = main(["--repo-root", str(tmp_path), "--base", "", "pr.md"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--base requires a non-empty ref" in err


def test_two_dot_with_install_skill_is_rejected(tmp_path, capsys):
    """``--install-skill`` renders no diff; ``--two-dot`` with it silently
    ignores the diff-shaping flag. validate_command_shape must reject the
    combination with a clear 'renders no reviewer diff' message."""
    rc = main(
        ["--repo-root", str(tmp_path), "--two-dot", "--base", "main", "--install-skill", "all"]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--two-dot" in err and "--install-skill" in err
    assert "renders no reviewer diff" in err
