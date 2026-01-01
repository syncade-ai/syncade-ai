"""Tests for :mod:`syncade.persistence` — ``persist_run_summary`` (part 1 of 2).

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutputError
from syncade.persistence import persist_round_manifest, persist_run_summary
from syncade.process import SubprocessTimeoutError
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _find_output_line,
    _make_round_dir,
    _no_ship_with_finding,
    _ship,
    _snapshot,
    _subprocess_result,
)


class TestPersistRunSummary:
    def _mixed_dispatch(self) -> DispatchResult:
        """A DispatchResult spanning every outcome shape persist_run_summary
        must render: a SHIP, a NO-SHIP, a timeout, and an output-error."""
        results = [
            ReviewerRunResult(
                reviewer_name="claude-ship",
                provider="anthropic",
                output=_ship(),
                error=None,
                duration_seconds=12.4,
                raw_subprocess_result=_subprocess_result(),
            ),
            ReviewerRunResult(
                reviewer_name="codex-no-ship",
                provider="openai",
                output=_no_ship_with_finding(),
                error=None,
                duration_seconds=30.1,
                raw_subprocess_result=_subprocess_result(),
            ),
            ReviewerRunResult(
                reviewer_name="claude-timeout",
                provider="anthropic",
                output=None,
                error=SubprocessTimeoutError("timed out", stdout="", stderr="", timeout=600.0),
                duration_seconds=600.1,
                raw_subprocess_result=_subprocess_result(rc=-1),
            ),
            ReviewerRunResult(
                reviewer_name="codex-garbage",
                provider="openai",
                output=None,
                error=ReviewerOutputError("not JSON"),
                duration_seconds=4.2,
                raw_subprocess_result=_subprocess_result(),
            ),
        ]
        return DispatchResult(results=results, total_duration_seconds=600.1)

    def test_summary_file_is_written_with_expected_sections(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot(branch="main")
        path = persist_run_summary(
            round_dir, snap, self._mixed_dispatch(), exit_code=40, started_at=_FIXED_STARTED_AT
        )
        assert path == round_dir / "summary.md"
        assert path.is_file()

        text = path.read_text()
        # Heading carries the run id (derived from round_dir.parent.name)
        assert "# Syncade run 2026-05-12T15-30-04" in text
        # Started line renders the run-start instant the caller passed in
        # (deterministic here via _FIXED_STARTED_AT), not a write-time now().
        assert "**Started:** 2026-05-12 15:30:04 UTC" in text
        # Exit code line with the human label
        assert "**Exit code:** 40 (REVIEWER_FAILURE)" in text
        # Repo line with the commit sha + branch
        assert "a" * 40 in text
        assert "on main" in text
        # Every reviewer has a section
        for name in ("claude-ship", "codex-no-ship", "claude-timeout", "codex-garbage"):
            assert f"### {name}" in text
        # Success entries show verdict; failure entries show error class
        assert "**Verdict:** SHIP" in text
        assert "**Verdict:** NO-SHIP" in text
        assert "**Error:** SubprocessTimeoutError" in text
        assert "**Error:** ReviewerOutputError" in text
        # Next steps section, keyed on the exit code
        assert "## Next steps" in text
        assert "--timeout" in text  # exit-40 guidance mentions the flag

    def test_summary_links_match_each_reviewers_outcome(self, tmp_path: Path):
        """Success entries link .parsed.json (not .error.txt); failure
        entries link .error.txt (not .parsed.json). .stdout / .stderr
        always appear. No dangling links either way."""
        round_dir = _make_round_dir(tmp_path)
        path = persist_run_summary(
            round_dir,
            _snapshot(),
            self._mixed_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
        )
        lines = path.read_text().splitlines()

        # The SHIP reviewer's Output line links parsed.json, not error.txt
        ship_output = _find_output_line(lines, "claude-ship")
        assert "claude-ship.parsed.json" in ship_output
        assert "claude-ship.error.txt" not in ship_output
        assert "claude-ship.stdout" in ship_output
        assert "claude-ship.stderr" in ship_output
        # The timed-out reviewer's Output line links error.txt, not parsed.json
        timeout_output = _find_output_line(lines, "claude-timeout")
        assert "claude-timeout.error.txt" in timeout_output
        assert "claude-timeout.parsed.json" not in timeout_output
        assert "claude-timeout.stdout" in timeout_output
        assert "claude-timeout.stderr" in timeout_output

    def test_summary_next_steps_varies_by_exit_code(self, tmp_path: Path):
        """exit 0 gets ship-it guidance; exit 30 points at the parsed
        findings — the Next steps block is keyed on the exit code."""
        snap = _snapshot()
        ship_dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )
        clean = persist_run_summary(
            _make_round_dir(tmp_path, run_id="2026-05-12T15-30-04"),
            snap,
            ship_dispatch,
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
        ).read_text()
        assert "ship it" in clean.lower()
        assert "**Exit code:** 0 (SUCCESS)" in clean

        findings = persist_run_summary(
            _make_round_dir(tmp_path, run_id="2026-05-12T15-31-00"),
            snap,
            ship_dispatch,
            exit_code=30,
            started_at=_FIXED_STARTED_AT,
        ).read_text()
        assert "**Exit code:** 30 (FINDINGS_PRESENT)" in findings
        assert ".parsed.json" in findings  # exit-30 guidance points at findings

    def test_summary_exit_70_next_steps_points_at_stdout_and_error_txt(self, tmp_path: Path):
        """PR-5.6: exit 70's Next steps block must explicitly direct
        the user at .stdout (raw response is preserved there) and
        .error.txt (the parse exception). The pre-PR-5.6 message
        ("tighten the prompt usually fixes it") wasn't actionable —
        the Acme 2026-05-15 user had no idea claude's NO-SHIP
        verdict was actually sitting in .stdout."""
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="claude-reviewer",
                    provider="anthropic",
                    output=None,
                    error=ReviewerOutputError("garbled output"),
                    duration_seconds=320.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=320.0,
        )
        path = persist_run_summary(
            round_dir, snap, dispatch, exit_code=70, started_at=_FIXED_STARTED_AT
        )
        text = path.read_text()
        assert "**Exit code:** 70 (REVIEWER_OUTPUT_UNPARSEABLE)" in text
        # The new Next steps content names .stdout and the user-facing
        # action ("Look for ... result field ... or inline JSON").
        assert ".stdout" in text
        # Names the JSON envelope's `result` field — the most common
        # place to find the verdict.
        assert "result" in text.lower()
        # Mentions "inline JSON" so the user knows to scan the
        # narrative, not just the envelope.
        assert "inline" in text.lower() or "narrative" in text.lower()

    def test_summary_records_detached_head(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot(branch=None)
        text = persist_run_summary(
            round_dir,
            snap,
            self._mixed_dispatch(),
            exit_code=40,
            started_at=_FIXED_STARTED_AT,
        ).read_text()
        assert "(detached HEAD)" in text

    def test_summary_round_dir_must_exist(self, tmp_path: Path):
        bogus = tmp_path / "missing"
        snap = _snapshot()
        dispatch = DispatchResult(results=[], total_duration_seconds=0.0)
        with pytest.raises(FileNotFoundError):
            persist_run_summary(bogus, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT)

    def test_manifest_and_summary_render_the_same_started_at(self, tmp_path: Path):
        """persist_round_manifest and persist_run_summary both receive
        the SAME started_at (run_review captures it once) and render it
        — not each function's own write-time datetime.now(). A regression
        back to datetime.now() fails this deterministic check."""
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )
        # A distinct instant (NOT _FIXED_STARTED_AT) so the assertion
        # proves the value is threaded through, not coincidentally right.
        started_at = datetime(2031, 7, 9, 1, 2, 3, tzinfo=UTC)

        manifest_path = persist_round_manifest(
            round_dir, snap, dispatch, exit_code=0, started_at=started_at
        )
        summary_path = persist_run_summary(
            round_dir, snap, dispatch, exit_code=0, started_at=started_at
        )

        manifest = json.loads(manifest_path.read_text())
        assert manifest["started_at_utc"] == "2031-07-09T01:02:03Z"
        assert "**Started:** 2031-07-09 01:02:03 UTC" in summary_path.read_text()
