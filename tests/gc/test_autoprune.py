"""PR-18 issue 2 — auto-prune on loop start.

`.syncade/runs/` must stay bounded without anyone remembering `syncade --gc`.
The PRD's constraints, each pinned by a test below:

  bounded   — transcripts only; no worktree removal, no lsof, no process reaping
  quiet     — a no-op prune says nothing
  protected — resume-eligible runs keep even their transcripts
  safe      — housekeeping NEVER fails a review
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import syncade.orchestrator.loop as loop_module
from syncade.gc import DEFAULT_KEEP, DEFAULT_MAX_AGE_DAYS, autoprune_transcripts
from syncade.logging import Logger

from ._helpers import STRUCTURED_ROUND_ARTIFACTS, make_repo, write_run


def test_autoprune_slims_old_runs_and_keeps_their_history(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    for i in range(1, 4):
        write_run(
            repo,
            f"2026-01-0{i}T00-00-00",
            final_exit_code=0,
            with_round=True,
            transcript_bytes=10_000,
        )

    report = autoprune_transcripts(repo, keep=1)

    assert len(report.runs_slimmed) == 2, "the 2 runs beyond keep=1 should be slimmed"
    assert report.bytes_freed >= 20_000
    for run_id in report.runs_slimmed:
        rd = repo / ".syncade" / "runs" / run_id / "round-0"
        assert not (rd / "codex-reviewer.stdout").exists()
        for name in STRUCTURED_ROUND_ARTIFACTS:
            assert (rd / name).exists(), f"auto-prune destroyed {name}"
    # newest run is inside the keep window — untouched
    newest = repo / ".syncade" / "runs" / "2026-01-03T00-00-00" / "round-0"
    assert (newest / "codex-reviewer.stdout").exists()


def test_autoprune_never_touches_worktrees_or_reaps(tmp_path: Path, monkeypatch) -> None:
    """BOUNDED. Auto-prune is transcripts-only: it must not remove worktree trees,
    shell out to lsof/git-worktree-prune, or SIGKILL anything. Those are the slow and
    destructive half of GC and have no place in a loop's opening moments, where a
    concurrent syncade may be mid-flight."""
    import syncade.gc_execute as gce

    repo = make_repo(tmp_path)
    write_run(repo, "2026-01-01T00-00-00", final_exit_code=0, with_round=True)

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("auto-prune reached the destructive worktree/reap path")

    monkeypatch.setattr(gce, "_reap_and_remove_tree", _boom)
    monkeypatch.setattr(gce, "_lsof_pids_in_tree", _boom)
    monkeypatch.setattr(gce, "_git_worktree_prune", _boom)

    report = autoprune_transcripts(repo, keep=0)

    assert report.runs_slimmed == ["2026-01-01T00-00-00"]
    assert report.worktrees_removed == []
    assert report.pids_reaped == []


def test_autoprune_protects_resume_eligible_runs(tmp_path: Path) -> None:
    """PROTECTED. An interrupted (resume-eligible) run keeps even its transcripts —
    a resume may still need them."""
    repo = make_repo(tmp_path)
    run_dir = write_run(repo, "interrupted", with_loop_manifest=False, with_round=True)

    report = autoprune_transcripts(repo, keep=0)

    assert report.runs_slimmed == []
    assert (run_dir / "round-0" / "codex-reviewer.stdout").exists()


def test_autoprune_is_idempotent_across_loop_starts(tmp_path: Path) -> None:
    """It runs on EVERY fresh loop start, so a second pass must free nothing, report
    nothing, and error nothing."""
    repo = make_repo(tmp_path)
    write_run(repo, "2026-01-01T00-00-00", final_exit_code=0, with_round=True)

    first = autoprune_transcripts(repo, keep=0)
    second = autoprune_transcripts(repo, keep=0)

    assert first.bytes_freed > 0
    assert second.bytes_freed == 0
    assert second.runs_slimmed == []
    assert second.errors == []


# --- the loop-start hook -----------------------------------------------------


def test_hook_is_quiet_when_it_frees_nothing(tmp_path: Path, capsys) -> None:
    """QUIET. A no-op prune prints nothing — the operator's pane is for the review."""
    repo = make_repo(tmp_path)
    loop_module._autoprune_old_transcripts(
        repo, Logger(), keep=DEFAULT_KEEP, max_age_days=DEFAULT_MAX_AGE_DAYS
    )
    assert capsys.readouterr().out == ""


def test_hook_reports_only_when_it_actually_freed_bytes(tmp_path: Path, capsys) -> None:
    """The hook runs at DEFAULT_KEEP (20), so it takes 22 runs before anything is even
    a candidate — which is exactly why the previous test's silence was correct and not
    a bug. Here 2 runs fall outside the keep window and get pruned."""
    repo = make_repo(tmp_path)
    for i in range(1, 23):
        write_run(
            repo,
            f"2026-01-{i:02d}T00-00-00",
            final_exit_code=0,
            with_round=True,
            transcript_bytes=1_000_000,
        )

    loop_module._autoprune_old_transcripts(
        repo, Logger(), keep=DEFAULT_KEEP, max_age_days=DEFAULT_MAX_AGE_DAYS
    )

    out = capsys.readouterr().out
    assert "auto-pruned 2 old run(s)" in out
    assert "run history kept" in out
    assert "MB of transcripts freed" in out
    # the two OLDEST runs lost transcripts; the 20 newest kept theirs
    runs = repo / ".syncade" / "runs"
    assert not (runs / "2026-01-01T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()
    assert not (runs / "2026-01-02T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()
    assert (runs / "2026-01-22T00-00-00" / "round-0" / "codex-reviewer.stdout").exists()


def test_hook_never_fails_a_review(tmp_path: Path, capsys, monkeypatch) -> None:
    """SAFE. Housekeeping must never abort a review. A disk-cleanup error killing an
    expensive multi-round loop is strictly worse than a fat .syncade/."""

    import syncade.gc as gc_module

    def _explode(*a, **k):
        raise OSError("disk on fire")

    # Patch the CONCRETE lookup site: the hook does a function-local
    # `from syncade.gc import autoprune_transcripts` (to break an import cycle), so
    # the name is resolved out of syncade.gc at CALL time, not off loop_module.
    monkeypatch.setattr(gc_module, "autoprune_transcripts", _explode)

    # must NOT raise
    loop_module._autoprune_old_transcripts(
        tmp_path, Logger(), keep=DEFAULT_KEEP, max_age_days=DEFAULT_MAX_AGE_DAYS
    )

    err = capsys.readouterr()
    assert "auto-prune skipped" in (err.out + err.err)


@pytest.mark.parametrize("exc", [OSError("nope"), RuntimeError("boom"), ValueError("bad")])
def test_hook_swallows_every_exception_class(tmp_path: Path, monkeypatch, exc) -> None:
    import syncade.gc as gc_module

    def _explode(*a, **k):
        raise exc

    monkeypatch.setattr(gc_module, "autoprune_transcripts", _explode)
    loop_module._autoprune_old_transcripts(
        tmp_path, Logger(), keep=DEFAULT_KEEP, max_age_days=DEFAULT_MAX_AGE_DAYS
    )  # must not raise


def test_hook_forwards_configured_retention_to_autoprune(tmp_path, monkeypatch) -> None:
    """PR-v2-9 (C3, auto-prune half): the hook forwards its ``keep`` / ``max_age_days`` straight
    to ``autoprune_transcripts``. The loop passes ``config.gc.keep`` / ``config.gc.max_age_days``
    (see ``loop.run_loop``), so ``[gc]`` governs the per-loop prune, not just ``--gc``."""
    import types

    import syncade.gc as gc_module

    captured: dict = {}

    def _capture(repo_root, *, keep, max_age_days):
        captured.update(keep=keep, max_age_days=max_age_days)
        return types.SimpleNamespace(runs_slimmed=[], bytes_freed=0)  # quiet no-op

    monkeypatch.setattr(gc_module, "autoprune_transcripts", _capture)
    loop_module._autoprune_old_transcripts(tmp_path, Logger(), keep=7, max_age_days=3)
    assert captured == {"keep": 7, "max_age_days": 3}


# --- import-cycle guard ------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "syncade.gc",
        "syncade.gc_execute",
        "syncade.gc_protection",
        "syncade.orchestrator.loop",
        "syncade.cli",
    ],
)
def test_every_module_imports_standalone(module: str) -> None:
    """Each module must import as the FIRST module in a fresh interpreter.

    Wiring auto-prune into the loop with a module-scope `from syncade.gc import ...`
    created a cycle: gc -> gc_execute -> gc_protection -> orchestrator.resume ->
    orchestrator/__init__ -> loop -> gc. `import syncade.gc` then died with
    "partially initialized module".

    The whole 1845-test suite passed anyway, and so did the CLI — because pytest and
    the CLI both happen to import `orchestrator` first, which walks the cycle in the
    one order that works. A subprocess per module is the only thing that actually
    catches it, which is why this test shells out instead of just importing.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"`import {module}` fails in a fresh interpreter — likely a circular import:\n"
        f"{result.stderr}"
    )


def test_hook_reports_zero_byte_only_prunes(tmp_path: Path, capsys) -> None:
    """NIT from the PR-18 dogfood (run 2026-07-12T16-53-19, codex-reviewer-adv).

    `execute_gc` was fixed to key its report off artifacts REMOVED rather than bytes
    freed — but this hook still gated its log line on `bytes_freed > 0`, so a prune
    that removed only zero-byte transcripts stayed silent while mutating the tree.
    Same bug, one layer up. Report on runs slimmed.
    """
    repo = make_repo(tmp_path)
    for i in range(1, 23):  # 22 runs > DEFAULT_KEEP (20) -> 2 candidates
        rd = write_run(repo, f"2026-01-{i:02d}T00-00-00", final_exit_code=0, with_round=True)
        for f in (rd / "round-0").rglob("*"):
            if f.is_file() and f.suffix in (".stdout", ".stderr"):
                f.write_text("", encoding="utf-8")  # zero-byte, still real artifacts

    loop_module._autoprune_old_transcripts(
        repo, Logger(), keep=DEFAULT_KEEP, max_age_days=DEFAULT_MAX_AGE_DAYS
    )

    out = capsys.readouterr().out
    assert "auto-pruned 2 old run(s)" in out, "hook stayed silent about a prune it performed"
    # and it really did prune them
    gone = repo / ".syncade" / "runs" / "2026-01-01T00-00-00" / "round-0"
    assert not (gone / "codex-reviewer.stdout").exists()
