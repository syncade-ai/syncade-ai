"""Tests for the synthesizer parser's RETRY-REPAIR tier (:mod:`syncade.synthesis_repair`).

The parser validates strictly first. Only on failure does it apply a narrow, closed
list of KNOWN, information-free deviations and validate once more. These tests pin
the property that makes that safe: it is a repair LIST, not a tolerance — every
deviation NOT on the list still reaches exit 70.

Most of this file exists because syncade's own adversarial panel kept breaking the
first three versions of the cluster repair. Each test names the level it caught.
"""

from __future__ import annotations

import json

import pytest

from syncade.synthesis import (
    SynthesizerOutput,
    SynthesizerOutputError,
    has_active_blocker,
    parse_synthesizer_output,
)
from tests.synthesis._helpers import _finding, _provenance
from tests.synthesis.test_clusters import _cluster, _clusterable_findings


class TestRetryRepairTier:
    def test_repairs_reviewer_only_fields_inside_cluster_evidence(self) -> None:
        """N10: live Codex sometimes copies reviewer-only ``line`` /
        ``spec_clause`` into a ``root_cause_clusters[*].evidence[*]`` quote
        object. ``ClusterMemberEvidence`` forbids extras, so without the
        retry-path repair an otherwise valid consolidation would become exit
        70. The repair must strip those keys (mirroring the consolidated_findings
        repair onto cluster evidence) and accept the output.
        """
        evidence = [
            {
                "finding_index": 0,
                "quote": "quote for 0",
                "line": 42,
                "spec_clause": "reviewer-only context",
            },
            {"finding_index": 1, "quote": "quote for 1"},
        ]
        payload = {
            # The findings themselves carry no reviewer-only keys, so the only
            # repairable extras live inside the cluster evidence — pinning the
            # NEW path, not the pre-existing consolidated_findings repair.
            "consolidated_findings": _clusterable_findings(n=2),
            "synthesis_summary": "two findings, one root cause",
            "root_cause_clusters": [_cluster(member_finding_indices=[0, 1], evidence=evidence)],
        }
        out = parse_synthesizer_output(json.dumps(payload))
        assert isinstance(out, SynthesizerOutput)
        assert len(out.root_cause_clusters) == 1
        dumped = out.root_cause_clusters[0].evidence[0].model_dump()
        assert "line" not in dumped
        assert "spec_clause" not in dumped
        # The quote itself survives the repair unchanged.
        assert out.root_cause_clusters[0].evidence[0].quote == "quote for 0"

    def test_drops_degenerate_cluster_with_no_members(self) -> None:
        """PR-v2-23, observed live on claude-sonnet-4-6: handed a single finding —
        i.e. nothing to cluster — it emitted ONE cluster with empty
        ``member_finding_indices`` and empty ``evidence`` instead of the ``[]``
        the schema asks for. ``min_length=2`` rejected it and took the entire
        (otherwise perfect) consolidation to exit 70 with it.

        gpt-5.5 emits ``[]`` correctly, which is exactly why this never surfaced
        while the judge was hardwired to codex. A cluster naming zero findings
        asserts nothing, so drop it and keep the run.
        """
        payload = {
            "consolidated_findings": _clusterable_findings(n=1),
            "synthesis_summary": "one finding, nothing to cluster",
            "root_cause_clusters": [
                {"member_finding_indices": [], "anchor_file": "src/a.py", "evidence": []}
            ],
        }
        out = parse_synthesizer_output(json.dumps(payload))
        assert isinstance(out, SynthesizerOutput)
        assert out.root_cause_clusters == []
        # The FINDING is untouched — dropping a cluster must never lose a finding.
        assert len(out.consolidated_findings) == 1

    def test_single_member_cluster_is_NOT_dropped_it_is_loud(self) -> None:
        """A 1-member cluster is NOT repaired. This test previously asserted the
        opposite, and asserting the opposite was the bug.

        The repair must match the OBSERVED deviation, not a generalisation of it.
        What claude-sonnet-4-6 emits is an EMPTY cluster — nothing to cluster, so it
        references nothing. Generalising that to "fewer than 2 members" quietly swept
        in the 1-member case, and a reviewer showed why that is different in kind: a
        cluster's real invariants include cross-checks against consolidated_findings
        (indices in range, anchor_file equal to the members' file, clusters disjoint)
        that a per-cluster probe cannot see. A 1-member cluster with an OUT-OF-RANGE
        index would have sailed through and been silently dropped.

        A 1-member cluster NAMED a finding. It makes a claim, and a wrong claim has
        to be loud. An empty cluster claims nothing, which is exactly why it — and
        only it — is safe to discard without consulting the findings list.
        """
        payload = {
            "consolidated_findings": _clusterable_findings(n=2),
            "synthesis_summary": "two findings",
            "root_cause_clusters": [
                {
                    "member_finding_indices": [0],
                    "anchor_file": "src/a.py",
                    "evidence": [{"finding_index": 0, "quote": "quote for 0"}],
                }
            ],
        }
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(json.dumps(payload))

    def test_single_member_cluster_with_out_of_range_index_is_loud(self) -> None:
        """The concrete case the per-cluster probe could not have caught. Index 99
        does not exist in a 1-finding consolidation; under the old "<2 members" rule
        this validated in isolation and was dropped, swallowing a real violation."""
        payload = {
            "consolidated_findings": _clusterable_findings(n=1),
            "synthesis_summary": "one finding",
            "root_cause_clusters": [
                {
                    "member_finding_indices": [99],
                    "anchor_file": "src/a.py",
                    "evidence": [{"finding_index": 99, "quote": "q"}],
                }
            ],
        }
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(json.dumps(payload))

    def test_degenerate_drop_does_not_touch_a_legitimate_cluster(self) -> None:
        """The repair is surgical: a valid (>=2 member) cluster in the SAME payload
        as a degenerate one must survive intact. A repair that ate real clusters
        would silently discard synthesis the judge actually did."""
        payload = {
            "consolidated_findings": _clusterable_findings(n=2),
            "synthesis_summary": "one real cluster, one degenerate",
            "root_cause_clusters": [
                {"member_finding_indices": [], "anchor_file": "src/a.py", "evidence": []},
                _cluster(member_finding_indices=[0, 1]),
            ],
        }
        out = parse_synthesizer_output(json.dumps(payload))
        assert len(out.root_cause_clusters) == 1
        surviving = out.root_cause_clusters[0]
        assert surviving.member_finding_indices == [0, 1]
        assert [e.quote for e in surviving.evidence] == ["quote for 0", "quote for 1"]

    def test_degenerate_cluster_cannot_launder_an_active_blocker(self) -> None:
        """The load-bearing safety property. Clusters are ADVISORY and never affect
        the verdict, so dropping one must not be able to soften a NO-SHIP. A
        unanimous blocker stays active and ``has_active_blocker`` stays True even
        though the payload only parsed BECAUSE of the repair."""
        blocker = _finding(
            description="unanimous blocker",
            file="src/a.py",
            severity="blocker",
            provenance=[
                _provenance(reviewer_name="rv-a", original_severity="blocker", original_index=0),
                _provenance(reviewer_name="rv-b", original_severity="blocker", original_index=0),
            ],
        )
        payload = {
            "consolidated_findings": [blocker],
            "synthesis_summary": "one unanimous blocker",
            "root_cause_clusters": [
                {"member_finding_indices": [], "anchor_file": "src/a.py", "evidence": []}
            ],
        }
        out = parse_synthesizer_output(json.dumps(payload))
        assert out.root_cause_clusters == []
        assert len(out.consolidated_findings) == 1
        assert out.consolidated_findings[0].dismissed is False
        assert has_active_blocker(out) is True

    def test_repair_is_a_list_not_a_tolerance(self) -> None:
        """An unknown extra key still fails. The retry path repairs a KNOWN,
        information-free deviation list — it does not make the schema lenient."""
        payload = {
            "consolidated_findings": _clusterable_findings(n=1),
            "synthesis_summary": "one finding",
            "root_cause_clusters": [
                {"member_finding_indices": [], "anchor_file": "src/a.py", "evidence": []}
            ],
        }
        payload["consolidated_findings"][0]["invented_field"] = "nope"
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(json.dumps(payload))

    def test_degenerate_cluster_with_extra_key_fails_validation(self) -> None:
        """A degenerate cluster (<2 members) carrying an unrelated unknown key
        must NOT be silently dropped. Dropping it would hide schema drift; the
        same unknown key on a non-degenerate cluster is rejected, so the hole
        must not exist only on the degenerate path.

        Regression: _drop_empty_clusters previously dropped the entire
        cluster dict before Pydantic saw the extra key, so the violation was
        silently accepted.
        """
        payload = {
            "consolidated_findings": _clusterable_findings(n=1),
            "synthesis_summary": "one finding",
            "root_cause_clusters": [
                {
                    "member_finding_indices": [0],
                    "anchor_file": "src/a.py",
                    "evidence": [{"finding_index": 0, "quote": "verbatim excerpt"}],
                    "unknown_extra_key": "this must not be silently swallowed",
                }
            ],
        }
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(json.dumps(payload))

    @pytest.mark.parametrize(
        ("label", "cluster"),
        [
            (
                "unknown top-level key",
                {
                    "member_finding_indices": [],
                    "anchor_file": "src/a.py",
                    "evidence": [],
                    "bogus": 1,
                },
            ),
            (
                "unknown key nested in evidence",
                {
                    "member_finding_indices": [],
                    "anchor_file": "src/a.py",
                    "evidence": [{"finding_index": 0, "quote": "q", "bogus": "X"}],
                },
            ),
            (
                "wrong value type",
                {"member_finding_indices": [], "anchor_file": 123, "evidence": []},
            ),
            (
                "wrong value type nested in evidence",
                {
                    "member_finding_indices": [0],
                    "anchor_file": "src/a.py",
                    "evidence": [{"finding_index": "NaN", "quote": "q"}],
                },
            ),
            (
                "blank anchor_file",
                {"member_finding_indices": [], "anchor_file": "   ", "evidence": []},
            ),
            (
                "evidence inconsistent with members",
                {
                    "member_finding_indices": [],
                    "anchor_file": "src/a.py",
                    "evidence": [{"finding_index": 0, "quote": "q"}],
                },
            ),
            ("missing required key", {"member_finding_indices": [], "evidence": []}),
        ],
    )
    def test_degenerate_drop_stays_loud_on_any_other_defect(self, label, cluster) -> None:
        """A degenerate cluster is dropped ONLY when being degenerate is the single
        thing wrong with it. Any other defect must still reach exit 70.

        Every case here is a level syncade's own adversarial panel escalated through
        across three rounds. The first fix gated on cluster key names; a reviewer
        found unknown keys nested in `evidence`; the next round's reviewer found
        unknown VALUE TYPES going through the same hole. Keys, then nested keys, then
        types — the checklist was unbounded, and each entry was a chance to miss one.

        `_OnlyDegenerateProbe` inverts it: validate against RootCauseCluster with only
        the >=2 constraints relaxed, so EVERY constraint the real model has (now and
        in future) is enforced for free. The last three cases below were never on any
        checklist — they pass because the design is total, not because someone
        remembered them.
        """
        del label
        payload = {
            "consolidated_findings": _clusterable_findings(n=1),
            "synthesis_summary": "one finding",
            "root_cause_clusters": [cluster],
        }
        with pytest.raises(SynthesizerOutputError):
            parse_synthesizer_output(json.dumps(payload))
