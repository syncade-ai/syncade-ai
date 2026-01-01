"""The CLI installs signal handlers and exits 128+signum on a caught signal,
finalizing the run-status breadcrumb rather than dying silently."""

from __future__ import annotations

import json
import shutil
import signal
from datetime import UTC

import pytest

from syncade import run_status
from syncade.cli import main
from tests.cli._helpers import _init_git_repo


def _repo_with_config(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[[reviewers]]\nname = "rv1"\nprovider = "fake1"\nmodel = "x"\n[loop]\nmax_rounds = 1\n'
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")
    return pr_doc


def test_main_returns_128_plus_signum_on_signal(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    pr_doc = _repo_with_config(tmp_path)

    import syncade.cli as cli_mod

    def fake_run_review(**kwargs):
        # simulate a signal caught mid-run: the handler recorded SIGTERM, then
        # the interrupt bubbled up (run_review left the breadcrumb un-finalized).
        run_status._received_signal = int(signal.SIGTERM)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "run_review", fake_run_review)
    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 128 + int(signal.SIGTERM)  # 143 — not an uncaught interrupt
    run_status.clear_active()
    run_status._received_signal = None


def test_main_finalizes_status_on_typed_exception_not_stale(tmp_path, monkeypatch):
    """A mid-run WorktreeError is caught by the CLI's TYPED handler (not the
    catch-all), so it must finalize status.json itself — a clean exit-60 must NOT
    leave a `running` breadcrumb that reads as a hard SIGKILL (the adversarial blocker)."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    pr_doc = _repo_with_config(tmp_path)
    from datetime import datetime

    from syncade.worktree import WorktreeError

    captured = {}

    def fake_run_review(*, repo_root, **kwargs):
        rd = repo_root / ".syncade" / "runs" / "R-wt"
        rd.mkdir(parents=True)
        run_status.begin(rd, datetime.now(tz=UTC))
        run_status.update_phase("round-0: reviewing", 0)  # a real post-begin phase
        captured["run_dir"] = rd
        raise WorktreeError("git worktree add failed (leftover dir from a prior crash)")

    import syncade.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run_review", fake_run_review)
    rc = main(["--repo-root", str(tmp_path), str(pr_doc)])
    assert rc == 60  # WORKTREE_ERROR — a clean mapped exit
    status = json.loads((captured["run_dir"] / "status.json").read_text())
    assert status["state"] == "terminated"  # NOT left `running`
    assert status["reason"] == "exception:WorktreeError"
    assert status["exit_code"] == 60
    assert run_status.is_stale_running(status) is False  # NOT misread as a hard kill
    run_status.clear_active()


def test_main_records_unexpected_exception_before_propagating(tmp_path, monkeypatch):
    """AC4: an unexpected exception is recorded (exception:<Type>) before it propagates."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    pr_doc = _repo_with_config(tmp_path)
    from datetime import datetime

    captured = {}

    def fake_run_review(*, repo_root, **kwargs):
        rd = repo_root / ".syncade" / "runs" / "R-exc"
        rd.mkdir(parents=True)
        run_status.begin(rd, datetime.now(tz=UTC))
        captured["run_dir"] = rd
        raise ValueError("unexpected boom")

    import syncade.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run_review", fake_run_review)
    with pytest.raises(ValueError):
        main(["--repo-root", str(tmp_path), str(pr_doc)])
    status = json.loads((captured["run_dir"] / "status.json").read_text())
    assert status["state"] == "terminated"
    assert status["reason"] == "exception:ValueError"
    run_status.clear_active()
