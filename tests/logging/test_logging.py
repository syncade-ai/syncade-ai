"""Tests for :mod:`syncade.logging`.

The Logger has no global state and writes via plain ``print`` — so
every test constructs its own Logger and captures output through
pytest's ``capsys``. No subprocesses, no filesystem.
"""

from __future__ import annotations

from syncade.logging import Logger
from tests.logging._helpers import (
    _run_result,
    _ship_result,
)

# ---------------------------------------------------------------------------
# Normal mode: generic events emit timestamped lines
# ---------------------------------------------------------------------------


class TestNormalMode:
    def test_event_emits_line(self, capsys):
        Logger("normal").event("snapshotting repo at /tmp/repo")
        out = capsys.readouterr().out
        assert "/tmp/repo" in out
        assert "snapshot" in out.lower()

    def test_each_event_emits_exactly_one_timestamped_line(self, capsys):
        log = Logger("normal")
        log.event("snapshotting repo at /tmp/repo")
        log.event("snapshot taken — abc123 on main")
        log.event("dispatching 2 reviewer(s)")
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        assert len(lines) == 3
        # every line carries the [HH:MM:SS] timestamp prefix
        for line in lines:
            assert line.startswith("[") and "] " in line

    def test_event_shows_payload(self, capsys):
        Logger("normal").event("  <- rv1 (anthropic) finished — SHIP")
        out = capsys.readouterr().out
        assert "rv1" in out
        assert "SHIP" in out

    def test_error_event_routes_to_stderr(self, capsys):
        Logger("normal").event("  <- rv2 (openai) finished — FAILED (RuntimeError)", error=True)
        captured = capsys.readouterr()
        assert captured.out == ""  # nothing on stdout
        assert "rv2" in captured.err
        assert "RuntimeError" in captured.err
        assert "FAILED" in captured.err

    def test_event_shows_count_and_timeout(self, capsys):
        Logger("normal").event("dispatching 3 reviewer(s) (timeout 600s each)")
        out = capsys.readouterr().out
        assert "3" in out
        assert "600" in out


# ---------------------------------------------------------------------------
# Quiet mode: phase methods silent, normal summaries collapse to one line
# ---------------------------------------------------------------------------


class TestQuietMode:
    def test_events_emit_nothing(self, capsys):
        log = Logger("quiet")
        log.event("snapshotting repo at /tmp/repo")
        log.event("  <- ok-rv finished — SHIP")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""  # non-error events are informational

    def test_error_event_still_emits_to_stderr_in_quiet_mode(self, capsys):
        log = Logger("quiet")
        log.event("  <- ok-rv finished — SHIP")
        log.event("  <- bad-rv finished — FAILED", error=True)
        captured = capsys.readouterr()
        assert captured.out == ""  # the success line was silenced
        assert "bad-rv" in captured.err
        assert "FAILED" in captured.err
        assert "ok-rv" not in captured.err  # success did NOT leak to stderr

    def test_summary_is_a_single_line(self, capsys):
        Logger("quiet").summary(_run_result(_ship_result(), exit_code=0))
        out = capsys.readouterr().out
        assert len(out.strip().splitlines()) == 1
        assert "exit 0" in out
        # The one quiet line points the user at summary.md.
        assert "summary.md" in out


# ---------------------------------------------------------------------------
# Warning method (PR-5.6: surface dirty-tree notices to stderr)
# ---------------------------------------------------------------------------


class TestWarning:
    """Logger.warning is the channel for non-fatal advisory messages
    (today: dirty-tree at snapshot time). Goes to stderr because it's
    a warning, not standard progress; suppressed in quiet mode for
    consistency with the rest of Logger's quiet semantics
    (informational-severity output stays silent under --quiet)."""

    def test_warning_emits_to_stderr_in_normal_mode(self, capsys):
        Logger("normal").warning("working tree has uncommitted changes")
        captured = capsys.readouterr()
        assert captured.out == ""  # not stdout
        assert "working tree has uncommitted changes" in captured.err
        # Carries the [HH:MM:SS] timestamp prefix, like the rest of Logger
        assert captured.err.startswith("[")
        assert "] " in captured.err

    def test_warning_suppressed_in_quiet_mode(self, capsys):
        Logger("quiet").warning("this should not appear")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_public_surface_has_docstrings():
    import inspect

    assert inspect.getdoc(Logger)
    assert inspect.getdoc(Logger.summary)
