"""Tests for :mod:`syncade.synthesis` (PR-7 task 2 — synthesis primitives).

Model + validator tests: FindingProvenance, ConsolidatedFinding, the
dismissal / unanimous-blocker / severity-change validators, and
SynthesizerOutput itself (PR-R3 split from test_synthesis.py).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncade.synthesis import (
    ConsolidatedFinding,
    FindingProvenance,
    SynthesizerOutput,
)
from tests.synthesis._helpers import _finding, _provenance

# ---------------------------------------------------------------------------
# FindingProvenance
# ---------------------------------------------------------------------------


class TestFindingProvenance:
    def test_valid_minimal(self) -> None:
        p = FindingProvenance(**_provenance())
        assert p.reviewer_name == "claude-reviewer"
        assert p.original_severity == "blocker"
        assert p.original_index == 0

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_rejects_whitespace_only_reviewer_name(self, blank: str) -> None:
        with pytest.raises(ValidationError):
            FindingProvenance(**_provenance(reviewer_name=blank))

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_rejects_whitespace_only_original_description(self, blank: str) -> None:
        with pytest.raises(ValidationError):
            FindingProvenance(**_provenance(original_description=blank))

    def test_rejects_negative_index(self) -> None:
        with pytest.raises(ValidationError):
            FindingProvenance(**_provenance(original_index=-1))

    def test_rejects_invalid_severity(self) -> None:
        with pytest.raises(ValidationError):
            FindingProvenance(**_provenance(original_severity="critical"))

    def test_rejects_extra_field(self) -> None:
        payload = _provenance()
        payload["surprise"] = "extra"
        with pytest.raises(ValidationError):
            FindingProvenance(**payload)


# ---------------------------------------------------------------------------
# ConsolidatedFinding — pass-through (single reviewer) + base validation
# ---------------------------------------------------------------------------


class TestConsolidatedFindingBase:
    def test_valid_single_reviewer(self) -> None:
        cf = ConsolidatedFinding(**_finding())
        assert cf.description.startswith("user.email")
        assert cf.dismissed is False
        assert cf.dismissal_rationale is None
        assert len(cf.provenance) == 1

    def test_file_may_be_none(self) -> None:
        cf = ConsolidatedFinding(**_finding(file=None))
        assert cf.file is None

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_rejects_whitespace_only_description(self, blank: str) -> None:
        with pytest.raises(ValidationError):
            ConsolidatedFinding(**_finding(description=blank))

    def test_rejects_empty_provenance(self) -> None:
        # "Cannot invent findings" rule — schema requires non-empty
        # provenance. Empty list is the synthesizer inventing a finding
        # the reviewers did not surface.
        with pytest.raises(ValidationError):
            ConsolidatedFinding(**_finding(provenance=[]))

    def test_rejects_extra_field(self) -> None:
        payload = _finding()
        payload["pinned"] = True
        with pytest.raises(ValidationError):
            ConsolidatedFinding(**payload)


# ---------------------------------------------------------------------------
# Validator 1: dismissal rationale required when dismissed
# ---------------------------------------------------------------------------


class TestDismissalRationaleRequired:
    def test_dismissed_without_rationale_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="minor",
                    provenance=[_provenance(original_severity="minor")],
                    dismissed=True,
                    dismissal_rationale=None,
                )
            )
        assert "dismissal_rationale" in str(exc_info.value)

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_dismissed_with_whitespace_rationale_rejected(self, blank: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="minor",
                    provenance=[_provenance(original_severity="minor")],
                    dismissed=True,
                    dismissal_rationale=blank,
                )
            )
        assert "dismissal_rationale" in str(exc_info.value)

    def test_dismissed_with_real_rationale_accepted(self) -> None:
        cf = ConsolidatedFinding(
            **_finding(
                severity="minor",
                provenance=[_provenance(original_severity="minor")],
                dismissed=True,
                dismissal_rationale="spec exempts types/ files explicitly",
            )
        )
        assert cf.dismissed is True
        assert cf.dismissal_rationale == "spec exempts types/ files explicitly"

    def test_not_dismissed_rationale_optional(self) -> None:
        # dismissed=False, dismissal_rationale=None should pass.
        cf = ConsolidatedFinding(
            **_finding(
                severity="minor",
                provenance=[_provenance(original_severity="minor")],
                dismissed=False,
                dismissal_rationale=None,
            )
        )
        assert cf.dismissed is False


# ---------------------------------------------------------------------------
# Validator 2: cannot deactivate unanimous blockers
# ---------------------------------------------------------------------------


class TestCannotDeactivateUnanimousBlocker:
    def test_two_reviewers_both_blocker_dismissed_rejected(self) -> None:
        # Hard schema-level guardrail: two reviewers at blocker cannot
        # be dismissed regardless of rationale quality.
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="blocker",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer"),
                        _provenance(reviewer_name="codex-reviewer"),
                    ],
                    dismissed=True,
                    dismissal_rationale="I think both reviewers misread the spec here",
                )
            )
        message = str(exc_info.value)
        assert "unanimous-blocker" in message
        assert "claude-reviewer" in message
        assert "codex-reviewer" in message

    def test_three_reviewers_all_blocker_dismissed_rejected(self) -> None:
        # Same rule scales — N>=2 reviewers at blocker is dismissal-
        # forbidden.
        with pytest.raises(ValidationError):
            ConsolidatedFinding(
                **_finding(
                    severity="blocker",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer"),
                        _provenance(reviewer_name="codex-reviewer"),
                        _provenance(reviewer_name="gemini-reviewer"),
                    ],
                    dismissed=True,
                    dismissal_rationale="all three reviewers were wrong",
                )
            )

    def test_two_reviewers_mixed_severity_dismissed_allowed(self) -> None:
        # Only one reviewer at blocker; the other at minor. Allowed
        # to dismiss with rationale — the unanimous-blocker rule
        # requires UNANIMOUS blocker.
        cf = ConsolidatedFinding(
            **_finding(
                description="redundant comment in service.py",
                severity="nit",
                provenance=[
                    _provenance(
                        reviewer_name="claude-reviewer",
                        original_severity="blocker",
                    ),
                    _provenance(
                        reviewer_name="codex-reviewer",
                        original_severity="minor",
                    ),
                ],
                dismissed=True,
                dismissal_rationale=(
                    "the comment claude flagged as blocker was a "
                    "TODO marker; spec doesn't forbid TODOs"
                ),
                severity_change_rationale=(
                    "downgraded from both reviewers' calls — true severity "
                    "is nit (a comment style issue), not blocker or minor"
                ),
            )
        )
        assert cf.dismissed is True

    def test_one_reviewer_blocker_dismissed_allowed(self) -> None:
        # Single-reviewer blocker can be dismissed — only one
        # reviewer flagged it. The unanimous-blocker rule kicks in at
        # N>=2.
        cf = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[
                    _provenance(reviewer_name="claude-reviewer"),
                ],
                dismissed=True,
                dismissal_rationale=(
                    "claude flagged the missing nullability but the spec "
                    "explicitly defines email as nullable in §3.2"
                ),
            )
        )
        assert cf.dismissed is True

    def test_two_reviewers_both_blocker_not_dismissed_allowed(self) -> None:
        # The guardrail only triggers on dismissed=True. Non-dismissed
        # unanimous blockers are the happy path.
        cf = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[
                    _provenance(reviewer_name="claude-reviewer"),
                    _provenance(reviewer_name="codex-reviewer"),
                ],
                dismissed=False,
            )
        )
        assert cf.dismissed is False
        assert len(cf.provenance) == 2

    def test_unanimous_blocker_check_fires_before_dismissal_rationale_check(self) -> None:
        """QA fix #13 (P1.8): when BOTH rules would reject an input
        (trying to dismiss a unanimous blocker WITHOUT rationale),
        the unanimous-blocker error fires FIRST. Without this order,
        the operator hits "rationale required", adds rationale,
        re-submits, only to hit the unanimous-blocker rule on the
        second pass — wasted effort. The structural-impossibility
        check should always lead.

        Inputs: dismissed=True, NO dismissal_rationale, two
        reviewers both flagged at blocker. Both validators apply;
        the unanimous-blocker one must be the one that surfaces.
        """
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="blocker",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer"),
                        _provenance(reviewer_name="codex-reviewer"),
                    ],
                    dismissed=True,
                    dismissal_rationale=None,  # rule 1 would also fire
                )
            )
        message = str(exc_info.value)
        # The unanimous-blocker error message names the rule and
        # the provenance count.
        assert "unanimous-blocker" in message
        # The dismissal_rationale error should NOT have fired (it'd
        # win the race in the wrong order). Pin that the operator
        # gets the structural error, not the rationale error.
        assert "dismissal_rationale is required" not in message


# ---------------------------------------------------------------------------
# Validator 3: severity-change rationale required when synth overrode all
# ---------------------------------------------------------------------------


class TestSeverityChangeRationaleRequired:
    def test_severity_differs_from_all_reviewers_no_rationale_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="nit",
                    provenance=[
                        _provenance(
                            reviewer_name="claude-reviewer",
                            original_severity="blocker",
                        ),
                        _provenance(
                            reviewer_name="codex-reviewer",
                            original_severity="minor",
                        ),
                    ],
                )
            )
        assert "severity_change_rationale" in str(exc_info.value)

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_severity_differs_from_all_whitespace_rationale_rejected(self, blank: str) -> None:
        with pytest.raises(ValidationError):
            ConsolidatedFinding(
                **_finding(
                    severity="nit",
                    provenance=[
                        _provenance(
                            reviewer_name="claude-reviewer",
                            original_severity="blocker",
                        ),
                    ],
                    severity_change_rationale=blank,
                )
            )

    def test_severity_differs_from_all_with_rationale_accepted(self) -> None:
        cf = ConsolidatedFinding(
            **_finding(
                severity="nit",
                provenance=[
                    _provenance(
                        reviewer_name="claude-reviewer",
                        original_severity="blocker",
                    ),
                    _provenance(
                        reviewer_name="codex-reviewer",
                        original_severity="minor",
                    ),
                ],
                severity_change_rationale=(
                    "both reviewers over-weighted; the affected code path is only hit in dev mode"
                ),
            )
        )
        assert cf.severity == "nit"

    def test_severity_matches_one_reviewer_no_rationale_needed(self) -> None:
        # Synthesizer's final severity matches at least one reviewer's
        # — that's disagreement-arbitration, not independent judgment.
        # No rationale required.
        cf = ConsolidatedFinding(
            **_finding(
                severity="minor",
                provenance=[
                    _provenance(
                        reviewer_name="claude-reviewer",
                        original_severity="blocker",
                    ),
                    _provenance(
                        reviewer_name="codex-reviewer",
                        original_severity="minor",
                    ),
                ],
            )
        )
        assert cf.severity == "minor"
        assert cf.severity_change_rationale is None

    def test_severity_matches_only_reviewer_no_rationale_needed(self) -> None:
        # Single-reviewer pass-through: severity matches that reviewer,
        # no override happened, no rationale needed.
        cf = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[_provenance(original_severity="blocker")],
            )
        )
        assert cf.severity_change_rationale is None


# ---------------------------------------------------------------------------
# SynthesizerOutput
# ---------------------------------------------------------------------------


class TestSynthesizerOutput:
    def test_valid_with_one_finding(self) -> None:
        out = SynthesizerOutput(
            consolidated_findings=[ConsolidatedFinding(**_finding())],
            synthesis_summary=(
                "Both reviewers flagged the missing NOT NULL constraint; merged into one blocker."
            ),
        )
        assert len(out.consolidated_findings) == 1

    def test_valid_empty_findings_with_summary(self) -> None:
        # Brief explicitly calls this out: empty consolidated_findings
        # plus a non-empty synthesis_summary is valid (both reviewers
        # surfaced nothing; the synthesizer narrates that).
        out = SynthesizerOutput(
            consolidated_findings=[],
            synthesis_summary=(
                "Both reviewers verified the spec end-to-end with no findings to consolidate."
            ),
        )
        assert out.consolidated_findings == []
        assert "no findings" in out.synthesis_summary

    def test_no_verdict_field(self) -> None:
        # Mechanical-verdict invariant: there is NO verdict field on
        # SynthesizerOutput. Adding one would re-introduce LLM
        # judgment at the verdict step.
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "verdict": "SHIP",
                    "consolidated_findings": [],
                    "synthesis_summary": "all clean",
                }
            )

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
    def test_rejects_whitespace_only_synthesis_summary(self, blank: str) -> None:
        with pytest.raises(ValidationError):
            SynthesizerOutput(consolidated_findings=[], synthesis_summary=blank)

    def test_synthesis_summary_required(self) -> None:
        # Required even when consolidated_findings is empty.
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate({"consolidated_findings": []})

    def test_default_consolidated_findings_is_empty(self) -> None:
        # default_factory=list — passing only the summary is fine.
        out = SynthesizerOutput(synthesis_summary="nothing to consolidate")
        assert out.consolidated_findings == []

    def test_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": [],
                    "synthesis_summary": "ok",
                    "round": 0,
                }
            )

    def test_roundtrip_model_dump_json(self) -> None:
        # Persistence (PR-7 task 7) will model_dump_json(indent=2) the
        # output; round-trip through JSON to guard against schema
        # drift that would silently lose fields.
        original = SynthesizerOutput(
            consolidated_findings=[ConsolidatedFinding(**_finding())],
            synthesis_summary="one finding",
        )
        dumped = original.model_dump_json()
        loaded = SynthesizerOutput.model_validate_json(dumped)
        assert loaded == original
