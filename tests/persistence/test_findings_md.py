"""Tests for :mod:`syncade.persistence`.

Constructs :class:`ReviewerRunResult` / :class:`DispatchResult` /
:class:`Snapshot` / :class:`SubprocessResult` directly — no real
subprocess calls or git operations. The orchestrator is the only
production caller; these tests target the persistence module in
isolation so a future regression in file-layout, JSON shape, or
manifest schema fails here rather than at the integration boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutputError
from syncade.persistence import persist_findings_md
from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _ship_with_summary,
    _subprocess_result,
    _synth_output_empty,
    _synth_output_with_findings,
    _synth_result,
)


class TestPersistFindingsMd:
    def test_renders_active_blocker_and_dismissed_nit(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        path = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT)
        assert path == round_dir / "findings.md"
        text = path.read_text()

        # Heading carries the run id.
        assert "# Findings — Syncade run 2026-05-12T15-30-04" in text
        # Verdict is mechanical (NO-SHIP because the consolidation
        # contains an active blocker — derived via has_active_blocker,
        # NOT via exit_code; R2.8 removed the exit_code param).
        assert "**Verdict:** NO-SHIP" in text
        # Synthesis summary appears verbatim.
        assert "Two findings consolidated" in text

        # Active blocker section
        assert "### [blocker] user.email column missing NOT NULL constraint" in text
        assert "**File:** `src/db/schema.sql`" in text
        assert "**Status:** Active" in text
        # Both reviewers flagged it — both appear in the flagged-by line
        # with their original severities.
        assert "claude-reviewer (blocker)" in text
        assert "codex-reviewer (blocker)" in text
        assert "**Synthesizer severity:** blocker" in text
        # Original per-reviewer descriptions show both verbatim
        assert "email column nullable; spec says required" in text
        assert "schema.sql line 14 — email not NULL-protected" in text

        # Dismissed nit section
        assert "### [nit] repo-wide: README still references gpt-4" in text
        assert "**File:** repo-wide" in text  # no backticks for null file
        assert "**Status:** Dismissed by synthesizer" in text
        assert "**Dismissal rationale:** spec explicitly defers doc updates" in text
        assert "**Severity change rationale:** downgraded from minor to nit" in text

    def test_verdict_is_ship_when_no_active_blocker(self, tmp_path: Path):
        """R2.8 renamed from ``test_verdict_is_ship_on_exit_zero``.
        Verdict label is derived from ``has_active_blocker(output)``,
        not from any caller-provided exit code; pinning that the
        empty-consolidation case renders SHIP."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        path = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT)
        text = path.read_text()
        assert "**Verdict:** SHIP" in text

    def test_empty_findings_renders_no_findings_message(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT).read_text()
        assert "No consolidated findings" in text
        # Synthesis summary still appears even with zero findings.
        assert "both reviewers verified the spec" in text

    def test_refuses_synth_failure_input(self, tmp_path: Path):
        from syncade.synthesizer import SynthesizerResult

        round_dir = _make_round_dir(tmp_path)
        failed = SynthesizerResult(
            output=None,
            error=RuntimeError("synth crashed"),
            duration_seconds=0.0,
            raw_subprocess_result=None,
        )
        with pytest.raises(ValueError, match="synth_result.output=None"):
            persist_findings_md(round_dir, failed, started_at=_FIXED_STARTED_AT)

    def test_provenance_descriptions_are_quoted(self, tmp_path: Path):
        """Provenance descriptions render with repr (quotes around them)
        so a description containing markdown special chars doesn't
        accidentally render as markdown."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT).read_text()
        # The bullet line for each provenance entry wraps the
        # description in quotes (repr style: 'text').
        assert "- claude-reviewer: 'email column nullable" in text
        assert "- codex-reviewer: 'schema.sql line 14" in text

    def test_findings_md_carries_generated_against_sha_header_when_kwarg_passed(
        self, tmp_path: Path
    ):
        """PR-15: when the orchestrator threads its snapshot SHA in,
        findings.md gains a ``**Generated against SHA:**`` line
        immediately after the verdict line. Short SHA is the first 12
        chars; full SHA is all 40 — operator can grep on either."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        sha = "abc123def456" + "0" * 28
        text = persist_findings_md(
            round_dir, synth, started_at=_FIXED_STARTED_AT, snapshot_sha=sha
        ).read_text()
        assert "**Generated against SHA:** `abc123def456` (full: `" + sha + "`)" in text
        # The new line lands between Verdict and Started — pinning the
        # position so a future refactor that moves it to the bottom of
        # the file fails this test.
        verdict_idx = text.find("**Verdict:**")
        sha_idx = text.find("**Generated against SHA:**")
        started_idx = text.find("**Started:**")
        assert verdict_idx != -1
        assert sha_idx != -1
        assert started_idx != -1
        assert verdict_idx < sha_idx < started_idx

    def test_findings_md_omits_sha_header_when_kwarg_not_passed(self, tmp_path: Path):
        """PR-15: back-compat. Every pre-PR-15 caller (and every test
        that doesn't pass ``snapshot_sha``) gets findings.md without
        the SHA header line. The default-None kwarg shape is what
        keeps ~20 existing call sites passing without fixture
        surgery."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT).read_text()
        assert "**Generated against SHA:**" not in text


class TestPersistCurrentFindingsMd:
    """PR-15: pin that the run-root convenience copy
    (``<run_dir>/findings.md``) carries the SHA header from the
    per-round source. The current copy mechanism is ``shutil.copy2``
    so this is trivially true today — the test exists to catch a
    future refactor that switches to a re-render path and silently
    drops the header."""

    def test_current_findings_md_carries_sha_via_copy(self, tmp_path: Path):
        from syncade.persistence import persist_current_findings_md

        round_dir = _make_round_dir(tmp_path)
        run_dir = round_dir.parent
        synth = _synth_result(output=_synth_output_empty())
        sha = "deadbeefcafe" + "0" * 28
        per_round_findings = persist_findings_md(
            round_dir, synth, started_at=_FIXED_STARTED_AT, snapshot_sha=sha
        )
        copied = persist_current_findings_md(run_dir, per_round_findings)
        assert copied is not None
        assert copied == run_dir / "findings.md"
        text = copied.read_text()
        assert "**Generated against SHA:** `deadbeefcafe` (full: `" + sha + "`)" in text


class TestFindingsMdPerReviewerSummaries:
    """PR-7.5 task 5: ``persist_findings_md`` always appends a
    ``## Per-reviewer summaries`` section when ``dispatch_result``
    is supplied — making findings.md self-sufficient (the Phase 04
    Acme run's no-findings findings.md was too thin)."""

    def _build_dispatch(self, summaries: list[str]) -> DispatchResult:
        """One ReviewerRunResult per summary, all SHIP."""
        results = []
        for i, s in enumerate(summaries):
            results.append(
                ReviewerRunResult(
                    reviewer_name=f"reviewer-{i}",
                    provider="anthropic" if i == 0 else "openai",
                    output=_ship_with_summary(s),
                    error=None,
                    duration_seconds=1.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            )
        return DispatchResult(results=results, total_duration_seconds=1.0)

    def test_no_findings_case_includes_per_reviewer_summaries(self, tmp_path: Path):
        """The Phase-04 Acme regression: a no-findings findings.md
        was three lines of synth summary + 'No consolidated findings'
        — thinner than summary.md. PR-7.5 makes it self-sufficient."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        dispatch = self._build_dispatch(
            [
                (
                    "I re-ran the full backend test suite (12 pass), "
                    "inspected schema.sql line-by-line, and confirmed "
                    "the new index against the spec's G3 clause."
                ),
                (
                    "I traced the migration's downgrade path against "
                    "staging Postgres and verified the SectorRotation "
                    "deletion is complete across the codebase."
                ),
            ]
        )
        text = persist_findings_md(round_dir, synth, _FIXED_STARTED_AT, None, dispatch).read_text()
        # The new section is present
        assert "## Per-reviewer summaries" in text
        # Both reviewers' summaries appear
        assert "I re-ran the full backend test suite" in text
        assert "I traced the migration's downgrade path" in text
        # Reviewer names + providers in the subheaders
        assert "### reviewer-0 (anthropic)" in text
        assert "### reviewer-1 (openai)" in text
        # The "No consolidated findings" message still appears
        # (the section above is unchanged in the no-findings case;
        # the per-reviewer summaries are additive context).
        assert "No consolidated findings" in text

    def test_findings_present_case_keeps_findings_above_summaries(self, tmp_path: Path):
        """When there ARE consolidated findings, the action items
        (the findings) must stay above the per-reviewer summaries
        — the operator's first read is what needs fixing."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        dispatch = self._build_dispatch(
            ["I verified everything end-to-end.", "I tested with a fresh DB."]
        )
        text = persist_findings_md(round_dir, synth, _FIXED_STARTED_AT, None, dispatch).read_text()
        findings_idx = text.find("## Findings")
        summaries_idx = text.find("## Per-reviewer summaries")
        assert findings_idx != -1
        assert summaries_idx != -1
        assert findings_idx < summaries_idx, (
            "Findings section must precede Per-reviewer summaries — "
            "the operator's first read is what needs fixing, not "
            "the reviewers' context narrative."
        )

    def test_summaries_section_omitted_when_dispatch_result_is_none(self, tmp_path: Path):
        """Pre-PR-7.5 back-compat: callers that don't pass
        ``dispatch_result`` (default None) get the old behavior —
        no summaries section."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(round_dir, synth, _FIXED_STARTED_AT, None, None).read_text()
        assert "## Per-reviewer summaries" not in text

    def test_summaries_section_skips_failed_reviewers(self, tmp_path: Path):
        """A failed reviewer has no ReviewerOutput.summary to
        render — the section omits that reviewer's block but
        keeps the successful reviewers."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        results = [
            ReviewerRunResult(
                reviewer_name="good-reviewer",
                provider="anthropic",
                output=_ship_with_summary("Good reviewer summary."),
                error=None,
                duration_seconds=1.0,
                raw_subprocess_result=_subprocess_result(),
            ),
            ReviewerRunResult(
                reviewer_name="bad-reviewer",
                provider="openai",
                output=None,
                error=ReviewerOutputError("parse failed"),
                duration_seconds=0.5,
                raw_subprocess_result=_subprocess_result(),
            ),
        ]
        dispatch = DispatchResult(results=results, total_duration_seconds=1.5)
        text = persist_findings_md(round_dir, synth, _FIXED_STARTED_AT, None, dispatch).read_text()
        # Section present, good-reviewer rendered, bad-reviewer omitted
        assert "## Per-reviewer summaries" in text
        assert "### good-reviewer (anthropic)" in text
        assert "### bad-reviewer" not in text
        # In production, this path is unreachable — the synth is
        # skipped on any reviewer failure, so findings.md isn't
        # written. The test exercises the defensive
        # "skip failed reviewer block" branch for the future
        # PR-8 round-N case where a prior round had failures but
        # round N succeeded.

    def test_summary_with_multiline_content_uses_block_form(self, tmp_path: Path):
        """The PR-6 fix #3 _format_summary_block helper renders
        multi-line / bulleted summaries in block form. Reuse the
        same helper here so findings.md and summary.md agree."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        multiline_summary = (
            "- Re-ran backend tests (12 pass)\n- Verified schema.sql\n- Confirmed G3 compliance"
        )
        dispatch = self._build_dispatch(["x"])  # placeholder; override below
        dispatch.results[0] = ReviewerRunResult(
            reviewer_name="rv1",
            provider="anthropic",
            output=_ship_with_summary(multiline_summary),
            error=None,
            duration_seconds=1.0,
            raw_subprocess_result=_subprocess_result(),
        )
        text = persist_findings_md(round_dir, synth, _FIXED_STARTED_AT, None, dispatch).read_text()
        # Multi-line bullet list is preserved verbatim — block form
        assert "- Re-ran backend tests" in text
        assert "- Verified schema.sql" in text


class TestFindingsMdConsensus:
    """PR-20 task 1: ``persist_findings_md`` renders an advisory
    per-finding consensus line — ``**Consensus:** N of M reviewers
    (unanimous)`` — derived at render time from each finding's
    ``provenance`` (N = distinct ``reviewer_name``s, who raised it) and
    the run's ``dispatch_result`` (M = reviewers in the run). Advisory
    only: consensus never reaches ``_compute_exit_code``. Omitted
    entirely when ``dispatch_result is None`` (byte-compatible with the
    pre-PR-20 layout)."""

    def _dispatch(self, count: int) -> DispatchResult:
        """A DispatchResult with ``count`` successful reviewers (= M)."""
        results = [
            ReviewerRunResult(
                reviewer_name=f"reviewer-{i}",
                provider="anthropic",
                output=_ship_with_summary(f"reviewer {i} verified the spec."),
                error=None,
                duration_seconds=1.0,
                raw_subprocess_result=_subprocess_result(),
            )
            for i in range(count)
        ]
        return DispatchResult(results=results, total_duration_seconds=float(count))

    def test_unanimous_and_partial_consensus_render(self, tmp_path: Path):
        """Two reviewers in the run (M=2). The unanimous blocker
        (raised by both → N=2) renders "2 of 2 reviewers (unanimous)";
        the single-reviewer nit (N=1) renders "1 of 2 reviewers" with
        NO unanimous tag."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, None, self._dispatch(2)
        ).read_text()
        assert "**Consensus:** 2 of 2 reviewers (unanimous)" in text
        assert "**Consensus:** 1 of 2 reviewers" in text

    def test_single_reviewer_run_renders_one_of_one(self, tmp_path: Path):
        """A single-reviewer run (M=1): consensus reads "1 of 1
        reviewer" — singular noun, never "(unanimous)" (unanimity is
        only meaningful with more than one reviewer)."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(
            output=SynthesizerOutput(
                consolidated_findings=[
                    ConsolidatedFinding(
                        description="solo finding from the only reviewer",
                        file="src/x.py",
                        severity="minor",
                        provenance=[
                            FindingProvenance(
                                reviewer_name="reviewer-0",
                                original_severity="minor",
                                original_index=0,
                                original_description="x is off by one",
                            )
                        ],
                        dismissed=False,
                    )
                ],
                synthesis_summary="one reviewer, one finding",
            )
        )
        text = persist_findings_md(
            round_dir, synth, _FIXED_STARTED_AT, None, self._dispatch(1)
        ).read_text()
        assert "**Consensus:** 1 of 1 reviewer" in text
        assert "(unanimous)" not in text

    def test_consensus_omitted_when_dispatch_result_none(self, tmp_path: Path):
        """Back-compat guard: with ``dispatch_result=None`` (the
        default), no consensus line renders — findings.md stays
        byte-identical to the pre-PR-20 layout. The finding body still
        renders (the **Flagged by:** line is unaffected)."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())
        text = persist_findings_md(round_dir, synth, _FIXED_STARTED_AT, None, None).read_text()
        assert "**Consensus:**" not in text
        assert "**Flagged by:**" in text
