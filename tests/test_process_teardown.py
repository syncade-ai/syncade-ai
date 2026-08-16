"""Prompt child teardown on a main-thread interrupt (PR-v2-RS Q2 fast-follow).

The MAJOR the adversarial QA found: a signal during the PARALLEL reviewer phase
lands in the main thread while worker threads are blocked in ``communicate()``;
the workers' own cleanup can't fire, so ThreadPoolExecutor shutdown hangs until
the reviewer timeout and the reviewer subprocesses orphan. Fix: ``run_subprocess``
registers each child's process group; a main-thread teardown kills them so each
``communicate()`` returns at once.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from syncade import process
from syncade.process import SubprocessTimeoutError, run_subprocess


def test_main_thread_terminate_unblocks_worker_communicate():
    """A worker thread blocked in ``run_subprocess(sleep 60)`` must be unblocked
    promptly when the MAIN thread calls ``terminate_active_child_groups()`` — this
    is the core of the reviewing-phase fix."""
    outcome: dict = {}

    def worker():
        t0 = time.monotonic()
        try:
            process.run_subprocess(
                [sys.executable, "-c", "import time; time.sleep(60)"], timeout=120
            )
        finally:
            outcome["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=worker)
    t.start()
    # wait until run_subprocess has launched + registered the child
    for _ in range(250):
        if process._active_procs:
            break
        time.sleep(0.02)
    assert process._active_procs, "child was never registered"

    killed = process.terminate_active_child_groups()  # simulate the dispatcher teardown
    t.join(timeout=10)
    assert not t.is_alive(), "worker did not unblock after terminate"
    assert killed >= 1
    assert outcome["elapsed"] < 8, f"communicate() took {outcome['elapsed']:.1f}s — not prompt"


def test_registry_empty_after_normal_run():
    """run_subprocess unregisters its child on the happy path (no leak)."""
    process.run_subprocess([sys.executable, "-c", "pass"], timeout=30)
    assert not process._active_procs


# Script that forks a grandchild holding stdout/stderr, prints the grandchild's pid, then exits.
# The grandchild inherits fd 1/2 from the fork so the pump threads cannot reach EOF until it
# is killed.  The parent (direct child) exits immediately, making proc.poll() non-None while
# the grandchild is still alive — the scenario that triggered the process-group kill bug.
_FORK_AND_EXIT_SCRIPT = """\
import os, sys, time
sys.stdout.buffer.flush()
pid = os.fork()
if pid:
    sys.stdout.buffer.write(str(pid).encode() + b"\\n")
    sys.stdout.buffer.flush()
    os._exit(0)
else:
    time.sleep(60)
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork not available")
def test_timeout_kills_descendants_after_leader_exit(tmp_path: Path):
    """Timeout must kill the whole process group even when the direct child has already exited.

    Regression: _kill_process_group_if_running returned early when proc.poll() was not None,
    leaving descendants alive and the pump threads blocked indefinitely.
    """
    capture = tmp_path / "rv"
    with pytest.raises(SubprocessTimeoutError):
        run_subprocess(
            [sys.executable, "-c", _FORK_AND_EXIT_SCRIPT],
            capture_prefix=capture,
            timeout=1.5,
        )

    grandchild_pid = int((tmp_path / "rv.stdout").read_text().strip())

    # Give the SIGKILL a moment to propagate, then confirm the grandchild is dead.
    time.sleep(0.15)
    with pytest.raises((ProcessLookupError, PermissionError)):
        os.kill(grandchild_pid, 0)
