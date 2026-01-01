"""Real-process reaping smoke tests for resume stale-worktree cleanup (M2).

Marked ``@pytest.mark.smoke`` so the default suite deselects it (the default
``addopts`` is ``-m 'not smoke'``). Like ``tests/gc/test_smoke.py`` these need
only real ``lsof`` + ``sleep`` (no provider CLIs): they spawn real ``sleep``
processes with cwds under a ``tmp_path`` base they create — NEVER
``/tmp/syncade``.

These cover the RESUME path's cleanup, which has two removal modes that must behave
differently:

- EXTERNAL worktree subtree (``reap=True``): a process whose cwd is inside is
  reaped BEFORE the ``rmtree`` (GC-equivalent — GC does still remove worktree
  trees), so the worktree is never deleted out from under it.
- Persisted run-artifact dir under ``.syncade/runs/`` (``reap=False``): resume drops
  a partial ``round-N/`` so it can be re-run, and an operator may be inspecting it,
  so it must NOT SIGKILL inside ``.syncade/runs/``.

Do not read the second mode as "GC does this too": since PR-v2-18, **GC never removes
a run directory**. It prunes subprocess transcripts and keeps the history. Removing a
run-artifact dir is resume's behavior alone.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from syncade.orchestrator.loop import _safe_resume_rmtree


def _assert_dead(proc: subprocess.Popen, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {proc.pid} was not reaped within {timeout}s")


@pytest.mark.smoke
def test_resume_worktree_cleanup_reaps_in_cwd_process(tmp_path: Path) -> None:
    """reap=True (external worktree): a live process with cwd INSIDE the
    doomed subtree is reaped and the dir removed; a process with cwd OUTSIDE
    it stays alive."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base = tmp_path / "wt"
    tree = base / "run" / "round-1"
    tree.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    # start_new_session=True mirrors process.run_subprocess; the reap still
    # kills only the exact cwd-confirmed PID.
    inside_proc = subprocess.Popen(["sleep", "60"], cwd=str(tree), start_new_session=True)
    outside_proc = subprocess.Popen(["sleep", "60"], cwd=str(outside), start_new_session=True)
    try:
        time.sleep(0.5)  # let the OS register the cwds for lsof

        _safe_resume_rmtree(tree, base, repo_root, reap=True)

        _assert_dead(inside_proc, timeout=5.0)
        assert not tree.exists()
        assert outside_proc.poll() is None, "outside process must still be alive"
    finally:
        for p in (inside_proc, outside_proc):
            if p.poll() is None:
                p.kill()
            p.wait()


@pytest.mark.smoke
def test_resume_artifact_cleanup_does_not_kill_in_cwd_process(tmp_path: Path) -> None:
    """reap=False (persisted run-artifact dir): a live process with cwd INSIDE
    the removed dir is NOT killed — the M2 over-reach fix. The dir is still
    removed by the guarded rmtree (Unix allows unlinking a process's cwd)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base = tmp_path / "runs"
    tree = base / "run" / "round-1"
    tree.mkdir(parents=True)

    inside_proc = subprocess.Popen(["sleep", "60"], cwd=str(tree), start_new_session=True)
    try:
        time.sleep(0.5)

        _safe_resume_rmtree(tree, base, repo_root)  # default reap=False

        # The process is NOT reaped — give the same window the reap path uses,
        # then assert it is still alive.
        time.sleep(0.5)
        assert inside_proc.poll() is None, "artifact-dir cleanup must not SIGKILL"
        assert not tree.exists()  # guarded rmtree still ran
    finally:
        if inside_proc.poll() is None:
            inside_proc.kill()
        inside_proc.wait()
