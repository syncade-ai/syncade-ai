"""Tests for :mod:`syncade.persistence` — ``persist_run_summary`` (part 2 of 2).

Moved verbatim from the former ``tests/test_persistence.py``.
"""

from __future__ import annotations

from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutput, ReviewerOutputError
from syncade.persistence import persist_run_summary
from syncade.process import SubprocessTimeoutError
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _snapshot,
    _subprocess_result,
)


class TestPersistRunSummary:
    def _single_reviewer_dispatch(
        self,
        output: ReviewerOutput,
        *,
        reviewer_name: str = "claude-reviewer",
        provider: str = "anthropic",
        duration_seconds: float = 1.0,
    ) -> DispatchResult:
        """Tiny helper: a DispatchResult with one successful reviewer.
        Used by the PR-6 rendering tests so each one stays focused on
        the rendered-content assertion, not boilerplate construction."""
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name=reviewer_name,
                    provider=provider,
                    output=output,
                    error=None,
                    duration_seconds=duration_seconds,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=duration_seconds,
        )

    def test_summary_md_has_no_trailing_header_whitespace(self, tmp_path: Path):
        """Fix #3: non-empty Coverage gaps / Dismissed concerns headers
        and the Summary block header must have NO trailing whitespace.

        The pre-fix renderer produced lines like `'**Coverage gaps:** '`
        (trailing space, then newline) which is harmless markdown but
        ugly in raw view. The ONLY lines allowed to end in trailing
        whitespace are the intentional two-space hard-break lines in
        the metadata block: ``**Started:**``, ``**Exit code:**``, and
        ``**Repo:**``."""
        round_dir = _make_round_dir(tmp_path)
        rich_output = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="I verified the trivial diff and ran pytest.",
            priority_order=[],
            coverage_gaps=["did not exercise the staging Postgres"],
            dismissed_concerns=["considered: README not updated, exempt"],
        )
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._single_reviewer_dispatch(rich_output),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
        ).read_text()

        # Markdown's "two trailing spaces == hard line break" idiom is
        # used DELIBERATELY on the metadata block so Started / Exit
        # code / Repo render on separate lines in a renderer. Those
        # are the only lines allowed trailing whitespace.
        intentional_hardbreak_prefixes = (
            "**Started:**",
            "**Exit code:**",
            "**Repo:**",
        )
        bad_lines = []
        for i, line in enumerate(text.splitlines(), 1):
            if not line or line == line.rstrip():
                continue
            # Trailing whitespace exists. Is it intentional?
            if any(line.lstrip().startswith(p) for p in intentional_hardbreak_prefixes):
                continue
            bad_lines.append((i, line))
        assert not bad_lines, f"unexpected trailing whitespace on lines: {bad_lines!r}"

        # And the specific non-empty PR-6 blocks have bare headers
        # (no inline content, no trailing space):
        assert "**Coverage gaps:**\n" in text or text.endswith("**Coverage gaps:**")
        assert "**Dismissed concerns:**\n" in text or text.endswith("**Dismissed concerns:**")

    def test_summary_md_empty_lists_still_render_inline_with_none(self, tmp_path: Path):
        """Fix #3 corollary: an empty list still renders as
        ``**Coverage gaps:** None.`` on one line — the no-trailing-
        whitespace fix is only about the non-empty case; the empty
        case was already clean."""
        round_dir = _make_round_dir(tmp_path)
        empty_output = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="verified everything the spec asked for",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._single_reviewer_dispatch(empty_output),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
        ).read_text()
        assert "**Coverage gaps:** None." in text
        assert "**Dismissed concerns:** None." in text
        # And neither header carries trailing whitespace.
        for line in text.splitlines():
            if line.startswith(("**Coverage gaps:**", "**Dismissed concerns:**")):
                assert line == line.rstrip(), f"trailing whitespace on: {line!r}"

    def test_summary_md_renders_bulleted_summary_as_block(self, tmp_path: Path):
        """Fix #4: a ``summary`` starting with a markdown list marker
        renders as a block (header on its own line, blank line, then
        the bullets) rather than inline. Inline would produce
        ``**Summary:** - ran pytest`` where the ``-`` reads as inline
        text instead of a list bullet — malformed."""
        round_dir = _make_round_dir(tmp_path)
        bulleted_output = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="- ran pytest (47/47)\n- inspected persistence.py\n- confirmed db schema",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._single_reviewer_dispatch(bulleted_output),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
        ).read_text()

        # Block form: header on its own line, then a blank line, then
        # the bullets. Inline form would have `**Summary:** - ran pytest`
        # on a single line, which is what we're avoiding.
        assert "**Summary:** -" not in text, (
            "bulleted summary rendered inline — `-` reads as text, not a list bullet"
        )
        # Exact block-form structure: header line + blank + first bullet.
        assert "**Summary:**\n\n- ran pytest" in text

    def test_summary_md_renders_multiline_summary_as_block(self, tmp_path: Path):
        """Fix #4: a ``summary`` containing newlines (not starting with
        a list marker) also renders as a block — inline form would
        leave the second-and-onward lines outside the bold marker's
        visual scope and look detached."""
        round_dir = _make_round_dir(tmp_path)
        multiline_output = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="Verified the following:\n- ran pytest\n- inspected db schema",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._single_reviewer_dispatch(multiline_output),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
        ).read_text()

        # Block form: header on its own line, blank line, then content.
        assert "**Summary:**\n\nVerified the following:\n- ran pytest" in text
        # Inline form would put "Verified the following:" on the same
        # line as "**Summary:**" — explicitly NOT what we want.
        assert "**Summary:** Verified" not in text

    def test_summary_md_one_line_summary_still_inline(self, tmp_path: Path):
        """Fix #4 corollary: a one-line summary (no newlines, no
        list-marker start) still renders inline. The block form was
        added as a targeted fix for multi-line / bulleted content,
        not a blanket change."""
        round_dir = _make_round_dir(tmp_path)
        oneliner_output = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="I verified the trivial diff against the spec.",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        text = persist_run_summary(
            round_dir,
            _snapshot(),
            self._single_reviewer_dispatch(oneliner_output),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
        ).read_text()
        # Inline form — header and content on one line.
        assert "**Summary:** I verified the trivial diff against the spec." in text

    def test_summary_renders_pr6_narrative_blocks_for_success(self, tmp_path: Path):
        """PR-6 task 4: each successful reviewer's section in summary.md
        includes the reviewer's ``summary``, ``coverage_gaps``, and
        ``dismissed_concerns`` as readable prose. This is the
        user-visible win for PR-6 even before PR-7's synthesizer lands
        — reading summary.md tells you what each reviewer actually
        checked, not just `Verdict: SHIP, Findings: 0`."""
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        rich_output = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary=(
                "I verified the MoneyMovement widget against mockup-v2: "
                "MOCK_DATA shape, color tokens, component structure. "
                "Ran the frontend test suite (279 tests pass). Confirmed "
                "the SectorRotation deletion. Verified no console errors "
                "or stale network requests via playwright."
            ),
            priority_order=[],
            coverage_gaps=[
                "could not reach the staging Postgres from the worktree",
                "did not run the e2e suite — only the unit suite",
            ],
            dismissed_concerns=[
                "considered: SectorRotationData still in types/index.ts. "
                "The spec exempts types files explicitly.",
            ],
        )
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="claude-reviewer",
                    provider="anthropic",
                    output=rich_output,
                    error=None,
                    duration_seconds=588.6,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=588.6,
        )
        text = persist_run_summary(
            round_dir, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
        ).read_text()

        # Summary block: the headline narrative, verbatim.
        assert "**Summary:** I verified the MoneyMovement widget" in text
        # Coverage gaps: bulleted list with the reviewer's two strings.
        assert "**Coverage gaps:**" in text
        assert "- could not reach the staging Postgres from the worktree" in text
        assert "- did not run the e2e suite — only the unit suite" in text
        # Dismissed concerns: bulleted list with the one string.
        assert "**Dismissed concerns:**" in text
        assert "- considered: SectorRotationData still in types/index.ts" in text

    def test_summary_renders_empty_lists_as_none_for_clean_reading(self, tmp_path: Path):
        """PR-6 task 4: empty ``coverage_gaps`` and ``dismissed_concerns``
        render as ``None.`` for cleaner reading rather than an empty
        bullet — the brief specifies this verbatim."""
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        clean_output = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="verified everything the spec asked for",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="claude-reviewer",
                    provider="anthropic",
                    output=clean_output,
                    error=None,
                    duration_seconds=10.0,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=10.0,
        )
        text = persist_run_summary(
            round_dir, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
        ).read_text()
        # Both empty lists render as `None.`, not as an empty bulleted
        # list with a stray `-`.
        assert "**Coverage gaps:** None." in text
        assert "**Dismissed concerns:** None." in text
        # And the summary still appears.
        assert "**Summary:** verified everything the spec asked for" in text

    def test_summary_omits_pr6_blocks_for_failed_reviewer(self, tmp_path: Path):
        """Per the PR-6 brief: for failed reviewers
        (``output is None``), the Summary / Coverage gaps / Dismissed
        concerns sections are omitted entirely — the reviewer didn't
        produce a structured output to render. The failure section
        keeps its existing Outcome / Duration / Error / Output shape."""
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="codex-reviewer",
                    provider="openai",
                    output=None,
                    error=ReviewerOutputError("garbage in stdout"),
                    duration_seconds=4.2,
                    raw_subprocess_result=_subprocess_result(),
                )
            ],
            total_duration_seconds=4.2,
        )
        text = persist_run_summary(
            round_dir, snap, dispatch, exit_code=70, started_at=_FIXED_STARTED_AT
        ).read_text()
        # The failure entry's structural lines stay intact.
        assert "**Outcome:** failure" in text
        assert "**Error:** ReviewerOutputError" in text
        # No PR-6 narrative blocks — accessing the missing fields
        # would crash the renderer; this test pins that we don't try.
        assert "**Summary:**" not in text
        assert "**Coverage gaps:**" not in text
        assert "**Dismissed concerns:**" not in text

    def test_summary_mixed_outcomes_renders_pr6_blocks_only_for_successes(self, tmp_path: Path):
        """One success + one failure in the same run. Only the success
        gets the PR-6 narrative blocks. The failure's section is the
        old Outcome/Duration/Error/Output shape."""
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        success = ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="claude SHIP narrative",
            priority_order=[],
            coverage_gaps=["one gap claude flagged"],
            dismissed_concerns=[],
        )
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="claude-reviewer",
                    provider="anthropic",
                    output=success,
                    error=None,
                    duration_seconds=12.0,
                    raw_subprocess_result=_subprocess_result(),
                ),
                ReviewerRunResult(
                    reviewer_name="codex-reviewer",
                    provider="openai",
                    output=None,
                    error=SubprocessTimeoutError("timed out", stdout="", stderr="", timeout=600.0),
                    duration_seconds=600.1,
                    raw_subprocess_result=_subprocess_result(rc=-1),
                ),
            ],
            total_duration_seconds=600.1,
        )
        text = persist_run_summary(
            round_dir, snap, dispatch, exit_code=40, started_at=_FIXED_STARTED_AT
        ).read_text()

        # Successful reviewer's PR-6 blocks present.
        assert "**Summary:** claude SHIP narrative" in text
        assert "- one gap claude flagged" in text
        # Failed reviewer's section: error class, no narrative blocks.
        # Find the codex section specifically and check it.
        codex_section = text.split("### codex-reviewer")[1]
        # codex section must NOT have the narrative blocks (they belong
        # to the claude success above).
        assert "**Summary:**" not in codex_section
        assert "**Coverage gaps:**" not in codex_section
        assert "**Dismissed concerns:**" not in codex_section
        assert "**Error:** SubprocessTimeoutError" in codex_section
