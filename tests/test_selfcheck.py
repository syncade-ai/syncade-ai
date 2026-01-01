"""Tests for :mod:`syncade.selfcheck` (PR-8.5 Task 3).

Uses :class:`FakeProducerAdapter` exclusively via the ``adapter=``
injection so the unit tests never shell out to a real producer CLI.
The smoke test in ``tests/smoke/test_selfcheck_smoke.py`` covers the
real claude + codex producer paths.

Four outcome paths, one test each:

1. Producer commits with the marker → exit 0 (SUCCESS)
2. Producer stalls → exit 60 (WORKTREE_ERROR)
3. Producer commits but no marker → exit 30 (FINDINGS_PRESENT)
4. Producer raises :class:`ReviewerInvocationError` → exit 60 (WORKTREE_ERROR)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from syncade.adapters.base import Invocation, ReviewerInvocationError
from syncade.adapters.fake import FakeProducerAdapter
from syncade.config import LoopConfig, ProducerConfig, ReviewerConfig, SyncadeConfig
from syncade.exit_codes import FINDINGS_PRESENT, SUCCESS, WORKTREE_ERROR
from syncade.selfcheck import (
    SELFCHECK_MARKER,
    SELFCHECK_SEED_BASENAME,
    run_selfcheck,
)


def _minimal_config() -> SyncadeConfig:
    """Build a :class:`SyncadeConfig` minimally suitable for the
    selfcheck path. The producer block routes through the
    ``adapter=`` injection so the provider/model strings don't
    have to correspond to real CLIs."""
    return SyncadeConfig(
        loop=LoopConfig(timeout_seconds=30.0),
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model="claude-sonnet-4-6",
            )
        ],
        producer=ProducerConfig(
            provider="anthropic",
            model="claude-sonnet-4-6",
        ),
    )


class _MarkerWritingFakeProducer(FakeProducerAdapter):
    """:class:`FakeProducerAdapter` subclass that writes the
    selfcheck marker to ``selfcheck.py`` *before* the parent's
    ``build_invocation`` runs the fixture commit, so the
    selfcheck's marker-check passes.

    Models what a real producer would do: edit the seed file, then
    commit. The fixture commit mechanism is what the parent class
    provides; the marker-write is the selfcheck-specific bit.
    """

    def __init__(self) -> None:
        super().__init__(commit_message="selfcheck: add passed marker")

    def build_invocation(
        self,
        producer_config: ProducerConfig,
        worktree_path: Path,
        prompt: str,
    ) -> Invocation:
        seed = worktree_path / SELFCHECK_SEED_BASENAME
        existing = seed.read_text(encoding="utf-8")
        seed.write_text(existing + SELFCHECK_MARKER + "\n", encoding="utf-8")
        return super().build_invocation(producer_config, worktree_path, prompt)


def test_selfcheck_happy_path_returns_success(capsys):
    """Producer that writes the marker AND commits → exit 0."""
    config = _minimal_config()
    adapter = _MarkerWritingFakeProducer()

    exit_code = run_selfcheck(config, adapter=adapter)

    assert exit_code == SUCCESS
    captured = capsys.readouterr()
    assert "selfcheck OK" in captured.out
    # Workspace cleaned up on success — no preserve line on stderr.
    assert "workspace preserved" not in captured.err


def test_selfcheck_stall_returns_worktree_error(capsys):
    """Producer that doesn't commit (HEAD doesn't move) → exit 60.

    The fake with no ``commit_message`` argument is the stall
    path: ``build_invocation`` skips the fixture commit, the
    no-op subprocess exits 0, ``run_producer``'s HEAD comparison
    finds ``ending == starting`` → ``outcome="stalled"``.
    """
    config = _minimal_config()
    adapter = FakeProducerAdapter()  # No commit_message → stall

    exit_code = run_selfcheck(config, adapter=adapter)

    assert exit_code == WORKTREE_ERROR
    captured = capsys.readouterr()
    assert "selfcheck FAILED" in captured.err
    assert "stalled" in captured.err
    # Operator-actionable hint about the permission level.
    assert "permissions=" in captured.err
    # Workspace preserved on failure for inspection.
    assert "workspace preserved" in captured.err


def test_selfcheck_always_cleanup_removes_workspace_on_failure(capsys, monkeypatch):
    """F3' (dogfood round 2): ``always_cleanup=True`` (used by ``syncade --doctor``, which
    reuses this smoke as an INERT preflight) removes the workspace even on failure and prints
    no preserved-path line — so a doctor run that drops this output leaves nothing behind."""
    import syncade.selfcheck as selfcheck_mod

    created: dict[str, str] = {}
    real_mkdtemp = selfcheck_mod.tempfile.mkdtemp

    def _tracking_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        created["path"] = path
        return path

    monkeypatch.setattr(selfcheck_mod.tempfile, "mkdtemp", _tracking_mkdtemp)
    config = _minimal_config()

    # No commit_message -> stall -> failure (would preserve the workspace by default).
    exit_code = run_selfcheck(config, adapter=FakeProducerAdapter(), always_cleanup=True)

    assert exit_code == WORKTREE_ERROR  # still the same failure verdict
    captured = capsys.readouterr()
    assert "workspace preserved" not in captured.err  # no preserved-path line
    assert not Path(created["path"]).exists()  # ...and the workspace is gone


def test_selfcheck_commit_without_marker_returns_findings_present(capsys):
    """Producer that commits a fixture file but doesn't touch
    ``selfcheck.py`` → exit 30.

    The plain :class:`FakeProducerAdapter` with ``commit_message``
    writes a fixture file ``.syncade-fake-producer-N`` and commits;
    it does NOT add the marker to ``selfcheck.py``. The
    selfcheck's marker-text check fails → exit 30.
    """
    config = _minimal_config()
    adapter = FakeProducerAdapter(commit_message="fix: address fictional finding")

    exit_code = run_selfcheck(config, adapter=adapter)

    assert exit_code == FINDINGS_PRESENT
    captured = capsys.readouterr()
    assert "selfcheck FAILED" in captured.err
    assert "does not contain" in captured.err
    assert SELFCHECK_MARKER in captured.err
    # Workspace preserved on failure for inspection.
    assert "workspace preserved" in captured.err


def test_selfcheck_subprocess_error_returns_worktree_error(capsys):
    """Producer adapter that raises :class:`ReviewerInvocationError`
    during ``parse_output`` → ``outcome="subprocess_error"`` →
    exit 60.

    Models the real-CLI failure path where the producer subprocess
    runs but the adapter's structured-output extraction fails (e.g.
    a non-zero rc, the provider's ``is_error: true`` envelope).
    """
    config = _minimal_config()
    adapter = FakeProducerAdapter(
        canned_exception=ReviewerInvocationError(
            "simulated producer subprocess failure",
            returncode=1,
            stdout="",
            stderr="simulated stderr",
        )
    )

    exit_code = run_selfcheck(config, adapter=adapter)

    assert exit_code == WORKTREE_ERROR
    captured = capsys.readouterr()
    assert "selfcheck FAILED" in captured.err
    assert "subprocess error" in captured.err
    assert "ReviewerInvocationError" in captured.err
    # Workspace preserved on failure for inspection.
    assert "workspace preserved" in captured.err


def test_selfcheck_quiet_suppresses_stdout_progress(capsys):
    """``quiet=True`` silences the progress / OK lines on stdout
    while leaving error lines on stderr untouched.

    Mirrors :class:`syncade.logging.Logger`'s quiet semantics —
    operators using ``--quiet`` still see failure paths."""
    config = _minimal_config()
    adapter = _MarkerWritingFakeProducer()

    exit_code = run_selfcheck(config, adapter=adapter, quiet=True)

    assert exit_code == SUCCESS
    captured = capsys.readouterr()
    # Quiet → no informational stdout
    assert captured.out == ""


def test_selfcheck_preserved_workspace_path_printed_on_failure(capsys, tmp_path):
    """On any failure, the preserved workspace path is the last
    line written to stderr so the operator can copy-paste it
    straight into ``ls`` / ``cat``.

    Verifies the operator hand-off contract: failure path always
    surfaces the preserved-workspace pointer, no matter which
    outcome triggered the failure. Also asserts the
    ``producer.stdout / .stderr / .commit.txt`` artifacts live
    next to ``findings.md`` so the operator can inspect raw
    producer output (the dogfood-QA contract: "cat
    <workspace>/round-0/producer.stdout").
    """
    config = _minimal_config()
    adapter = FakeProducerAdapter()  # Stall

    exit_code = run_selfcheck(config, adapter=adapter)

    assert exit_code == WORKTREE_ERROR
    captured = capsys.readouterr()
    # Last non-empty line of stderr is the workspace-preserved pointer.
    stderr_lines = [ln for ln in captured.err.strip().splitlines() if ln.strip()]
    assert stderr_lines, "expected at least one stderr line on failure"
    last = stderr_lines[-1]
    assert "workspace preserved at:" in last
    # Path on disk exists and contains the workspace layout the
    # docstring promises.
    path_str = last.rsplit("workspace preserved at:", 1)[1].strip()
    workspace = Path(path_str)
    try:
        assert workspace.exists()
        assert (workspace / "repo").exists()
        round_dir = workspace / "round-0"
        assert (round_dir / "findings.md").exists()
        # PR-8.5 dogfood QA fix: producer artifacts must be persisted
        # so the operator can inspect raw producer output. Stall path
        # writes stdout/stderr/commit.txt; subprocess_error adds
        # error.txt. This test exercises stall, so error.txt is
        # absent.
        assert (round_dir / "producer.stdout").exists()
        assert (round_dir / "producer.stderr").exists()
        assert (round_dir / "producer.commit.txt").exists()
        # Stall path: commit.txt holds starting_sha (no commit made).
        commit_sha = (round_dir / "producer.commit.txt").read_text().strip()
        assert len(commit_sha) == 40
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_selfcheck_subprocess_error_persists_error_artifact(capsys):
    """PR-8.5 dogfood QA: on the subprocess_error path, the
    ``producer.error.txt`` artifact is written alongside the other
    producer.* files so the operator can read the exception
    class + message without re-running."""
    config = _minimal_config()
    adapter = FakeProducerAdapter(
        canned_exception=ReviewerInvocationError(
            "selfcheck-injected producer failure",
            returncode=1,
            stdout="",
            stderr="",
        )
    )

    exit_code = run_selfcheck(config, adapter=adapter)

    assert exit_code == WORKTREE_ERROR
    captured = capsys.readouterr()
    # Extract the preserved workspace path and verify the producer
    # error artifact landed there.
    stderr_lines = [ln for ln in captured.err.strip().splitlines() if ln.strip()]
    last = stderr_lines[-1]
    path_str = last.rsplit("workspace preserved at:", 1)[1].strip()
    workspace = Path(path_str)
    try:
        round_dir = workspace / "round-0"
        error_path = round_dir / "producer.error.txt"
        assert error_path.exists(), (
            "producer.error.txt missing from preserved workspace on subprocess_error path"
        )
        body = error_path.read_text()
        assert "ReviewerInvocationError" in body
        assert "selfcheck-injected producer failure" in body
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_selfcheck_git_setup_failure_returns_exit_60(monkeypatch, capsys):
    """PR-8.5 R1.T1: a failing setup git command (init/add/commit/
    rev-parse) is mapped to WORKTREE_ERROR (exit 60) with the
    preserved-workspace contract intact — NOT an uncaught
    :class:`subprocess.CalledProcessError` traceback leaking out
    through the CLI.

    Models codex-reviewer's reproducer pattern from the brief:
    monkey-patch :func:`syncade.selfcheck._git` to raise
    ``CalledProcessError`` on first invocation; run selfcheck; assert
    the exception is mapped to the documented exit code instead of
    propagating. Asserts: (a) returns 60, (b) preserved-workspace
    path printed as the final stderr line (same contract as the
    stall / subprocess_error paths), (c) the named failing step
    (``init``) appears in stderr so the operator can diagnose, and
    (d) no Python traceback bleeds into stderr.
    """

    def boom(cwd, *args):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["git", *args],
            stderr="simulated git failure",
        )

    monkeypatch.setattr("syncade.selfcheck._git", boom)

    config = _minimal_config()
    exit_code = run_selfcheck(config)

    assert exit_code == WORKTREE_ERROR
    captured = capsys.readouterr()
    # (c) Named failing step in stderr (init is the first _git call).
    assert "selfcheck FAILED" in captured.err
    assert "init" in captured.err
    # (d) No Python traceback bleeds through.
    assert "Traceback" not in captured.err
    # (b) Preserved-workspace path is the final non-empty stderr line.
    stderr_lines = [ln for ln in captured.err.strip().splitlines() if ln.strip()]
    assert stderr_lines, "expected at least one stderr line on failure"
    last = stderr_lines[-1]
    assert "workspace preserved at:" in last
    path_str = last.rsplit("workspace preserved at:", 1)[1].strip()
    workspace = Path(path_str)
    try:
        assert workspace.exists()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_selfcheck_git_helper_uses_bounded_run_subprocess(monkeypatch, tmp_path):
    calls = []

    def fake_run_subprocess(argv, *, cwd=None, env=None, timeout=None, input_text=None):
        calls.append((argv, cwd, timeout))
        from syncade.process import SubprocessResult

        return SubprocessResult(returncode=0, stdout="abc123\n", stderr="", duration_seconds=0.0)

    monkeypatch.setattr("syncade.selfcheck.run_subprocess", fake_run_subprocess)

    from syncade.selfcheck import _git

    result = _git(tmp_path, "rev-parse", "HEAD")

    assert result.stdout == "abc123\n"
    assert calls == [
        (
            [
                "git",
                "-c",
                "user.email=selfcheck@syncade.test",
                "-c",
                "user.name=Syncade Selfcheck",
                "rev-parse",
                "HEAD",
            ],
            tmp_path,
            30.0,
        )
    ]
