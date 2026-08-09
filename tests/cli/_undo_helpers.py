"""Shared fixtures for the two undo suites.

`test_undo_auto_init.py` asks WHEN undo fires; `test_undo_scope.py` asks WHAT it removes.
Both need the same empty-work-dir fixture and the same post-undo contract, so it lives here
rather than being imported across test modules.
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _assert_undone(work, why=""):
    """The post-undo contract: **the repository is gone; nothing else was touched.**

    Asserts the RESIDUE exactly rather than "no .git", so a NEW thing syncade starts deleting
    fails here instead of passing silently. Two permitted survivors, each deliberate:
    `.gitignore` (bytes an operator could have edited) and `.syncade/` (run state, governed by
    the retention rule that GC never deletes a run directory — see `undo_auto_init`).
    """
    left = sorted(p.name for p in work.iterdir())
    assert ".git" not in left, f"the repository survived the refusal{why}"
    assert set(left) <= {".gitignore", ".syncade"}, f"unexpected residue {left}{why}"


def _fresh(tmp_path, name="w"):
    """An EMPTY work dir, with the brief OUTSIDE it.

    The brief must not live in the work dir: auto-init refuses a populated directory, so a
    brief inside makes the run refuse BEFORE the mutation — and a test asserting "no .git"
    would then pass without ever exercising undo. Returns (work, brief).
    """
    work = tmp_path / name
    work.mkdir()
    brief = tmp_path / f"{name}-brief.md"
    brief.write_text("# PR\n")
    return work, brief


def _dispatch(started):
    return type("D", (), {"reviewer_subprocess_started": started})()


def _result(exit_code, reviewer_subprocess_started=True):
    """Stub for the fields undo reads: rounds[*].dispatch_result.reviewer_subprocess_started."""
    round_ = type("Rnd", (), {"dispatch_result": _dispatch(reviewer_subprocess_started)})()
    return type("R", (), {"exit_code": exit_code, "rounds": [round_]})()


def _fake_review(work, exit_code, reviewer_subprocess_started=True):
    """A stub that behaves like the real thing where it matters: it writes a run directory."""

    def _run(*a, **k):
        (work / ".syncade" / "runs" / "2026-01-01T00-00-00").mkdir(parents=True, exist_ok=True)
        return _result(exit_code, reviewer_subprocess_started)

    return _run
