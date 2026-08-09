"""Synthesizer provenance cross-check regression tests.

Split out of ``tests/test_synthesizer.py`` to keep that file under the
LOC gate. Covers ``_validate_provenance_against_reviewers``' severity
cross-check — the guard that stops a synth from misreporting a
reviewer's severity to evade the unanimous-blocker deactivation rule.
"""

from __future__ import annotations

import pytest

from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.synthesis import (
    ConsolidatedFinding,
    FindingProvenance,
    SynthesizerOutput,
    SynthesizerOutputError,
)
from syncade.synthesizer import _validate_provenance_against_reviewers


class TestProvenanceSeverityValidation:
    """The unanimous-blocker deactivation guard keys on ``original_severity``,
    so a synth could slip a genuinely-unanimous blocker past it by recording
    ONE reviewer's severity as ``"minor"`` (a valid enum, real reviewer,
    in-range index) — ``all(== "blocker")`` then fails and the schema lets
    the dismissal through. ``_validate_provenance_against_reviewers`` closes
    this by cross-checking each entry's ``original_severity`` against the
    source reviewer's actual finding severity.
    """

    def _reviewer_results(self) -> list[ReviewerRunResult]:
        # Two reviewers, each with ONE blocker finding at index 0 — a
        # genuinely-unanimous blocker.
        def mk(name: str) -> ReviewerRunResult:
            out = ReviewerOutput(
                verdict="NO-SHIP",
                summary="bug",
                findings=[
                    Finding(
                        severity="blocker",
                        file="src/foo.py",
                        line=10,
                        spec_clause="must guard against None",
                        finding="missing null check",
                    )
                ],
                priority_order=[0],
                coverage_gaps=[],
                dismissed_concerns=[],
            )
            return ReviewerRunResult(
                reviewer_name=name,
                provider="anthropic",
                output=out,
                error=None,
                duration_seconds=1.0,
            )

        return [mk("claude-reviewer"), mk("codex-reviewer")]

    def _dismissed_output(self, codex_severity: str) -> SynthesizerOutput:
        return SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="genuinely unanimous blocker",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="claude-reviewer",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing null check",
                        ),
                        FindingProvenance(
                            reviewer_name="codex-reviewer",
                            original_severity=codex_severity,  # type: ignore[arg-type]
                            original_index=0,
                            original_description="missing null check",
                        ),
                    ],
                    dismissed=True,
                    dismissal_rationale="both reviewers misread; dismissing",
                )
            ],
            synthesis_summary="dismissed a finding two reviewers flagged",
        )

    def test_false_minor_severity_bypass_rejected(self) -> None:
        # The schema ACCEPTS this output (all(== blocker) is False because
        # codex's severity is recorded as "minor", so the unanimous-blocker
        # guard does not fire) — but the cross-check must reject it because
        # codex's ACTUAL finding severity is blocker.
        out = self._dismissed_output(codex_severity="minor")
        with pytest.raises(SynthesizerOutputError) as exc_info:
            _validate_provenance_against_reviewers(out, self._reviewer_results())
        assert "original_severity" in str(exc_info.value)
        assert "codex-reviewer" in str(exc_info.value)

    def test_truthful_severity_passes(self) -> None:
        # When codex's recorded severity matches its actual blocker finding,
        # the cross-check must NOT raise (no false positive). The schema
        # itself would have refused the dismissal here; the point of this
        # test is only that the severity cross-check passes truthful input.
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="genuinely unanimous blocker",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="claude-reviewer",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing null check",
                        ),
                        FindingProvenance(
                            reviewer_name="codex-reviewer",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing null check",
                        ),
                    ],
                    dismissed=False,
                )
            ],
            synthesis_summary="two reviewers flagged the same blocker",
        )
        _validate_provenance_against_reviewers(out, self._reviewer_results())


class TestProvenanceQuoteValidation:
    """PR-h-01 increment C. ``original_description`` renders in findings.md as
    the reviewer's verbatim framing, so a synthesizer able to author it can
    restate a broad blocker as something narrow, dismiss the restatement with a
    rationale that is true *of the restatement*, and leave the operator reading
    a fabricated quote attributed to a reviewer who never wrote it.
    """

    _SOURCE = "auth query interpolates the username without parameterization"

    def _reviewer_results(self) -> list[ReviewerRunResult]:
        out = ReviewerOutput(
            verdict="NO-SHIP",
            summary="bug",
            findings=[
                Finding(
                    severity="blocker",
                    file="src/auth.py",
                    line=10,
                    spec_clause="§3.1",
                    finding=self._SOURCE,
                )
            ],
            priority_order=[0],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        return [
            ReviewerRunResult(
                reviewer_name="rv1",
                provider="openai",
                output=out,
                error=None,
                duration_seconds=1.0,
            )
        ]

    def _output(self, quote: str) -> SynthesizerOutput:
        return SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="sql injection in auth",
                    file="src/auth.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="rv1",
                            original_severity="blocker",
                            original_index=0,
                            original_description=quote,
                        )
                    ],
                )
            ],
            synthesis_summary="consolidated",
        )

    def test_verbatim_quote_passes(self) -> None:
        _validate_provenance_against_reviewers(self._output(self._SOURCE), self._reviewer_results())

    def test_reflowed_quote_passes(self) -> None:
        """Line-wrapping and indentation are not rewrites — a model that
        reflows a long quote must not burn the run."""
        reflowed = "auth query interpolates\n   the username    without\nparameterization"
        _validate_provenance_against_reviewers(self._output(reflowed), self._reviewer_results())

    @pytest.mark.parametrize(
        "quote",
        [
            "auth query has a minor style issue",  # wholesale rewrite
            "auth query interpolates the username",  # truncated — drops the point
            "Auth query interpolates the username without parameterization",  # case
            "auth query interpolates the user name without parameterization",  # one word
        ],
        ids=["rewritten", "truncated", "recased", "reworded"],
    )
    def test_any_rewrite_is_REPAIRED_not_rendered(self, quote: str) -> None:
        """INVERTED by PR-h-field-01 item 5 — and PR-h-01's guarantee got STRONGER, not weaker.

        The property increment C bought was: *the operator never reads a fabricated quote
        attributed to a reviewer who never wrote it.* Aborting delivered that by refusing
        the whole run. Repair delivers it by construction — `original_description` is
        OVERWRITTEN with the reviewer's own text, so no string a synthesizer authors can
        reach findings.md at all. Before, the rendered quote was verbatim only because the
        check happened to pass; now it is verbatim because it was copied from the source.

        What abort bought and repair must not lose is the SIGNAL that the model rewrote a
        source. That is why every repair is recorded with both strings, and asserted here.

        The abort's cost was measured: one dropped backtick in a finding about
        `style={{ backdropFilter: ... }}` ended a run at exit 70 after 713 seconds of
        reviewer wall-clock, discarding six valid findings and three unrun rounds. Nothing
        distinguishes that from a deliberate rewrite at validation time, and it does not
        need to — the ground truth is in `reviewer_results` either way.
        """
        output = self._output(quote)
        repairs = _validate_provenance_against_reviewers(output, self._reviewer_results())

        # The fabrication never reaches the rendered artifact.
        rendered = output.consolidated_findings[0].provenance[0].original_description
        assert rendered == self._SOURCE, "a synthesizer-authored quote survived into the output"

        # ...and the operator can still see that it happened, with both strings.
        assert len(repairs) == 1, "the rewrite was corrected silently — the signal is lost"
        assert repairs[0].reviewer_text == self._SOURCE
        assert repairs[0].synthesizer_text == quote
        assert repairs[0].reviewer_name == "rv1"
        assert repairs[0].original_index == 0
