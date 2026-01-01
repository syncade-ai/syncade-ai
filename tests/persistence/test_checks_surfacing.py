"""Mechanical-checks surfacing in findings.md / summary.md / manifest.json
(PR-21) + check-aware per-round next-steps (PR-22 blocker 1).

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import (
    persist_findings_md,
    persist_round_manifest,
    persist_run_summary,
)
from syncade.test_runner import TestRunResult
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _ship,
    _snapshot,
    _subprocess_result,
    _synth_output_empty,
    _synth_output_with_findings,
    _synth_result,
)


class TestChecksSurfacing:
    """PR-21 T4: mechanical checks surface in findings.md + summary.md +
    manifest.json. Advisory failures tagged non-blocking. No checks →
    sections / manifest key omitted (byte-identical to pre-PR-21)."""

    def _checks(self):
        return [
            TestRunResult(
                name="file-length",
                severity="advisory",
                exit_code=1,
                outcome="failed",
                duration_seconds=0.5,
                stdout="src/x.py: 506 > 500\n",
                stderr="",
            ),
            TestRunResult(
                name="lint",
                severity="blocking",
                exit_code=0,
                outcome="passed",
                duration_seconds=0.3,
                stdout="",
                stderr="",
            ),
        ]

    def _dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_findings_md_renders_checks_section(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, None, None, check_results=self._checks()
        ).read_text()
        assert "## Mechanical checks" in text
        assert "file-length" in text
        assert "lint" in text
        # The failing ADVISORY check is tagged non-blocking.
        assert "non-blocking" in text

    def test_findings_md_omits_checks_when_none(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT).read_text()
        assert "Mechanical checks" not in text

    def test_summary_renders_checks_section(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            check_results=self._checks(),
        ).read_text()
        assert "## Mechanical checks" in text
        assert "file-length" in text

    def test_summary_omits_checks_when_none(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir, _snapshot(), self._dispatch(), exit_code=0, started_at=_FIXED_STARTED_AT
        ).read_text()
        assert "Mechanical checks" not in text

    def test_manifest_includes_checks_array(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_round_manifest(
            round_dir,
            _snapshot(),
            self._dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            check_results=self._checks(),
        )
        manifest = json.loads(path.read_text())
        assert "checks" in manifest
        assert [c["name"] for c in manifest["checks"]] == ["file-length", "lint"]
        assert manifest["checks"][0]["severity"] == "advisory"
        assert manifest["checks"][0]["blocking"] is False
        assert manifest["checks"][1]["blocking"] is True

    def test_manifest_omits_checks_key_when_none(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        path = persist_round_manifest(
            round_dir, _snapshot(), self._dispatch(), exit_code=0, started_at=_FIXED_STARTED_AT
        )
        manifest = json.loads(path.read_text())
        # Byte-identical to pre-PR-21: no 'checks' key at all.
        assert "checks" not in manifest


class TestSummariesCheckAware:
    """PR-22 blocker 1: per-round summary.md next-steps attribute check-driven
    failures to the check (not synth blockers / a reviewer subprocess). With no
    checks the next-steps are byte-identical to pre-PR-22."""

    def _dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=_ship(),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def _blocking(self, outcome: str, exit_code: int):
        # subprocess_error requires a non-None error AND the exit_code=-1
        # sentinel per TestRunResult's consistency table; passed/failed
        # require error=None and a real exit code.
        if outcome == "subprocess_error":
            error: Exception | None = RuntimeError("binary not found")
            exit_code = -1
        else:
            error = None
        return [
            TestRunResult(
                name="loc",
                severity="blocking",
                exit_code=exit_code,
                outcome=outcome,
                duration_seconds=0.4,
                stdout="src/x.py: 506 > 500\n",
                stderr="",
                error=error,
            )
        ]

    def _next_steps(self, text: str) -> str:
        return text[text.find("## Next steps") :]

    def test_check_failed_next_steps_points_at_check(self, tmp_path: Path):
        # exit 30 driven by a failing BLOCKING check, synth clean (no active
        # blocker). Next-steps must point at the check, not the synth blockers.
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._dispatch(),
            30,
            _FIXED_STARTED_AT,
            _synth_result(output=_synth_output_empty()),
            check_results=self._blocking("failed", 1),
        ).read_text()
        ns = self._next_steps(text)
        assert "mechanical check" in ns.lower()
        # NOT the misleading synth-blocker variant.
        assert "lists the active blockers" not in ns

    def test_check_subprocess_error_next_steps_says_abort(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._dispatch(),
            40,
            _FIXED_STARTED_AT,
            _synth_result(output=_synth_output_empty()),
            check_results=self._blocking("subprocess_error", 127),
        ).read_text()
        ns = self._next_steps(text)
        assert "mechanical check" in ns.lower()
        assert "ABORT" in ns
        # NOT the misleading "a reviewer subprocess failed" variant.
        assert "A reviewer subprocess failed" not in ns

    def test_no_checks_next_steps_unchanged(self, tmp_path: Path):
        # exit 30 from a real synth blocker, no checks → original text verbatim.
        round_dir = _make_round_dir(tmp_path)
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._dispatch(),
            30,
            _FIXED_STARTED_AT,
            _synth_result(output=_synth_output_with_findings()),
        ).read_text()
        assert "lists the active blockers" in self._next_steps(text)
