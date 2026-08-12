"""`--resume` says when the run it is resuming was hard-killed — PR-h-field-02 item 1.

`run_status.is_stale_running` existed and nothing in the product called it: a `running` state
against a dead pid was detectable and never reported. Three field runs died exactly that way and
the operator's only clue was a status file still claiming the run was in progress.

Nothing in-process can finalize that file after SIGKILL, by definition — so the fix is not
"write it on the way out", it is removing the silence at the place someone actually goes after a
run vanishes.

These assert on the FILE and the emitted text, not on an internal call, because the claim is
about what an operator sees.
"""

from __future__ import annotations

import json

from syncade import run_status
from syncade.cli.resume_mode import _report_hard_kill


def _write(tmp_path, payload: dict):
    (tmp_path / "status.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_a_hard_killed_run_is_reported(tmp_path, capsys):
    # pid 999999 is not alive; state still 'running' == the SIGKILL signature.
    _write(tmp_path, {"state": "running", "phase": "round-2: reviewing", "pid": 999999})
    _report_hard_kill(tmp_path, run_status)
    err = capsys.readouterr().err
    assert "HARD-KILLED" in err
    assert "round-2: reviewing" in err, "the phase is the single most useful fact; name it"
    assert "reused" in err, "an operator needs to know completed rounds survive"


def test_a_cleanly_terminated_run_is_NOT_reported(tmp_path, capsys):
    """The counter-test: a message on every resume would train people to ignore it."""
    _write(tmp_path, {"state": "terminated", "reason": "ship", "exit_code": 0, "pid": 999999})
    _report_hard_kill(tmp_path, run_status)
    assert capsys.readouterr().err == ""


def test_a_still_running_run_is_NOT_reported(tmp_path, capsys):
    """A live pid means a concurrent run, not a corpse — different problem, different message."""
    import os

    _write(tmp_path, {"state": "running", "phase": "round-1: reviewing", "pid": os.getpid()})
    _report_hard_kill(tmp_path, run_status)
    assert capsys.readouterr().err == ""


def test_a_missing_or_corrupt_status_file_is_silent(tmp_path, capsys):
    """Diagnostics must never become the reason a resume fails."""
    _report_hard_kill(tmp_path, run_status)  # no file at all
    (tmp_path / "status.json").write_text("{not json", encoding="utf-8")
    _report_hard_kill(tmp_path, run_status)
    assert capsys.readouterr().err == ""


def test_it_matches_the_real_field_status_files(tmp_path, capsys):
    """The exact shape of the three runs that motivated this, transcribed from disk."""
    _write(
        tmp_path,
        {"state": "running", "phase": "round-2: reviewing", "round": 2, "pid": 63045},
    )
    _report_hard_kill(tmp_path, run_status)
    assert "HARD-KILLED" in capsys.readouterr().err
