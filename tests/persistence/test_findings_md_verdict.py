"""Tests for :mod:`syncade.persistence` findings.md verdict + cluster section.

Split out of the former ``tests/test_persistence.py`` monolith (PR-R2) to
keep ``test_findings_md_more.py`` under the LOC cap. Classes moved verbatim.
"""

from __future__ import annotations

from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.persistence import persist_findings_md
from syncade.process import SubprocessTimeoutError
from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput
from syncade.synthesis_clusters import ClusterMemberEvidence, RootCauseCluster
from syncade.test_runner import TestRunResult
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _ship_with_summary,
    _subprocess_result,
    _synth_output_empty,
    _synth_output_with_findings,
    _synth_result,
)


class TestFindingsMdVerdictRespectsBlockingChecks:
    """PR-21: the findings.md headline Verdict must fold in BLOCKING
    mechanical-check results the same way it folds in the test leg, so it
    never disagrees with the mechanical exit code. A synth-clean round with a
    failing blocking check exits 30 (NO-SHIP) / a blocking subprocess_error
    exits 40 (ABORT); the persisted report must agree. Advisory results are
    never passed to the verdict — they cannot move the headline off SHIP."""

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

    def _blocking_check(self, outcome: str):
        if outcome == "failed":
            return TestRunResult(
                name="lint",
                severity="blocking",
                exit_code=1,
                outcome="failed",
                duration_seconds=0.3,
                stdout="E501 line too long\n",
                stderr="",
            )
        if outcome == "subprocess_error":
            return TestRunResult(
                name="lint",
                severity="blocking",
                exit_code=-1,
                outcome="subprocess_error",
                duration_seconds=0.5,
                stdout="",
                stderr="",
                error=SubprocessTimeoutError("lint timed out", stdout="", stderr="", timeout=0.5),
            )
        return TestRunResult(
            name="lint",
            severity="blocking",
            exit_code=0,
            outcome="passed",
            duration_seconds=0.3,
            stdout="",
            stderr="",
        )

    def test_synth_clean_blocking_check_failed_says_no_ship(self, tmp_path):
        """The headline bug: synth clean + a FAILED blocking check exits 30,
        but findings.md was rendering SHIP. It must say NO-SHIP."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            None,
            self._build_dispatch(),
            check_results=[self._blocking_check("failed")],
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** NO-SHIP" in head
        # Anti-regression: the pre-fix bug rendered exactly "Verdict: SHIP".
        assert "**Verdict:** SHIP" not in head
        # The qualifier attributes the verdict to the blocking check.
        assert "lint" in head

    def test_synth_clean_blocking_check_subprocess_error_says_abort(self, tmp_path):
        """A blocking check whose binary couldn't run exits 40 — operationally
        ABORT (indeterminate), the same wording a test subprocess_error uses."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            None,
            self._build_dispatch(),
            check_results=[self._blocking_check("subprocess_error")],
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** ABORT" in head, f"expected ABORT, got headline:\n{head}"
        assert "**Verdict:** SHIP" not in head
        assert "**Verdict:** NO-SHIP" not in head

    def test_synth_clean_failing_advisory_check_still_ships(self, tmp_path):
        """Advisory results are filtered out before the verdict — a failing
        advisory check must NOT move the headline off SHIP."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        advisory_fail = TestRunResult(
            name="file-length",
            severity="advisory",
            exit_code=1,
            outcome="failed",
            duration_seconds=0.2,
            stdout="x.py: 506 > 500\n",
            stderr="",
        )
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            None,
            self._build_dispatch(),
            check_results=[advisory_fail],
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** SHIP" in head

    def test_synth_blocker_and_blocking_check_subprocess_error_says_abort(self, tmp_path):
        """Precedence: when the synth ALSO surfaces an active blocker, a blocking
        check that couldn't run (subprocess_error → ABORT, exit 40) still wins the
        headline. Checks run on synth-blocker rounds, so the report must match the
        exit code — which maps a blocking subprocess_error to 40 regardless of
        synth state. Pre-fix the synth-blocker branch returned NO-SHIP first,
        masking the indeterminate gate."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_with_findings())  # has an active blocker
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            None,
            self._build_dispatch(),
            test_skip_reason="synth_blocker",
            check_results=[self._blocking_check("subprocess_error")],
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** ABORT" in head, f"expected ABORT, got headline:\n{head}"
        assert "**Verdict:** NO-SHIP" not in head
        assert "**Verdict:** SHIP" not in head

    def test_synth_clean_passing_blocking_check_ships(self, tmp_path):
        """A passing blocking check leaves SHIP intact (no false NO-SHIP)."""
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=_synth_output_empty())
        text = persist_findings_md(
            round_dir,
            synth,
            _FIXED_STARTED_AT,
            None,
            self._build_dispatch(),
            check_results=[self._blocking_check("passed")],
        ).read_text()
        head = "\n".join(text.splitlines()[:5])
        assert "**Verdict:** SHIP" in head


class TestFindingsClusterSection:
    """PR-19: findings.md renders a descriptive-only '## Root-cause clusters'
    section ABOVE the individual findings; absent clusters → the section is
    omitted and the document is byte-identical to the pre-PR-19 layout."""

    def _output(self, *, with_cluster: bool) -> SynthesizerOutput:
        prov0 = FindingProvenance(
            reviewer_name="claude-reviewer",
            original_severity="minor",
            original_index=0,
            original_description="leak via gitignore",
        )
        prov1 = FindingProvenance(
            reviewer_name="claude-reviewer",
            original_severity="minor",
            original_index=1,
            original_description="symlink case",
        )
        findings = [
            ConsolidatedFinding(
                description="gitignore-dependent leak (directory case)",
                file="src/git_preconditions.py",
                severity="minor",
                provenance=[prov0],
            ),
            ConsolidatedFinding(
                description="gitignore-dependent leak (symlink case)",
                file="src/git_preconditions.py",
                severity="minor",
                provenance=[prov1],
            ),
        ]
        clusters = []
        if with_cluster:
            clusters = [
                RootCauseCluster(
                    member_finding_indices=[0, 1],
                    anchor_file="src/git_preconditions.py",
                    evidence=[
                        ClusterMemberEvidence(finding_index=0, quote="leak via gitignore"),
                        ClusterMemberEvidence(finding_index=1, quote="symlink case"),
                    ],
                    label="gitignore",
                )
            ]
        return SynthesizerOutput(
            consolidated_findings=findings,
            synthesis_summary="two findings, one root cause",
            root_cause_clusters=clusters,
        )

    def test_cluster_section_renders_above_findings(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=self._output(with_cluster=True))
        text = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT).read_text()

        assert "## Root-cause clusters" in text
        assert "src/git_preconditions.py" in text
        assert "#0" in text and "#1" in text
        # Verbatim reviewer quotes are rendered.
        assert "leak via gitignore" in text
        assert "symlink case" in text
        # The optional label (itself a verbatim quote substring) appears.
        assert "gitignore" in text
        # Rendered ABOVE the individual findings.
        assert text.index("## Root-cause clusters") < text.index("## Findings")
        # Descriptive-only framing present (no authored cause/fix).
        assert "do not change the verdict" in text.lower() or "advisory" in text.lower()

    def test_no_clusters_omits_section(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        synth = _synth_result(output=self._output(with_cluster=False))
        text = persist_findings_md(round_dir, synth, started_at=_FIXED_STARTED_AT).read_text()

        assert "## Root-cause clusters" not in text
        # The rest of the document renders normally.
        assert "## Synthesis summary" in text
        assert "## Findings" in text

    def test_no_clusters_byte_identical_to_clusterless_render(self, tmp_path: Path):
        """The no-cluster section addition must be a pure no-op: a SynthesizerOutput
        whose root_cause_clusters is [] renders the same findings.md it would have
        before PR-19 (the cluster code path contributes zero lines)."""
        out = self._output(with_cluster=False)
        round_dir = _make_round_dir(tmp_path)
        text = persist_findings_md(
            round_dir, _synth_result(output=out), started_at=_FIXED_STARTED_AT
        ).read_text()
        # Between the synthesis summary and the Findings heading there is exactly
        # the blank line + the heading — nothing injected by the cluster path.
        assert "one root cause\n\n## Findings\n" in text
