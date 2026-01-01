"""Prompt child teardown on a main-thread interrupt (PR-v2-RS Q2 fast-follow).

The MAJOR the adversarial QA found: a signal during the PARALLEL reviewer phase
lands in the main thread while worker threads are blocked in ``communicate()``;
the workers' own cleanup can't fire, so ThreadPoolExecutor shutdown hangs until
the reviewer timeout and the reviewer subprocesses orphan. Fix: ``run_subprocess``
registers each child's process group; a main-thread teardown kills them so each
``communicate()`` returns at once.
"""

from __future__ import annotations

import sys
import threading
import time

from syncade import process


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
