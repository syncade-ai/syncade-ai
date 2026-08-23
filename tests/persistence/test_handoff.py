"""Tests for ``persist_handoff`` (moved verbatim from the former
``tests/test_persistence.py`` — PR-R2 test-monster decomposition).
"""

from __future__ import annotations

from pathlib import Path

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutput
from syncade.snapshot import Snapshot
from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput


class TestPersistHandoff:
    """PR-10.5: ``persist_handoff`` writes ``<run_dir>/handoff.md``
    when the loop terminates with active blockers (exit 20 / 30).

    The four tests below mirror the brief's acceptance criteria:

    1. Only writes on exit 20/30 with blockers (no-op otherwise).
    2. Auto-classifies findings across all heuristic categories.
    3. Renders the producer commits across rounds.
    4. Handles the no-producer-runs path explicitly.
    """

    def _blocker_finding(
        self,
        description: str,
        file: str | None,
        *,
        provenance: list[FindingProvenance] | None = None,
    ) -> ConsolidatedFinding:
        if provenance is None:
            provenance = [
                FindingProvenance(
                    reviewer_name="codex-reviewer",
                    original_severity="blocker",
                    original_index=0,
                    original_description=description,
                ),
            ]
        return ConsolidatedFinding(
            description=description,
            file=file,
            severity="blocker",
            provenance=provenance,
            dismissed=False,
            dismissal_rationale=None,
            severity_change_rationale=None,
        )

    def _round_with_synth(
        self,
        round_idx: int,
        consolidated_findings: list[ConsolidatedFinding],
        *,
        producer_outcome: str | None = None,
        ending_sha: str | None = None,
        round_exit_code: int = 30,
        commit_sha: str = "a" * 40,
    ):
        from syncade.adapters.producer import ProducerOutput
        from syncade.orchestrator import RoundArtifacts, RoundResult
        from syncade.producer import ProducerResult
        from syncade.synthesizer import SynthesizerResult

        synth_output = SynthesizerOutput(
            consolidated_findings=consolidated_findings,
            synthesis_summary="synth ran",
        )
        synth_result = SynthesizerResult(output=synth_output, error=None, duration_seconds=5.0)
        dispatch = DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=ReviewerOutput(
                        verdict="NO-SHIP",
                        findings=[],
                        summary="ok",
                        priority_order=[],
                        coverage_gaps=[],
                        dismissed_concerns=[],
                    ),
                    error=None,
                    duration_seconds=2.0,
                ),
            ],
            total_duration_seconds=2.0,
        )
        snapshot = Snapshot(
            repo_root=Path("/tmp/test"),
            commit_sha=commit_sha,
            branch="main",
            base_ref=None,
            diff_text="",
            dirty_state="clean",
        )
        producer = None
        if producer_outcome is not None:
            if producer_outcome == "committed":
                producer = ProducerResult(
                    outcome="committed",
                    starting_sha="a" * 40,
                    ending_sha=ending_sha or ("c" * 40),
                    duration_seconds=20.0,
                    output=ProducerOutput(narrative_text="fix attempted"),
                    error=None,
                )
            elif producer_outcome == "stalled":
                producer = ProducerResult(
                    outcome="stalled",
                    starting_sha="a" * 40,
                    ending_sha="a" * 40,
                    duration_seconds=20.0,
                    output=ProducerOutput(narrative_text="no edits made"),
                    error=None,
                )
        round_dir = Path(f"round-{round_idx}")
        return RoundResult(
            round_idx=round_idx,
            snapshot=snapshot,
            dispatch_result=dispatch,
            synth_result=synth_result,
            test_result=None,
            test_skip_reason="test_command_unset",
            test_worktree_error=None,
            producer_result=producer,
            round_exit_code=round_exit_code,
            artifacts=RoundArtifacts(
                round_idx=round_idx,
                round_dir=round_dir,
                manifest_path=round_dir / "manifest.json",
                summary_path=round_dir / "summary.md",
            ),
        )

    def test_persist_handoff_only_writes_on_exit_20_or_30_with_blockers(self, tmp_path):
        """Gate matrix: no handoff on exit 0 (loop SHIPped); no
        handoff on exit 20 with zero blockers (impossible-state
        guard); no handoff on exit 30 with zero blockers (spec gate
        requires active_blocker_count > 0 for ALL exit-30 paths);
        handoff written on exit 20 WITH blockers; handoff written on
        exit 30 WITH blockers."""
        from syncade.persistence import persist_handoff

        # exit 0 → no handoff
        run_dir = tmp_path / "run-ship"
        run_dir.mkdir()
        result = persist_handoff(
            run_dir,
            final_exit_code=0,
            final_round=0,
            termination_reason="ship",
            rounds=[self._round_with_synth(0, [], round_exit_code=0)],
            max_rounds=1,
        )
        assert result is None
        assert not (run_dir / "handoff.md").exists()

        # exit 20 + zero blockers → no handoff (max_rounds_reached
        # without blockers is an impossible state; the loop should
        # have shipped instead).
        run_dir2 = tmp_path / "run-20-empty"
        run_dir2.mkdir()
        result = persist_handoff(
            run_dir2,
            final_exit_code=20,
            final_round=2,
            termination_reason="max_rounds_reached",
            rounds=[self._round_with_synth(i, []) for i in range(3)],
            max_rounds=3,
        )
        assert result is None
        assert not (run_dir2 / "handoff.md").exists()

        # exit 30 + zero blockers → no handoff. The spec gate is
        # final_exit_code in (20, 30) AND active_blocker_count > 0;
        # producer_stalled with a clean synth does not produce a
        # handoff because there are no blockers for the operator to
        # action.
        run_dir2b = tmp_path / "run-30-empty"
        run_dir2b.mkdir()
        result = persist_handoff(
            run_dir2b,
            final_exit_code=30,
            final_round=0,
            termination_reason="producer_stalled",
            rounds=[self._round_with_synth(0, [], producer_outcome="stalled")],
            max_rounds=3,
        )
        assert result is None
        assert not (run_dir2b / "handoff.md").exists()

        # exit 20 + blockers → handoff written.
        run_dir3 = tmp_path / "run-20"
        run_dir3.mkdir()
        finding = self._blocker_finding(
            description="genuine code defect in src/syncade/foo.py",
            file="src/syncade/foo.py",
        )
        result = persist_handoff(
            run_dir3,
            final_exit_code=20,
            final_round=2,
            termination_reason="max_rounds_reached",
            rounds=[self._round_with_synth(i, [finding]) for i in range(3)],
            max_rounds=3,
        )
        assert result is not None
        assert result.name == "handoff.md"
        text = result.read_text()
        assert "Final exit code:** 20" in text
        assert "Active blockers remaining:** 1" in text
        assert "max rounds reached" in text

        # exit 30 + blockers → handoff written.
        run_dir4 = tmp_path / "run-30"
        run_dir4.mkdir()
        result = persist_handoff(
            run_dir4,
            final_exit_code=30,
            final_round=0,
            termination_reason="producer_stalled",
            rounds=[self._round_with_synth(0, [finding], producer_outcome="stalled")],
            max_rounds=3,
        )
        assert result is not None
        text = result.read_text()
        assert "Final exit code:** 30" in text
        assert "producer stalled" in text

    def test_persist_handoff_auto_classifies_categories(self, tmp_path):
        """Synthetic findings covering each heuristic category
        (P/F/A/M) — verify each one ends up under the right group in
        the rendered markdown."""
        from syncade.persistence import persist_handoff

        # P: workflow-state phrase + PR-brief file path
        p_finding = self._blocker_finding(
            description="PR brief still records Stage 2 as in progress; "
            "status header has not been updated yet",
            file="path/to/pr.md",
        )
        # F: convention-mismatch phrase
        f_finding = self._blocker_finding(
            description="brief said exit 60 if config load fails; "
            "implementation correctly follows the established 50/60 "
            "convention mismatch from _run_selfcheck precedent",
            file="src/syncade/cli/__init__.py",
        )
        # A: operator-attested phrase (sandbox)
        a_finding = self._blocker_finding(
            description="reviewer couldn't run the real-CLI smoke; "
            "recursive `claude -p` invocation blocked by the sandbox",
            file="tests/smoke/test_anthropic_smoke.py",
        )
        # M: default (no matching phrases)
        m_finding = self._blocker_finding(
            description="null pointer dereference at the new code path",
            file="src/syncade/foo.py",
        )

        rounds = [
            self._round_with_synth(
                0,
                [p_finding, f_finding, a_finding, m_finding],
                producer_outcome="committed",
                ending_sha="d" * 40,
            ),
            self._round_with_synth(
                1,
                [p_finding, f_finding, a_finding, m_finding],
            ),
        ]
        run_dir = tmp_path / "run-classify"
        run_dir.mkdir()
        path = persist_handoff(
            run_dir,
            final_exit_code=20,
            final_round=1,
            termination_reason="max_rounds_reached",
            rounds=rounds,
            max_rounds=2,
            pr_doc_path=Path("path/to/pr.md"),
        )
        assert path is not None
        text = path.read_text()
        # All five category headers appear under "Suggested next-
        # step categories".
        assert "### M — Manual fix needed" in text
        assert "### F — False positive / convention mismatch" in text
        assert "### P — Operator-procedural / self-resolving" in text
        assert "### A — Operator-attested" in text
        assert "### D — Dismiss with rationale" in text
        # The heuristic warning is in the rendered output so the
        # operator knows to treat it as a hint, not a decision.
        assert "HEURISTIC" in text
        # Each category bucket lists the right blocker. Match on
        # the unique description prefix.
        assert "Blocker 1: PR brief still records" in text or "Blocker 1: PR brief still" in text
        # Per-blocker disposition category line is in the "What's
        # left" section.
        assert "Suggested disposition category:** P — Operator-procedural" in text
        assert "Suggested disposition category:** F — False positive" in text
        assert "Suggested disposition category:** A — Operator-attested" in text
        assert "Suggested disposition category:** M — Manual fix needed" in text

    def test_persist_handoff_attempts_section_renders_producer_candidates(self, tmp_path):
        """Producer-attempted section: ``Round N producer candidate:
        <sha>`` for each committed round, plus the stall outcome
        when applicable."""
        from syncade.persistence import persist_handoff

        finding = self._blocker_finding(
            description="something wrong in src/foo.py",
            file="src/foo.py",
        )
        rounds = [
            self._round_with_synth(0, [finding], producer_outcome="committed", ending_sha="c" * 40),
            self._round_with_synth(1, [finding], producer_outcome="committed", ending_sha="d" * 40),
            self._round_with_synth(2, [finding]),  # no producer (final round)
        ]
        run_dir = tmp_path / "run-attempts"
        run_dir.mkdir()
        path = persist_handoff(
            run_dir,
            final_exit_code=20,
            final_round=2,
            termination_reason="max_rounds_reached",
            rounds=rounds,
            max_rounds=3,
        )
        assert path is not None
        text = path.read_text()
        assert "## What the producer attempted" in text
        # Two isolated producer candidates rendered.
        assert "Round 0 producer candidate:** `cccccccccccc`" in text
        assert "Round 1 producer candidate:** `dddddddddddd`" in text
        # Round 2 didn't run a producer — not in the section.
        assert "Round 2 producer commit" not in text
        # Findings-addressed and remaining-forwarded rollup present
        # for each committed round (all three rounds have 1 blocker,
        # so count change is 0 addressed / 1 remaining each time).
        assert "Findings addressed:** (heuristic)" in text
        assert "Remaining findings forwarded to round" in text

    def test_persist_handoff_handles_no_producer_runs(self, tmp_path):
        """Exit 30 + producer_stalled at round 0 → the section
        explicitly says ``(no producer rounds ran)``."""
        from syncade.persistence import persist_handoff

        finding = self._blocker_finding(
            description="real code defect in src/foo.py",
            file="src/foo.py",
        )
        # Single round, no producer ran at all (e.g. the loop
        # terminated before reaching the producer phase). When
        # producer_result is None the round contributes nothing
        # to the "what the producer attempted" section.
        rounds = [self._round_with_synth(0, [finding])]
        run_dir = tmp_path / "run-no-producer"
        run_dir.mkdir()
        path = persist_handoff(
            run_dir,
            final_exit_code=30,
            final_round=0,
            termination_reason="producer_stalled",
            rounds=rounds,
            max_rounds=3,
        )
        assert path is not None
        text = path.read_text()
        assert "## What the producer attempted" in text
        assert "(no producer rounds ran)" in text

    def test_persist_handoff_multiline_description_renders_valid_bullets(self, tmp_path):
        """N9: a multi-line synthesizer description must not break the
        handoff's bullet list. The description is flattened onto the single
        ``- **Description:**`` bullet (mirroring findings_md), so every line in
        the blocker's metadata block stays a bullet — no stray paragraph or
        dash-prefixed continuation leaks out to detach the disposition/action
        bullets below it."""
        from syncade.persistence import persist_handoff

        finding = self._blocker_finding(
            description=(
                "First line summary of the defect\n"
                "\n"
                "Second paragraph with more detail\n"
                "- a dash-prefixed continuation line"
            ),
            file="src/foo.py",
        )
        run_dir = tmp_path / "run-multiline"
        run_dir.mkdir()
        path = persist_handoff(
            run_dir,
            final_exit_code=30,
            final_round=0,
            termination_reason="producer_stalled",
            rounds=[self._round_with_synth(0, [finding], producer_outcome="stalled")],
            max_rounds=3,
        )
        assert path is not None
        text = path.read_text()

        # Isolate Blocker 1's metadata block: the lines from the
        # "### Blocker 1" header up to the next "## "/"### " header.
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("### Blocker 1"))
        block: list[str] = [lines[start]]
        for ln in lines[start + 1 :]:
            if ln.startswith("## ") or ln.startswith("### "):
                break
            block.append(ln)

        # Every non-blank line in the block is either the section header or a
        # markdown bullet. Pre-fix, the embedded newlines split the description
        # into a stray paragraph + dash line, breaking this invariant.
        for ln in block:
            if not ln.strip():
                continue
            assert ln.startswith("### Blocker 1") or ln.startswith("- "), (
                f"multi-line description broke the handoff bullet list: {ln!r}"
            )

        # The full description survives, flattened onto one Description bullet.
        desc_line = next(ln for ln in block if ln.startswith("- **Description:**"))
        assert "Second paragraph with more detail" in desc_line
        assert "dash-prefixed continuation line" in desc_line
        # And the disposition/action bullets still render after it.
        assert any(ln.startswith("- **Suggested disposition category:**") for ln in block)
        assert any(ln.startswith("- **Operator action:**") for ln in block)

    def test_handoff_md_carries_generated_against_sha_header(self, tmp_path):
        """PR-15: handoff.md carries a
        ``**Generated against SHA:** \\`<short>\\` (full: \\`<long>\\`)``
        line in its header block. The SHA is the FINAL round's
        snapshot — what reviewers had as HEAD when they produced the
        findings the handoff describes.

        Also pins the position: the new line lands BEFORE
        ``**Rounds executed:**`` so it groups with the verdict /
        termination meta, not down with the exit-code-y fields.
        """
        from syncade.persistence import persist_handoff

        finding = self._blocker_finding(
            description="real code defect",
            file="src/foo.py",
        )
        sha = "abc123def456" + "0" * 28
        # Two rounds; the handoff should pin against the LAST round's
        # snapshot SHA. Round 0 carries a different SHA to make sure
        # we're reading rounds[-1], not rounds[0].
        rounds = [
            self._round_with_synth(0, [finding], commit_sha="f" * 40),
            self._round_with_synth(1, [finding], commit_sha=sha),
        ]
        run_dir = tmp_path / "run-sha-header"
        run_dir.mkdir()
        path = persist_handoff(
            run_dir,
            final_exit_code=30,
            final_round=1,
            termination_reason="producer_stalled",
            rounds=rounds,
            max_rounds=3,
        )
        assert path is not None
        text = path.read_text()
        assert "**Generated against SHA:** `abc123def456` (full: `" + sha + "`)" in text
        # Round 0's SHA must NOT appear in the header — confirms we
        # read rounds[-1], not rounds[0].
        assert "`ffffffffffff`" not in text.split("## ", 1)[0]
        # Position: SHA line lands before "Rounds executed".
        sha_idx = text.find("**Generated against SHA:**")
        rounds_idx = text.find("**Rounds executed:**")
        assert sha_idx != -1
        assert rounds_idx != -1
        assert sha_idx < rounds_idx
