"""Unanimous-blocker dismissal: distinct-reviewer keying.

Split out of ``tests/synthesis/test_models.py`` to keep that file
under the LOC gate. Covers the mis-key fix — the unanimous-blocker
rule keys on DISTINCT ``reviewer_name``s, not raw provenance entries.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence._markdown import _consensus_lines
from syncade.synthesis import ConsolidatedFinding
from tests.synthesis._helpers import _finding, _provenance


class TestUnanimousBlockerDistinctKeying:
    def test_single_reviewer_listed_twice_does_not_lock_dismissal(self) -> None:
        """Mis-key fix: the unanimous-blocker rule keys on DISTINCT
        reviewers, not raw provenance entries. ONE reviewer listed
        twice (two entries, same ``reviewer_name``, both blocker) is a
        single-reviewer blocker — dismissible — and must NOT be falsely
        locked. Before the fix ``len(provenance) >= 2`` counted the two
        raw entries and rejected the dismissal.
        """
        cf = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[
                    _provenance(reviewer_name="claude-reviewer", original_index=0),
                    _provenance(reviewer_name="claude-reviewer", original_index=1),
                ],
                dismissed=True,
                dismissal_rationale=(
                    "the single reviewer flagged this twice; both are the same "
                    "false positive the spec exempts in §3.2"
                ),
            )
        )
        assert cf.dismissed is True

    def test_two_distinct_reviewers_both_blocker_still_locked(self) -> None:
        """Keep: two DISTINCT reviewers both at blocker is a genuine
        unanimous blocker — dismissal stays refused after the
        distinct-keying change (the mis-key fix must not weaken the
        guard for the real case)."""
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="blocker",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer"),
                        _provenance(reviewer_name="codex-reviewer"),
                    ],
                    dismissed=True,
                    dismissal_rationale="I think both reviewers misread the spec",
                )
            )
        message = str(exc_info.value)
        assert "unanimous-blocker" in message
        # Distinct count of 2 reflected in the message, not a raw-entry count.
        assert "by 2 reviewers" in message

    def test_schema_guard_and_consensus_renderer_both_count_distinct_reviewers(self) -> None:
        finding = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[
                    _provenance(reviewer_name="claude-reviewer", original_index=0),
                    _provenance(reviewer_name="claude-reviewer", original_index=1),
                ],
                dismissed=True,
                dismissal_rationale="same reviewer duplicated the same false positive",
            )
        )
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="claude-reviewer",
                    provider="anthropic",
                    output=None,
                    error=RuntimeError("not used by consensus rendering"),
                    duration_seconds=1.0,
                ),
                ReviewerRunResult(
                    reviewer_name="codex-reviewer",
                    provider="openai",
                    output=None,
                    error=RuntimeError("not used by consensus rendering"),
                    duration_seconds=1.0,
                ),
            ],
            total_duration_seconds=2.0,
        )

        assert _consensus_lines(finding, dispatch) == ["**Consensus:** 1 of 2 reviewers  "]


class TestUnanimousBlockerMixedProvenance:
    """2026-07-27 audit rank 1 (A C-01 / B C1): a third, lower-severity
    provenance entry must not disarm the guard.

    The predicate used to be ``all(p.original_severity == "blocker")``
    over raw provenance, so ``[r1:blocker, r2:blocker, r1:minor]`` — a
    shape the synthesizer reaches by merging a reviewer's own smaller
    note about the same concern — turned a locked unanimous blocker
    into a dismissible finding, i.e. a false SHIP. Coverage is now
    counted over the blocker-severity entries only, which makes the
    predicate monotone: extra provenance can add coverage, never
    remove it.
    """

    def test_extra_minor_entry_from_counted_reviewer_does_not_unlock_dismissal(self) -> None:
        """The audit's exact repro: [r1:blocker, r2:blocker, r1:minor]."""
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="blocker",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer", original_index=0),
                        _provenance(reviewer_name="codex-reviewer", original_index=0),
                        _provenance(
                            reviewer_name="claude-reviewer",
                            original_index=1,
                            original_severity="minor",
                            original_description="nullability nit on the same column",
                        ),
                    ],
                    dismissed=True,
                    dismissal_rationale="one of them called it minor, so it's minor",
                )
            )
        message = str(exc_info.value)
        assert "unanimous-blocker" in message
        # Counted over blocker entries only: 2, not 3 raw entries and not 1.
        assert "by 2 reviewers" in message
        # The message enumerates the blocker provenance, so it cannot claim
        # the minor entry was raised "@blocker".
        assert message.count("@blocker") == 2

    def test_extra_minor_entry_does_not_unlock_downgrade(self) -> None:
        """Same shape, the other deactivation path (finding A2 covers
        dismissal AND downgrade; the bypass reopened both)."""
        with pytest.raises(ValidationError) as exc_info:
            ConsolidatedFinding(
                **_finding(
                    severity="minor",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer", original_index=0),
                        _provenance(reviewer_name="codex-reviewer", original_index=0),
                        _provenance(
                            reviewer_name="claude-reviewer",
                            original_index=1,
                            original_severity="nit",
                            original_description="style nit on the same line",
                        ),
                    ],
                    severity_change_rationale="downgrading, two of three entries agree",
                )
            )
        assert "unanimous-blocker" in str(exc_info.value)

    def test_third_distinct_reviewer_at_minor_does_not_unlock(self) -> None:
        """The extra entry belonging to an UNCOUNTED reviewer is the
        same bypass — two distinct reviewers still said blocker."""
        with pytest.raises(ValidationError):
            ConsolidatedFinding(
                **_finding(
                    severity="blocker",
                    provenance=[
                        _provenance(reviewer_name="claude-reviewer"),
                        _provenance(reviewer_name="codex-reviewer"),
                        _provenance(
                            reviewer_name="gemini-reviewer",
                            original_severity="minor",
                            original_description="probably fine, flagging lightly",
                        ),
                    ],
                    dismissed=True,
                    dismissal_rationale="the third reviewer downgraded it",
                )
            )

    def test_genuine_single_blocker_reviewer_stays_dismissible(self) -> None:
        """Negative control: the fix must not over-lock. Only ONE
        reviewer said blocker, so this was never unanimous and the
        synthesizer keeps its authority to dismiss it."""
        cf = ConsolidatedFinding(
            **_finding(
                severity="blocker",
                provenance=[
                    _provenance(reviewer_name="claude-reviewer"),
                    _provenance(
                        reviewer_name="codex-reviewer",
                        original_severity="minor",
                        original_description="minor style concern on the same line",
                    ),
                ],
                dismissed=True,
                dismissal_rationale=(
                    "only one reviewer called this a blocker and the spec "
                    "explicitly exempts the pattern in §3.2"
                ),
            )
        )
        assert cf.dismissed is True
