"""Tests for :mod:`syncade.synthesis` (PR-7 task 2 — synthesis primitives).

PR-19 root-cause cluster tests (descriptive-only, zero-invention),
split from test_synthesis.py (PR-R3).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from syncade.synthesis import (
    SynthesizerOutput,
    get_synthesizer_schema_string,
    has_active_blocker,
    parse_synthesizer_output,
)
from tests.synthesis._helpers import _finding, _provenance

# ---------------------------------------------------------------------------
# PR-19: root-cause clusters (descriptive-only, zero-invention)
# ---------------------------------------------------------------------------


def _cluster(
    *,
    member_finding_indices: list[int] | None = None,
    anchor_file: str = "src/a.py",
    evidence: list[dict] | None = None,
    label: str | None = None,
) -> dict:
    if member_finding_indices is None:
        member_finding_indices = [0, 1]
    if evidence is None:
        evidence = [{"finding_index": i, "quote": f"quote for {i}"} for i in member_finding_indices]
    out: dict = {
        "member_finding_indices": member_finding_indices,
        "anchor_file": anchor_file,
        "evidence": evidence,
    }
    if label is not None:
        out["label"] = label
    return out


def _clusterable_findings(file: str = "src/a.py", n: int = 2) -> list[dict]:
    """n findings that share a file, each minor with matching minor provenance
    (so the severity-change validator stays quiet)."""
    return [
        _finding(
            description=f"finding {i} about {file}",
            file=file,
            severity="minor",
            provenance=[_provenance(original_severity="minor", original_index=i)],
        )
        for i in range(n)
    ]


class TestRootCauseClusters:
    def test_absent_field_defaults_empty(self) -> None:
        so = SynthesizerOutput.model_validate(
            {"consolidated_findings": [], "synthesis_summary": "nothing to consolidate"}
        )
        assert so.root_cause_clusters == []

    def test_valid_cluster_accepted(self) -> None:
        so = SynthesizerOutput.model_validate(
            {
                "consolidated_findings": _clusterable_findings(n=2),
                "synthesis_summary": "two findings, one root cause",
                "root_cause_clusters": [_cluster(member_finding_indices=[0, 1])],
            }
        )
        assert len(so.root_cause_clusters) == 1
        assert so.root_cause_clusters[0].anchor_file == "src/a.py"
        assert [e.finding_index for e in so.root_cause_clusters[0].evidence] == [0, 1]

    def test_rejects_single_member_cluster(self) -> None:
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": _clusterable_findings(n=2),
                    "synthesis_summary": "s",
                    "root_cause_clusters": [
                        {
                            "member_finding_indices": [0],
                            "anchor_file": "src/a.py",
                            "evidence": [{"finding_index": 0, "quote": "q"}],
                        }
                    ],
                }
            )

    def test_rejects_out_of_range_member(self) -> None:
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": _clusterable_findings(n=2),
                    "synthesis_summary": "s",
                    "root_cause_clusters": [_cluster(member_finding_indices=[0, 5])],
                }
            )

    def test_rejects_non_disjoint_clusters(self) -> None:
        """Finding 1 cannot belong to two clusters."""
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": _clusterable_findings(n=3),
                    "synthesis_summary": "s",
                    "root_cause_clusters": [
                        _cluster(member_finding_indices=[0, 1]),
                        _cluster(member_finding_indices=[1, 2]),
                    ],
                }
            )

    def test_rejects_anchor_file_not_matching_members(self) -> None:
        findings = [
            _finding(
                description="a",
                file="src/a.py",
                severity="minor",
                provenance=[_provenance(original_severity="minor")],
            ),
            _finding(
                description="b",
                file="src/b.py",
                severity="minor",
                provenance=[_provenance(original_severity="minor")],
            ),
        ]
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": findings,
                    "synthesis_summary": "s",
                    "root_cause_clusters": [
                        _cluster(member_finding_indices=[0, 1], anchor_file="src/a.py")
                    ],
                }
            )

    def test_rejects_file_none_member(self) -> None:
        """A finding with file=None cannot be clustered (anchor_file is a
        required non-blank string; the precision floor)."""
        findings = [
            _finding(
                description="a",
                file=None,
                severity="minor",
                provenance=[_provenance(original_severity="minor")],
            ),
            _finding(
                description="b",
                file=None,
                severity="minor",
                provenance=[_provenance(original_severity="minor")],
            ),
        ]
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": findings,
                    "synthesis_summary": "s",
                    "root_cause_clusters": [
                        _cluster(member_finding_indices=[0, 1], anchor_file="src/a.py")
                    ],
                }
            )

    def test_rejects_evidence_set_not_equal_member_set(self) -> None:
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": _clusterable_findings(n=2),
                    "synthesis_summary": "s",
                    "root_cause_clusters": [
                        _cluster(
                            member_finding_indices=[0, 1],
                            evidence=[
                                {"finding_index": 0, "quote": "q0"},
                                {"finding_index": 0, "quote": "q0 again"},
                            ],
                        )
                    ],
                }
            )

    def test_rejects_whitespace_only_quote(self) -> None:
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": _clusterable_findings(n=2),
                    "synthesis_summary": "s",
                    "root_cause_clusters": [
                        _cluster(
                            member_finding_indices=[0, 1],
                            evidence=[
                                {"finding_index": 0, "quote": "real"},
                                {"finding_index": 1, "quote": "   "},
                            ],
                        )
                    ],
                }
            )

    def test_label_must_be_substring_of_a_quote(self) -> None:
        # label that IS a substring of a quote → accepted
        so = SynthesizerOutput.model_validate(
            {
                "consolidated_findings": _clusterable_findings(n=2),
                "synthesis_summary": "s",
                "root_cause_clusters": [
                    _cluster(
                        member_finding_indices=[0, 1],
                        evidence=[
                            {"finding_index": 0, "quote": "depends on .gitignore"},
                            {"finding_index": 1, "quote": "symlink case"},
                        ],
                        label="gitignore",
                    )
                ],
            }
        )
        assert so.root_cause_clusters[0].label == "gitignore"
        # label that is authored prose (not a substring of any quote) → rejected
        with pytest.raises(ValidationError):
            SynthesizerOutput.model_validate(
                {
                    "consolidated_findings": _clusterable_findings(n=2),
                    "synthesis_summary": "s",
                    "root_cause_clusters": [
                        _cluster(
                            member_finding_indices=[0, 1],
                            evidence=[
                                {"finding_index": 0, "quote": "depends on .gitignore"},
                                {"finding_index": 1, "quote": "symlink case"},
                            ],
                            label="the real root cause is precedence",
                        )
                    ],
                }
            )

    def test_clusters_do_not_affect_has_active_blocker(self) -> None:
        """The mechanical verdict reads consolidated findings only — clustering
        the same findings must not change has_active_blocker either way."""
        blocker_findings = [
            _finding(
                description=f"blocker {i}",
                file="src/a.py",
                severity="blocker",
                provenance=[_provenance(original_severity="blocker", original_index=i)],
            )
            for i in range(2)
        ]
        without = SynthesizerOutput.model_validate(
            {"consolidated_findings": blocker_findings, "synthesis_summary": "s"}
        )
        with_cluster = SynthesizerOutput.model_validate(
            {
                "consolidated_findings": blocker_findings,
                "synthesis_summary": "s",
                "root_cause_clusters": [_cluster(member_finding_indices=[0, 1])],
            }
        )
        assert has_active_blocker(without) is True
        assert has_active_blocker(with_cluster) is True
        assert has_active_blocker(without) == has_active_blocker(with_cluster)

        # And the inverse: minor findings stay SHIP whether clustered or not.
        minor = SynthesizerOutput.model_validate(
            {
                "consolidated_findings": _clusterable_findings(n=2),
                "synthesis_summary": "s",
                "root_cause_clusters": [_cluster(member_finding_indices=[0, 1])],
            }
        )
        assert has_active_blocker(minor) is False

    def test_parse_synthesizer_output_round_trips_clusters(self) -> None:
        payload = {
            "consolidated_findings": _clusterable_findings(n=2),
            "synthesis_summary": "two findings, one root cause",
            "root_cause_clusters": [_cluster(member_finding_indices=[0, 1])],
        }
        raw = "```json\n" + json.dumps(payload) + "\n```"
        out = parse_synthesizer_output(raw)
        assert len(out.root_cause_clusters) == 1
        assert out.root_cause_clusters[0].member_finding_indices == [0, 1]

    def test_schema_string_documents_clusters(self) -> None:
        schema = get_synthesizer_schema_string()
        assert "root_cause_clusters" in schema
        assert "ADVISORY" in schema  # never affects the verdict
        assert "VERBATIM" in schema  # do not paraphrase
