"""Tests for :mod:`syncade.persistence` — findings.md test-suite section
+ verdict-respects-test-outcome + complete-links coverage.

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import persist_findings_md
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _errored_test_result,
    _failed_test_result,
    _make_round_dir,
    _passed_test_result,
    _ship_with_summary,
    _subprocess_result,
    _synth_output_empty,
    _synth_output_with_findings,
    _synth_result,
)


class TestFindingsMdTestSuiteSection:
    """PR-7.5 task 5: ``persist_findings_md`` prepends a
    ``## Test Suite`` section at the top of findings.md when the
    test leg ran. Detailed test output stays in test-run.stdout;
    findings.md summarizes."""

    def _build_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship_with_summary("summary"),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_test_suite_section_omitted_when_test_result_is_none(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, None, self._build_dispatch()
        ).read_text()
        assert "## Test Suite" not in text

    def test_test_suite_section_rendered_at_top_when_test_passed(self, tmp_path: Path):
        """Section appears BEFORE Synthesis summary so it's the
        first thing the operator sees on a test-leg run."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            _passed_test_result(),
            self._build_dispatch(),
        ).read_text()
        test_idx = text.find("## Test Suite")
        synth_idx = text.find("## Synthesis summary")
        findings_idx = text.find("## Findings")
        assert test_idx != -1
        assert test_idx < synth_idx < findings_idx
        # Outcome + command + duration + pointer all present
        assert "**Outcome:** passed (exit 0)" in text
        assert "**Command:** `pytest -q`" in text
        assert "test-run.stdout" in text

    def test_test_suite_section_renders_failed(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            _failed_test_result(),
            self._build_dispatch(),
        ).read_text()
        ts_idx = text.find("## Test Suite")
        synth_idx = text.find("## Synthesis summary")
        ts_block = text[ts_idx:synth_idx]
        assert "**Outcome:** failed (exit 1)" in ts_block
        assert "test-run.stdout" in ts_block

    def test_test_suite_section_renders_subprocess_error(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            _errored_test_result(),
            self._build_dispatch(),
        ).read_text()
        ts_idx = text.find("## Test Suite")
        synth_idx = text.find("## Synthesis summary")
        ts_block = text[ts_idx:synth_idx]
        assert "subprocess_error" in ts_block
        assert "SubprocessTimeoutError" in ts_block
        assert "test-run.stderr" in ts_block

    def test_findings_md_no_findings_with_test_passed_is_substantive(self, tmp_path: Path):
        """The Phase-04 regression check: a no-findings run with
        the test leg passing should produce a findings.md that's
        substantively richer than pre-PR-7.5's three-line synth
        summary + 'No consolidated findings'. Concretely: more
        than 10 non-empty lines."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            _passed_test_result(),
            self._build_dispatch(),
        ).read_text()
        non_empty_lines = [line for line in text.splitlines() if line.strip()]
        assert len(non_empty_lines) > 10, (
            f"findings.md is too thin ({len(non_empty_lines)} non-empty "
            "lines); the PR-7.5 enrichment should produce a "
            "self-sufficient document"
        )
        # And the structural sections are all present.
        sections = (
            "## Test Suite",
            "## Synthesis summary",
            "## Findings",
            "## Per-reviewer summaries",
        )
        for section in sections:
            assert section in text


class TestFindingsMdVerdictRespectsTestOutcome:
    """T1.6: findings.md headline Verdict must reflect the OVERALL
    mechanical result, not just the synth's view. Pre-T1.6, a
    clean synth + failed test rendered "SHIP" in findings.md while
    the orchestrator's exit code was 30 — two surfaces disagreed."""

    def _build_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship_with_summary("ok"),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def test_synth_clean_test_passed_says_ship(self, tmp_path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, _passed_test_result(), self._build_dispatch()
        ).read_text()
        # The headline (first 5 lines) should say SHIP — both legs clean.
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** SHIP" in head
        # And mention the test re-run passed.
        assert "test re-run passed" in head

    def test_synth_clean_test_failed_says_no_ship_not_ship(self, tmp_path):
        """The headline bug T1.6 fixes: clean synth + failed test
        was rendering SHIP. Now it must say NO-SHIP."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, _failed_test_result(), self._build_dispatch()
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** NO-SHIP" in head
        # And NOT SHIP — explicit anti-regression assertion since
        # the pre-T1.6 bug was exactly "verdict says SHIP".
        # Use the bold-tagged form to avoid matching "NO-SHIP".
        assert "**Verdict:** SHIP" not in head
        # The qualifier names the test-leg-failed reason
        assert "test re-run exit 1" in head
        assert "synthesizer was clean" in head

    def test_synth_clean_test_subprocess_error_says_abort(self, tmp_path):
        """T1.6: subprocess_error is operationally distinct from
        both SHIP and NO-SHIP — the harness couldn't decide. Use
        ABORT wording to make the indeterminate state explicit."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, _errored_test_result(), self._build_dispatch()
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** ABORT" in head, f"expected ABORT, got headline:\n{head}"
        # MUST NOT say SHIP — that was the pre-T1.6 misimplied
        # state for any clean-synth path regardless of test outcome.
        assert "**Verdict:** SHIP" not in head
        # MUST NOT say NO-SHIP either — that would mis-imply a
        # real defect when the actual issue is environmental.
        assert "**Verdict:** NO-SHIP" not in head
        assert "indeterminate" in head

    def test_synth_blocker_says_no_ship_regardless_of_test_result_none(self, tmp_path):
        """Sanity: pre-PR-7.5 verdict-from-synth-blocker path
        unchanged. Synth with active blocker → NO-SHIP. Test leg
        is skipped on this path (orchestrator preconditions), so
        test_result is None here."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, None, self._build_dispatch()
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** NO-SHIP" in head

    def test_synth_clean_test_skipped_says_ship_two_legged(self, tmp_path):
        """Pre-PR-7.5 back-compat: test_command unset means the
        leg never runs. Verdict is SHIP from synth-only; the
        qualifier explicitly notes the test leg was skipped so the
        operator doesn't read the SHIP as three-legged."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, None, self._build_dispatch()
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** SHIP" in head
        assert "test leg skipped" in head


class TestFindingsMdTestSuiteCompleteLinks:
    """T2.9: the ``## Test Suite`` block in findings.md links all
    three test-run artifacts on every outcome (passed, failed,
    subprocess_error). Pre-T2.9, passed/failed only linked
    stdout and subprocess_error omitted exit-code.txt."""

    def _build_dispatch(self) -> DispatchResult:
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv",
                    provider="anthropic",
                    output=_ship_with_summary("ok"),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=1.0,
        )

    def _ts_block(self, tmp_path, test_result):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, test_result, self._build_dispatch()
        ).read_text()
        return text[text.find("## Test Suite") : text.find("## Synthesis summary")]

    def test_passed_links_all_three_artifacts(self, tmp_path):
        block = self._ts_block(tmp_path, _passed_test_result())
        for name in ("test-run.stdout", "test-run.stderr", "test-run.exit-code.txt"):
            assert f"[{name}]" in block or f"]({name})" in block, (
                f"missing {name} link in passed-test-suite block:\n{block}"
            )

    def test_failed_links_all_three_artifacts(self, tmp_path):
        block = self._ts_block(tmp_path, _failed_test_result())
        for name in ("test-run.stdout", "test-run.stderr", "test-run.exit-code.txt"):
            assert f"]({name})" in block, (
                f"missing {name} link in failed-test-suite block:\n{block}"
            )

    def test_subprocess_error_links_all_three_artifacts(self, tmp_path):
        block = self._ts_block(tmp_path, _errored_test_result())
        # Pre-T2.9 the subprocess_error branch omitted
        # exit-code.txt; this assertion is the regression pin.
        for name in ("test-run.stdout", "test-run.stderr", "test-run.exit-code.txt"):
            assert f"]({name})" in block, (
                f"missing {name} link in subprocess_error-test-suite block:\n{block}"
            )
