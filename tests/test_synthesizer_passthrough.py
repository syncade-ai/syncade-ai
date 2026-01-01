"""Synthesizer pass-through (cannot-omit) regression tests — finding R.

The cannot-INVENT guard (``_validate_provenance_against_reviewers``) checks
that every emitted provenance entry traces to a real reviewer finding. Its
symmetric partner, ``_validate_reviewer_blockers_passed_through``, enforces
cannot-OMIT for blockers: PR-7's pass-through promise that a reviewer-surfaced
blocker cannot silently vanish from ``consolidated_findings``.

The reproduced defect: two reviewers each surface a blocker, the synth returns
``consolidated_findings=[]``, the schema accepts it (empty is a valid shape) and
the mechanical verdict returns exit 0 SHIP. This file pins that the cross-input
check now rejects it (-> exit 70), without false-positiving on legitimate empty
sets or on dropped advisory findings.
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
from syncade.synthesizer import _validate_reviewer_blockers_passed_through
from syncade.synthesizer.validation import _validate_duplicate_blockers_not_split_deactivated


def _reviewer(name: str, severities: list[str]) -> ReviewerRunResult:
    """A reviewer whose findings have the given severities (index = position)."""
    findings = [
        Finding(
            severity=sev,  # type: ignore[arg-type]
            file="src/foo.py",
            line=10 + i,
            spec_clause="must guard against None",
            finding=f"{name} finding {i}",
        )
        for i, sev in enumerate(severities)
    ]
    out = ReviewerOutput(
        verdict="NO-SHIP" if "blocker" in severities else "SHIP",
        summary="review",
        findings=findings,
        priority_order=list(range(len(findings))),
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


def _prov(name: str, index: int, severity: str) -> FindingProvenance:
    return FindingProvenance(
        reviewer_name=name,
        original_severity=severity,  # type: ignore[arg-type]
        original_index=index,
        original_description=f"{name} finding {index}",
    )


def _reviewer_with_blocker(name: str, *, file: str | None, finding: str) -> ReviewerRunResult:
    out = ReviewerOutput(
        verdict="NO-SHIP",
        summary="review",
        findings=[
            Finding(
                severity="blocker",
                file=file,
                spec_clause="must guard against None",
                finding=finding,
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


class TestReviewerBlockerPassThrough:
    def test_omitted_unanimous_blockers_rejected(self) -> None:
        """THE REPRO: two reviewers each a blocker, synth returns
        consolidated_findings=[] -> must raise (would otherwise exit 0 SHIP)."""
        reviewers = [
            _reviewer("claude-reviewer", ["blocker"]),
            _reviewer("codex-reviewer", ["blocker"]),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[],
            synthesis_summary="(empty — but the reviewers flagged blockers)",
        )
        with pytest.raises(SynthesizerOutputError) as exc_info:
            _validate_reviewer_blockers_passed_through(out, reviewers)
        msg = str(exc_info.value)
        assert "claude-reviewer#0" in msg
        assert "codex-reviewer#0" in msg

    def test_partial_omission_rejected_names_only_missing(self) -> None:
        """Synth keeps one reviewer's blocker but drops the other's ->
        raises, naming ONLY the dropped one."""
        reviewers = [
            _reviewer("claude-reviewer", ["blocker"]),
            _reviewer("codex-reviewer", ["blocker"]),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="kept claude's blocker only",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[_prov("claude-reviewer", 0, "blocker")],
                    dismissed=False,
                )
            ],
            synthesis_summary="dropped codex's blocker",
        )
        with pytest.raises(SynthesizerOutputError) as exc_info:
            _validate_reviewer_blockers_passed_through(out, reviewers)
        msg = str(exc_info.value)
        assert "codex-reviewer#0" in msg
        assert "claude-reviewer#0" not in msg

    def test_dismissed_blocker_is_valid_passthrough(self) -> None:
        """A single-reviewer blocker the synth dismisses-with-rationale is still
        PRESENT (referenced) -> no raise. Dismiss-with-rationale is a legitimate
        pass-through; the unanimous-blocker guard handles the rest."""
        reviewers = [
            _reviewer("claude-reviewer", ["blocker"]),
            _reviewer("codex-reviewer", ["minor"]),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="claude's blocker, dismissed",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[_prov("claude-reviewer", 0, "blocker")],
                    dismissed=True,
                    dismissal_rationale="false positive on closer read",
                )
            ],
            synthesis_summary="dismissed a single-reviewer blocker",
        )
        _validate_reviewer_blockers_passed_through(out, reviewers)


class TestDuplicateBlockerSplitGuard:
    def test_exact_duplicate_blockers_split_and_downgraded_are_rejected(self) -> None:
        """Defense-in-depth split repro: pass-through is satisfied, but two
        reviewers emitted the same blocker text for the same file and the synth
        split them into separate downgraded single-reviewer findings."""
        reviewers = [
            _reviewer_with_blocker(
                "claude-reviewer",
                file="src/foo.py",
                finding="missing null check",
            ),
            _reviewer_with_blocker(
                "codex-reviewer",
                file="src/foo.py",
                finding="  Missing   NULL check  ",
            ),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="claude blocker downgraded",
                    file="src/foo.py",
                    severity="minor",
                    provenance=[_prov("claude-reviewer", 0, "blocker")],
                    severity_change_rationale="synth claims lower blast radius",
                ),
                ConsolidatedFinding(
                    description="codex blocker downgraded",
                    file="src/foo.py",
                    severity="minor",
                    provenance=[_prov("codex-reviewer", 0, "blocker")],
                    severity_change_rationale="synth claims lower blast radius",
                ),
            ],
            synthesis_summary="split exact duplicate blockers",
        )
        _validate_reviewer_blockers_passed_through(out, reviewers)
        with pytest.raises(SynthesizerOutputError) as exc_info:
            _validate_duplicate_blockers_not_split_deactivated(out, reviewers)
        msg = str(exc_info.value)
        assert "split/deactivated duplicate reviewer blocker" in msg
        assert "claude-reviewer#0" in msg
        assert "codex-reviewer#0" in msg

    def test_exact_duplicate_blockers_merged_active_pass(self) -> None:
        reviewers = [
            _reviewer_with_blocker(
                "claude-reviewer",
                file="src/foo.py",
                finding="missing null check",
            ),
            _reviewer_with_blocker(
                "codex-reviewer",
                file="src/foo.py",
                finding="missing null check",
            ),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="merged duplicate blocker",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[
                        _prov("claude-reviewer", 0, "blocker"),
                        _prov("codex-reviewer", 0, "blocker"),
                    ],
                    dismissed=False,
                )
            ],
            synthesis_summary="merged exact duplicate blockers",
        )
        _validate_duplicate_blockers_not_split_deactivated(out, reviewers)

    def test_same_text_different_files_not_treated_as_duplicate(self) -> None:
        reviewers = [
            _reviewer_with_blocker(
                "claude-reviewer",
                file="src/foo.py",
                finding="missing null check",
            ),
            _reviewer_with_blocker(
                "codex-reviewer",
                file="src/bar.py",
                finding="missing null check",
            ),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="foo issue downgraded",
                    file="src/foo.py",
                    severity="minor",
                    provenance=[_prov("claude-reviewer", 0, "blocker")],
                    severity_change_rationale="different file",
                ),
                ConsolidatedFinding(
                    description="bar issue downgraded",
                    file="src/bar.py",
                    severity="minor",
                    provenance=[_prov("codex-reviewer", 0, "blocker")],
                    severity_change_rationale="different file",
                ),
            ],
            synthesis_summary="same words, different files",
        )
        _validate_duplicate_blockers_not_split_deactivated(out, reviewers)

    def test_all_blockers_active_passes(self) -> None:
        """Both reviewers' blockers merged into one active consolidated finding
        with both provenance entries -> no raise."""
        reviewers = [
            _reviewer("claude-reviewer", ["blocker"]),
            _reviewer("codex-reviewer", ["blocker"]),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="merged unanimous blocker",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[
                        _prov("claude-reviewer", 0, "blocker"),
                        _prov("codex-reviewer", 0, "blocker"),
                    ],
                    dismissed=False,
                )
            ],
            synthesis_summary="one merged blocker, both reviewers",
        )
        _validate_reviewer_blockers_passed_through(out, reviewers)

    def test_dropped_advisory_findings_not_enforced(self) -> None:
        """SCOPE: pass-through is enforced for BLOCKERS only. Reviewers with
        only minor/nit findings + an empty consolidated set must NOT raise — an
        omitted advisory can't cause a false ship (verdict reads only blockers)."""
        reviewers = [
            _reviewer("claude-reviewer", ["minor", "nit"]),
            _reviewer("codex-reviewer", ["minor"]),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[],
            synthesis_summary="dropped only advisory findings",
        )
        _validate_reviewer_blockers_passed_through(out, reviewers)

    def test_legitimate_empty_passes(self) -> None:
        """Reviewers genuinely found nothing -> empty consolidated set is
        legitimate, no raise."""
        reviewers = [
            _reviewer("claude-reviewer", []),
            _reviewer("codex-reviewer", []),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[],
            synthesis_summary="clean — no findings",
        )
        _validate_reviewer_blockers_passed_through(out, reviewers)

    def test_failed_reviewer_skipped(self) -> None:
        """A failed reviewer (output=None) contributes no required blockers."""
        reviewers = [
            _reviewer("claude-reviewer", ["blocker"]),
            ReviewerRunResult(
                reviewer_name="codex-reviewer",
                provider="openai",
                output=None,
                error=RuntimeError("boom"),
                duration_seconds=1.0,
            ),
        ]
        out = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="claude's blocker",
                    file="src/foo.py",
                    severity="blocker",
                    provenance=[_prov("claude-reviewer", 0, "blocker")],
                    dismissed=False,
                )
            ],
            synthesis_summary="one reviewer failed, the other's blocker kept",
        )
        _validate_reviewer_blockers_passed_through(out, reviewers)
