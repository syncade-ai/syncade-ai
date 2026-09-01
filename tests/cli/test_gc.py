"""CLI-surface tests for ``syncade --gc`` (PR-27 task 4).

Invokes ``main()`` directly with an explicit ``argv`` list (same convention as
the rest of ``tests/cli/``). Covers dispatch, the side-effect-free ``--gc-dry-run``
contract, mutual exclusivity with every other mode, and the exit-code convention.

CRITICAL TEST SAFETY: every test operates on ``tmp_path`` synthetic dirs and
NEVER lets GC touch the real ``<repo>/.syncade/runs/`` or the real
``/tmp/syncade/``. The CLI handler passes ``worktree_base=config.worktree_base``
to ``gc.plan_gc``; the ``_safe_worktree_base`` fixture wraps ``plan_gc`` to force
that base to a tmp dir, so no test can ever reach the real worktree base.
``syncade --gc`` is NEVER run for real against the operator's repo — these tests
build their own throwaway git repos.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import syncade.gc as gc_module
from syncade.cli import main
from tests.cli._helpers import _init_git_repo
from tests.gc._helpers import snapshot_tree, write_run


@pytest.fixture
def _safe_worktree_base(tmp_path, monkeypatch):
    """Force ``plan_gc``'s ``worktree_base`` to a tmp dir so a ``--gc`` dispatch can NEVER scan or
    delete the real ``/tmp/syncade``.

    The CLI now passes ``worktree_base=config.worktree_base`` EXPLICITLY (PR-v2-9 dogfood fix), so
    patching the keyword-only default no longer helps — an explicit arg wins. Wrap the function to
    override whatever base the CLI hands it. (The "does --gc pass the configured base" behaviour is
    covered separately by ``test_gc_threads_configured_worktree_base``, which does not use this
    fixture.)
    """
    wt_base = tmp_path / "wt_base"
    wt_base.mkdir()
    real_plan_gc = gc_module.plan_gc

    def _forced(repo_root, **kwargs):
        kwargs["worktree_base"] = wt_base
        return real_plan_gc(repo_root, **kwargs)

    monkeypatch.setattr(gc_module, "plan_gc", _forced)
    return wt_base


def _make_git_repo_with_runs(tmp_path: Path) -> Path:
    """A real git repo (so ``discover_repo_root`` resolves) plus a synthetic
    ``.syncade/runs/`` populated with a mix of protected + candidate runs."""
    _init_git_repo(tmp_path)
    (tmp_path / ".syncade" / "runs").mkdir(parents=True)
    # Two candidates (normally-completed) + one protected (env-failure).
    write_run(tmp_path, "2026-01-01T00-00-00", final_exit_code=0, with_round=True)
    write_run(tmp_path, "2026-01-02T00-00-00", final_exit_code=0, with_round=True)
    write_run(tmp_path, "2026-01-03T00-00-00", final_exit_code=40, with_round=True)  # protected
    return tmp_path


def test_gc_dispatch_slims_candidates_keeps_protected(tmp_path, capsys, _safe_worktree_base):
    """``syncade --gc --gc-keep 0`` prunes every candidate's transcripts, keeps every
    run directory and its history, leaves the protected (resume-eligible) run wholly
    untouched, and exits 0.

    The run dirs surviving is the POINT: whole-dir deletion destroyed the corpus
    `metrics.db` is derived from (see tests/gc/test_two_tier_retention.py)."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)
    runs = repo / ".syncade" / "runs"

    rc = main(["--repo-root", str(repo), "--gc", "--gc-keep", "0"])

    assert rc == 0
    # Every run directory and its history SURVIVES.
    for run_id in ("2026-01-01T00-00-00", "2026-01-02T00-00-00", "2026-01-03T00-00-00"):
        assert (runs / run_id).exists()
        assert (runs / run_id / "loop-manifest.json").exists()
        assert (runs / run_id / "round-0" / "manifest.json").exists()
    # The two candidates lost only their transcripts...
    assert not (runs / "2026-01-01T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()
    assert not (runs / "2026-01-02T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()
    # ...and the protected run kept even those.
    assert (runs / "2026-01-03T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()

    out = capsys.readouterr().out
    assert "2 run(s)" in out
    assert "slimmed" in out
    assert "1 protected" in out


def test_gc_dry_run_changes_nothing_on_disk(tmp_path, capsys, _safe_worktree_base):
    """``--gc-dry-run`` reports the plan but deletes nothing — the on-disk
    runs tree is byte-identical before and after, and exit is 0."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)
    runs = repo / ".syncade" / "runs"

    before = snapshot_tree(runs)
    rc = main(["--repo-root", str(repo), "--gc", "--gc-keep", "0", "--gc-dry-run"])
    after = snapshot_tree(runs)

    assert rc == 0
    assert before == after, "dry-run must not change the runs tree"
    out = capsys.readouterr().out
    assert "dry run" in out.lower()
    assert "would slim run" in out


def test_gc_keep_default_keeps_everything_small(tmp_path, capsys, _safe_worktree_base):
    """With the default --gc-keep (20) and only 2 candidates, nothing is
    deleted (both candidates are within the keep window)."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)
    runs = repo / ".syncade" / "runs"

    before = snapshot_tree(runs)
    rc = main(["--repo-root", str(repo), "--gc"])
    after = snapshot_tree(runs)

    assert rc == 0
    assert before == after  # 2 candidates < default keep=20 → nothing slimmed
    assert "0 run(s) slimmed" in capsys.readouterr().out


def _write_gc_config(repo: Path, body: str) -> None:
    (repo / ".syncade" / "config.toml").write_text(body, encoding="utf-8")


def test_gc_config_keep_feeds_the_gc_path(tmp_path, capsys, _safe_worktree_base):
    """PR-v2-9 (C3, --gc half): ``[gc] keep = 0`` in the TOML makes ``--gc`` (no flag) prune
    every candidate — so ``[gc]`` governs ``--gc``, not just the CLI flag or auto-prune."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)
    _write_gc_config(repo, "[gc]\nkeep = 0\n")
    runs = repo / ".syncade" / "runs"

    rc = main(["--repo-root", str(repo), "--gc"])  # no --gc-keep: config supplies it

    assert rc == 0
    assert not (runs / "2026-01-01T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()
    assert not (runs / "2026-01-02T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()
    assert "2 run(s)" in capsys.readouterr().out


def test_gc_cli_keep_overrides_config_keep(tmp_path, capsys, _safe_worktree_base):
    """Precedence: CLI ``--gc-keep`` wins over ``[gc] keep``. Config says prune-everything
    (keep=0), the flag says keep=20 → the 2 candidates survive."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)
    _write_gc_config(repo, "[gc]\nkeep = 0\n")
    runs = repo / ".syncade" / "runs"

    before = snapshot_tree(runs)
    rc = main(["--repo-root", str(repo), "--gc", "--gc-keep", "20"])
    after = snapshot_tree(runs)

    assert rc == 0
    assert before == after  # CLI keep=20 beat config keep=0 → nothing slimmed
    assert "0 run(s) slimmed" in capsys.readouterr().out


def test_gc_bad_config_fails_exit_50_and_changes_nothing(tmp_path, capsys, _safe_worktree_base):
    """A malformed ``[gc]`` value is a config ERROR (brief C3: bad new config fails 50), not a
    silent fallback — ``--gc`` exits 50 like the review/doctor paths and prunes nothing. (PR-v2-9
    dogfood B1: the earlier graceful-degrade let a typo run GC with the wrong retention.)"""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)
    _write_gc_config(repo, "[gc]\nkeep = -5\n")  # ge=0 → ConfigError
    runs = repo / ".syncade" / "runs"

    before = snapshot_tree(runs)
    rc = main(["--repo-root", str(repo), "--gc"])
    after = snapshot_tree(runs)

    assert rc == 50  # CONFIG_ERROR — did not silently substitute defaults
    assert before == after  # nothing pruned
    assert "config error" in capsys.readouterr().err.lower()


def test_gc_does_not_require_actor_api_keys(tmp_path, capsys, monkeypatch, _safe_worktree_base):
    """R2-B2: ``--gc`` spawns no model actors, so a missing actor API key must NOT block
    maintenance — an ``anthropic`` producer on ``auth = "api"`` with no key set fails the REVIEW
    path (exit 50) but ``--gc`` proceeds (exit 0). (Malformed ``[gc]`` still fails 50 — above.)"""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    monkeypatch.delenv("SOME_UNSET_GC_KEY", raising=False)
    repo = _make_git_repo_with_runs(tmp_path)
    _write_gc_config(
        repo,
        '[producer]\nprovider = "anthropic"\nauth = "api"\napi_key_env = "SOME_UNSET_GC_KEY"\n',
    )
    assert main(["--repo-root", str(repo), "--gc"]) == 0  # not 50: GC needs no producer creds


def test_gc_threads_configured_worktree_base(tmp_path, monkeypatch, capsys):
    """PR-v2-9 dogfood B2: ``--gc`` passes ``config.worktree_base`` (and ``--worktree-base``) to
    ``plan_gc``, so a relocated base's leftover worktrees are actually collected. Captures the base
    plan_gc receives — no fixture, no real /tmp/syncade scan."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    import syncade.gc as gc_mod

    repo = _make_git_repo_with_runs(tmp_path)
    (repo / ".syncade" / "config.toml").write_text(
        'worktree_base = "/custom/base"\n', encoding="utf-8"
    )
    captured: dict = {}
    # The stub plan carries every field the renderer reads; the test's subject is the
    # BASE, so a missing field must not be what fails it.
    plan = SimpleNamespace(
        protected_run_ids=[],
        unclaimable_recordless_trees=[],
        unclaimable_unreadable_trees=[],
        unclaimable_bytes=0,
    )
    report = SimpleNamespace(
        dry_run=False,
        runs_slimmed=[],
        bytes_freed=0,
        worktrees_removed=[],
        worktrees_declined=[],
        worktrees_failed=[],
        worktrees_refused=[],
        pids_reaped=[],
        errors=[],
    )
    monkeypatch.setattr(
        gc_mod,
        "plan_gc",
        # **kw rather than a fixed list: this stub broke when `worktree_max_age_days` was added
        # (PR-h-12 item 2), and the test's subject is the BASE, not the argument roster.
        lambda repo_root, *, worktree_base, **kw: captured.update(base=worktree_base, **kw) or plan,
    )
    monkeypatch.setattr(gc_mod, "execute_gc", lambda p, *, dry_run, repo_root: report)

    assert main(["--repo-root", str(repo), "--gc"]) == 0
    assert captured["base"] == Path("/custom/base")  # config base, not /tmp/syncade

    # and --worktree-base overrides the config base
    assert main(["--repo-root", str(repo), "--gc", "--worktree-base", "/flag/base"]) == 0
    assert captured["base"] == Path("/flag/base")


def test_gc_repo_discovery_failure_returns_60(tmp_path, monkeypatch, capsys):
    """No git repo at the hint → discover_repo_root raises SnapshotError →
    exit 60 (WORKTREE_ERROR), per the CLI mode-handler exit-code convention."""
    # Point PATH at an empty dir so git cannot be found → discovery fails.
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    rc = main(["--repo-root", str(tmp_path), "--gc"])
    assert rc == 60
    assert "snapshot error" in capsys.readouterr().err.lower()


def test_gc_help_appears(capsys):
    """``--gc`` and its knobs are listed in ``--help``."""
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "--gc" in out
    assert "--gc-keep" in out
    assert "--gc-max-age-days" in out
    assert "--gc-dry-run" in out


# --- mutual exclusivity: --gc vs every other mode ---------------------------


def test_gc_with_pr_doc_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--gc", "some-pr.md"])
    assert rc == 2
    assert "--gc cannot be combined with a PR_DOC" in capsys.readouterr().err


def test_gc_with_resume_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--gc", "--resume", "abc"])
    assert rc == 2
    assert "--gc cannot be combined with --resume" in capsys.readouterr().err


def test_gc_with_selfcheck_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--gc", "--selfcheck"])
    assert rc == 2
    assert "--gc cannot be combined with --selfcheck" in capsys.readouterr().err


def test_gc_with_auth_check_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--gc", "--auth-check"])
    assert rc == 2
    assert "--gc cannot be combined with --auth-check" in capsys.readouterr().err


def test_gc_with_spec_audit_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--gc", "--spec-audit", "pr.md"])
    assert rc == 2
    assert "--gc cannot be combined with --spec-audit" in capsys.readouterr().err


def test_gc_with_draft_spec_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--gc", "--draft-spec"])
    assert rc == 2
    assert "--gc cannot be combined with --draft-spec" in capsys.readouterr().err


def test_gc_with_openspec_errors_with_mutex_message(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--gc", "--openspec"])
    assert rc == 2
    assert "--gc cannot be combined with --openspec" in capsys.readouterr().err


def test_gc_dispatches_before_other_modes(tmp_path, monkeypatch, capsys, _safe_worktree_base):
    """``--gc`` (alone) routes to ``_run_gc``, not the review loop or any other
    handler."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)

    called: dict = {}

    import syncade.cli.modes as modes

    real_run_gc = modes._run_gc

    def spy(args):
        called["gc"] = True
        return real_run_gc(args)

    monkeypatch.setattr(modes, "_run_gc", spy)
    # Re-import the name the dispatcher reads (cli/__init__ imported it).
    import syncade.cli as cli

    monkeypatch.setattr(cli, "_run_gc", spy)

    rc = main(["--repo-root", str(repo), "--gc"])
    assert rc == 0
    assert called.get("gc") is True


def test_gc_subprocess_invocation_dry_run_is_safe(tmp_path):
    """A CLI subprocess against an isolated throwaway repo exits 0 and deletes
    nothing (the only real-invocation form the brief permits)."""
    if __import__("shutil").which("git") is None:
        pytest.skip("git not on PATH")
    repo = _make_git_repo_with_runs(tmp_path)
    runs = repo / ".syncade" / "runs"
    before = snapshot_tree(runs)

    # Redirect the worktree base via env-free means: a dry run never touches it
    # anyway (side-effect-free), so this is doubly safe.
    # Use sys.executable (NOT bare "python") so the subprocess resolves the SAME
    # interpreter/venv running the tests — a bare "python" can resolve to a
    # system interpreter without syncade installed (PR-27 finding 7).
    bootstrap = """
import sys
from pathlib import Path
import syncade.config_loader as config_loader
import syncade.cli.update_notice as update_notice
config_loader._default_global_config_path = lambda: Path(sys.argv[1])
update_notice.emit_update_notice = lambda **kwargs: False
from syncade.cli import main
raise SystemExit(main(sys.argv[2:]))
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            bootstrap,
            str(tmp_path / "absent-global-config.toml"),
            "--repo-root",
            str(repo),
            "--gc",
            "--gc-dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert snapshot_tree(runs) == before


def test_gc_knobs_without_gc_are_rejected(tmp_path, capsys):
    """PR-27 dogfood finding 3: --gc-keep / --gc-max-age-days / --gc-dry-run are
    meaningful ONLY with --gc. Passing one WITHOUT --gc must error (exit 2), not
    fall through to the normal review path (which would silently ignore the knob
    and even provision reviewers with a PR_DOC)."""
    for argv in (
        ["--repo-root", str(tmp_path), "--gc-dry-run"],
        ["--repo-root", str(tmp_path), "--gc-keep", "5"],
        ["--repo-root", str(tmp_path), "--gc-max-age-days", "7"],
        ["--repo-root", str(tmp_path), "some-pr.md", "--gc-dry-run"],
    ):
        rc = main(argv)
        assert rc == 2, f"{argv} should be rejected (exit 2), got {rc}"
        assert "meaningful only with --gc" in capsys.readouterr().err


def test_negative_gc_knobs_are_rejected(tmp_path):
    """PR-27 dogfood finding 3: negative --gc-keep / --gc-max-age-days are
    rejected by argparse (exit 2) — a destructive maintenance command must not
    accept a negative count/age (a negative --gc-keep would slice from the end
    and delete the NEWEST runs)."""
    for argv in (
        ["--repo-root", str(tmp_path), "--gc", "--gc-keep", "-1"],
        ["--repo-root", str(tmp_path), "--gc", "--gc-max-age-days", "-5"],
    ):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2, argv


def test_gc_reports_declined_workspaces_on_stdout(tmp_path, capsys, monkeypatch):
    """A workspace GC declined must appear on STDOUT, the channel the operator reads.

    Without it the summary is byte-identical to a run that had nothing to remove —
    ``0 worktree(s) removed`` at exit 0 — while the reason sits on stderr under a
    headline that contradicts it. PR-h-06b item 1.
    """
    import syncade.gc_execute as gc_execute_module
    from syncade.process import SubprocessNotFoundError, SubprocessResult
    from syncade.workspace_owner import record_owner
    from tests.gc._helpers import make_repo

    repo = make_repo(tmp_path / "repo")
    # Older than the default 14-day tier-3 floor, so the workspace is genuinely selected.
    write_run(
        repo,
        "run-alpha",
        started_at=datetime.now(UTC) - timedelta(days=60),
        final_exit_code=0,
        with_round=True,
    )
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-alpha"
    tree.mkdir(parents=True)
    record_owner(tree, repo)

    def fake_run(argv, **kwargs):
        if argv[0] == "lsof":
            raise SubprocessNotFoundError("lsof")
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    rc = main(["--repo-root", str(repo), "--gc", "--gc-keep", "0", "--worktree-base", str(wt_base)])

    assert rc == 0
    assert tree.exists()
    out = capsys.readouterr().out
    # The run id deliberately does NOT contain "declined": an earlier version of this
    # test used `run-declined`, so the substring was satisfied by the `slimmed run:`
    # line and the assertion passed with both reporting paths deleted.
    assert "run-alpha" in out and "declined" in out.lower(), out
    assert "1 declined" in out, out  # the headline count, not just the per-tree line
    assert str(tree) in out, out
    assert "no live-process proof" not in out.lower(), "an EPERM decline is not a missing proof"


def test_gc_quiet_declined_path_still_on_stdout(tmp_path, capsys, monkeypatch):
    """--quiet must not hide the path of a declined workspace from stdout.

    The path is the actionable information — an operator reading stdout summary
    output must see which workspace to inspect even when --quiet suppresses the
    verbose transcript lines.
    """
    import syncade.gc_execute as gc_execute_module
    from syncade.process import SubprocessNotFoundError, SubprocessResult
    from syncade.workspace_owner import record_owner
    from tests.gc._helpers import make_repo

    repo = make_repo(tmp_path / "repo")
    write_run(
        repo,
        "run-quiet-check",
        started_at=datetime.now(UTC) - timedelta(days=60),
        final_exit_code=0,
        with_round=True,
    )
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-quiet-check"
    tree.mkdir(parents=True)
    record_owner(tree, repo)

    def fake_run(argv, **kwargs):
        if argv[0] == "lsof":
            raise SubprocessNotFoundError("lsof")
        return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)

    monkeypatch.setattr(gc_execute_module, "run_subprocess", fake_run)

    rc = main(
        [
            "--repo-root",
            str(repo),
            "--gc",
            "--gc-keep",
            "0",
            "--worktree-base",
            str(wt_base),
            "--quiet",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert str(tree) in out, f"declined path must appear on stdout even with --quiet: {out}"


def test_gc_partial_delete_failure_path_on_stdout(tmp_path, capsys, monkeypatch):
    """A partial rmtree failure must put the workspace path on stdout, not only stderr.

    Before the fix, the path only appeared in report.errors → stderr. The summary
    showed '0 worktrees removed' with no path on stdout, indistinguishable from a
    run that had nothing to do.
    """
    import os

    import syncade.gc_execute as gc_execute_module
    from syncade.process import SubprocessResult
    from syncade.workspace_owner import record_owner
    from tests.gc._helpers import make_repo

    repo = make_repo(tmp_path / "repo")
    write_run(
        repo,
        "run-partial",
        started_at=datetime.now(UTC) - timedelta(days=60),
        final_exit_code=0,
        with_round=True,
    )
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-partial"
    locked_sub = tree / "sub"
    locked_sub.mkdir(parents=True)
    (locked_sub / "f.txt").write_text("x", encoding="utf-8")
    record_owner(tree, repo)

    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=1, stdout="", stderr="", duration_seconds=0.0
        ),
    )
    os.chmod(locked_sub, 0o500)
    try:
        rc = main(
            [
                "--repo-root",
                str(repo),
                "--gc",
                "--gc-keep",
                "0",
                "--worktree-base",
                str(wt_base),
            ]
        )
    finally:
        os.chmod(locked_sub, 0o700)

    assert rc == 0
    out = capsys.readouterr().out
    assert str(tree) in out, f"failed-to-remove path must appear on stdout: {out}"


def test_gc_guard_refused_identity_mismatch_path_on_stdout(tmp_path, capsys, monkeypatch):
    """A worktree whose identity changed since planning must appear on stdout as refused.

    Before the fix, the identity-mismatch guard only added to report.errors (stderr).
    The summary showed '0 worktrees removed' with no path on stdout, indistinguishable
    from a clean run. PR-h-06b stdout/path invariant.
    """
    import io
    import shutil
    import sys

    import syncade.gc_execute as gc_execute_module
    from syncade.cli.gc_mode import _report_refused
    from syncade.gc import execute_gc as _execute_gc
    from syncade.gc import plan_gc as _plan_gc
    from syncade.process import SubprocessResult
    from syncade.workspace_owner import record_owner
    from tests.gc._helpers import make_repo

    repo = make_repo(tmp_path / "repo")
    write_run(
        repo,
        "run-identity-refused",
        started_at=datetime.now(UTC) - timedelta(days=60),
        final_exit_code=0,
        with_round=True,
    )
    wt_base = tmp_path / "wt"
    tree = wt_base / "run-identity-refused"
    tree.mkdir(parents=True)
    record_owner(tree, repo)

    monkeypatch.setattr(
        gc_execute_module,
        "run_subprocess",
        lambda argv, **k: SubprocessResult(
            returncode=0, stdout="", stderr="", duration_seconds=0.0
        ),
    )

    plan = _plan_gc(repo, keep=0, max_age_days=0, worktree_base=wt_base, worktree_max_age_days=0)
    assert tree in plan.worktree_trees_to_remove

    # Swap the directory so the identity changes before execute.
    shutil.rmtree(tree)
    tree.mkdir(parents=True)
    record_owner(tree, repo)

    report = _execute_gc(plan, dry_run=False, repo_root=repo)

    assert tree in report.worktrees_refused
    # Verify the path appears on stdout via _report_refused.
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        _report_refused(report)
    finally:
        sys.stdout = old_stdout
    assert str(tree) in buf.getvalue(), (
        "guard-refused path must appear on stdout via _report_refused"
    )
