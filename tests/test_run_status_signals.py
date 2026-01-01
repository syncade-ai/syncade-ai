"""End-to-end: the status.json breadcrumb matches reality under a REAL OS signal.

The unit tests prove the pieces in-process; this spawns a real subprocess that
registers a breadcrumb + a phase, then delivers an actual signal from the parent
and asserts the on-disk file — the "data matches the actual process state" gate.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from syncade import run_status

# A tiny driver: register the breadcrumb, mark a phase, then block. On a caught
# signal it finalizes (signal:NAME) + exits 128+signum. SIGKILL can't run any of
# the except body — the breadcrumb then stays `running` at its last phase.
_DRIVER = r"""
import sys, time
from datetime import datetime, timezone
from syncade import run_status
run_dir = sys.argv[1]
with run_status.install_signal_handlers():
    run_status.begin(run_dir, datetime.now(tz=timezone.utc))
    run_status.update_phase("round-1: producer", 1)
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        sys.exit(run_status.finalize_signal())
"""


def _spawn(run_dir: Path) -> subprocess.Popen:
    """Start the driver and wait until it has recorded the round-1 producer phase
    (so the signal lands deterministically at that phase, not mid-startup)."""
    proc = subprocess.Popen([sys.executable, "-c", _DRIVER, str(run_dir)])
    status_path = run_dir / "status.json"
    for _ in range(250):  # up to ~5s
        try:
            if json.loads(status_path.read_text()).get("phase") == "round-1: producer":
                return proc
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        time.sleep(0.02)
    return proc  # fall through; the assertions will surface the failure


def test_sigterm_finalizes_status_with_signal_reason(tmp_path):
    proc = _spawn(tmp_path)
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=10)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "terminated"
    assert status["reason"] == "signal:SIGTERM"
    assert status["phase"] == "round-1: producer"  # died exactly where it was
    assert rc == 128 + int(signal.SIGTERM)  # 143 — conventional signal exit


def test_sigkill_leaves_stale_running_breadcrumb(tmp_path):
    proc = _spawn(tmp_path)
    proc.send_signal(signal.SIGKILL)  # uncatchable — nothing can finalize
    proc.wait(timeout=10)  # reap so the pid is truly gone (not a zombie)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "running"  # stuck at the last phase it reached
    assert status["phase"] == "round-1: producer"
    # a running breadcrumb whose pid is dead IS the evidence of a hard kill
    assert run_status.is_stale_running(status) is True
