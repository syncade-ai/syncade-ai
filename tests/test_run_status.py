"""run_status breadcrumb: never terminate without a discoverable reason.

Unit contract for the live ``status.json`` writer + stale-running detection.
Signal-handler behavior is exercised here (deliver-to-self) and end-to-end under
a real OS signal in ``tests/test_run_status_signals.py``.
"""

from __future__ import annotations

import json
import os
import signal as _signal
from datetime import UTC, datetime

from syncade import run_status


def _read(run_dir):
    return json.loads((run_dir / "status.json").read_text())


def _started():
    return datetime(2026, 7, 5, tzinfo=UTC)


def test_begin_writes_running_with_phase_and_pid(tmp_path):
    run_status.clear_active()
    run_status.begin(tmp_path, _started())
    s = _read(tmp_path)
    assert s["state"] == "running"
    assert s["pid"] == os.getpid()
    assert s["phase"] == "starting"
    assert "started_at_utc" in s and "updated_at_utc" in s
    run_status.clear_active()


def test_update_phase_updates_active(tmp_path):
    run_status.clear_active()
    run_status.begin(tmp_path, _started())
    run_status.update_phase("round-1: producer", round_index=1)
    s = _read(tmp_path)
    assert s["state"] == "running"
    assert s["phase"] == "round-1: producer"
    assert s["round"] == 1
    run_status.clear_active()


def test_update_phase_noop_without_active(tmp_path):
    run_status.clear_active()
    run_status.update_phase("x")  # must not raise
    assert not (tmp_path / "status.json").exists()


def test_finalize_active_writes_terminated_and_clears(tmp_path):
    run_status.clear_active()
    run_status.begin(tmp_path, _started())
    run_status.update_phase("synthesizing")
    run_status.finalize_active("ship", 0)
    s = _read(tmp_path)
    assert s["state"] == "terminated"
    assert s["reason"] == "ship"
    assert s["exit_code"] == 0
    assert s["phase"] == "synthesizing"  # last phase retained
    assert run_status.active() is None  # cleared
    run_status.clear_active()


def test_finalize_active_noop_without_active(tmp_path):
    run_status.clear_active()
    run_status.finalize_active("ship", 0)  # must not raise
    assert not (tmp_path / "status.json").exists()


def test_is_stale_running_true_for_dead_pid():
    # a running status whose pid is not alive IS the hard-kill breadcrumb
    assert run_status.is_stale_running({"state": "running", "pid": 999999}) is True


def test_is_stale_running_false_for_live_pid_and_terminated():
    assert run_status.is_stale_running({"state": "running", "pid": os.getpid()}) is False
    assert run_status.is_stale_running({"state": "terminated", "pid": 999999}) is False
    assert run_status.is_stale_running({"state": "running"}) is False  # no pid → not stale


def test_handler_raises_keyboardinterrupt_and_records_signum():
    run_status._received_signal = None
    raised = False
    with run_status.install_signal_handlers():
        try:
            os.kill(os.getpid(), _signal.SIGTERM)  # delivered to self; handler raises
        except KeyboardInterrupt:
            raised = True
    assert raised is True
    assert run_status.received_signal() == int(_signal.SIGTERM)


def test_install_signal_handlers_restores_previous():
    before = _signal.getsignal(_signal.SIGTERM)
    with run_status.install_signal_handlers():
        assert _signal.getsignal(_signal.SIGTERM) is not before
    assert _signal.getsignal(_signal.SIGTERM) is before


def test_signal_exit_code():
    assert run_status.signal_exit_code(int(_signal.SIGTERM)) == 128 + int(_signal.SIGTERM)  # 143
    assert run_status.signal_exit_code(None) == 128 + int(_signal.SIGINT)  # 130 fallback


def test_finalize_signal_records_reason_returns_code_and_logs_stderr(tmp_path, capsys):
    run_status.clear_active()
    run_status._received_signal = int(_signal.SIGTERM)
    run_status.begin(tmp_path, _started())
    code = run_status.finalize_signal()
    s = _read(tmp_path)
    assert s["state"] == "terminated"
    assert s["reason"] == "signal:SIGTERM"
    assert code == 128 + int(_signal.SIGTERM)
    assert s["exit_code"] == code
    err = capsys.readouterr().err  # AC3: a stderr line accompanies signal termination
    assert "SIGTERM" in err and "status.json" in err
    run_status.clear_active()
    run_status._received_signal = None


def test_finalize_signal_no_active_does_not_claim_status_written(capsys):
    """finalize_signal() with no active breadcrumb must not claim
    'recorded in status.json' — that would be a false promise (no run dir exists
    to write into, and finalize_active is a no-op when _active is None)."""
    run_status.clear_active()
    run_status._received_signal = int(_signal.SIGTERM)
    # Deliberately do NOT call begin() — simulate a signal before any run dir exists.
    code = run_status.finalize_signal()
    err = capsys.readouterr().err
    assert "SIGTERM" in err
    assert "status.json" not in err or "not written" in err, (
        "finalize_signal() must not claim 'recorded in status.json' "
        "when no active breadcrumb exists"
    )
    assert code == 128 + int(_signal.SIGTERM)
    run_status.clear_active()
    run_status._received_signal = None
