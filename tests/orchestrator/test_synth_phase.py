"""Tests for :mod:`syncade.orchestrator`.

Uses :class:`FakeAdapter` exclusively via the ``adapter_factory``
parameter — no real CLI calls. Each test sets up an ephemeral git
repo in ``tmp_path`` so the snapshot + worktree provisioning steps
exercise real git, then injects fakes for the reviewer dispatch.

Total runtime under 5 seconds (the brief's bound).
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.findings import ReviewerOutputError
from syncade.orchestrator import run_review
from syncade.worktree import DEFAULT_WORKTREE_BASE
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _ship,
    _synth_with_blocker,
    _two_reviewer_config,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestSynthesizerPhase:
    """PR-7 task 5: the cold Codex synthesizer phase fires after the
    reviewer dispatch succeeds, is skipped when any reviewer fails,
    and its own failure modes map to 70 (parse) / 40 (subprocess) per
    the decision table."""

    def test_synthesizer_fires_when_both_reviewers_succeed(self, repo_with_pr_doc):
        """Happy path: both reviewers SHIP → synthesizer runs →
        run_result.synth_result is populated with the synthesizer's
        output."""
        from syncade.synthesizer import SynthesizerResult

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        synth_adapter = FakeSynthesizerAdapter()
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        assert isinstance(result.synth_result, SynthesizerResult)
        assert result.synth_result.output is not None
        assert result.synth_result.error is None
        # The fake's build_invocation was called exactly once — the
        # synthesizer is single-shot, not parallel.
        assert len(synth_adapter.invocations) == 1
        # QA fix P0.3: the recorded "worktree_path" is NOT repo_root —
        # it's an isolated tempdir workspace containing only the PR
        # doc (cold-isolation invariant). The workspace tempdir is
        # already cleaned up by run_synthesizer's `with
        # tempfile.TemporaryDirectory(...)` exit, so the path may
        # point at a no-longer-existing dir; we just assert it isn't
        # the repo root.
        recorded_path = synth_adapter.invocations[0][1]
        assert recorded_path != repo.resolve(), (
            "synth invocation's worktree_path arg leaked the repo "
            "root — cold-workspace isolation regressed"
        )
        # Tempdir name hints (prefix used by run_synthesizer)
        assert "syncade-synth-" in str(recorded_path)

    def test_synthesizer_skipped_when_any_reviewer_fails(self, repo_with_pr_doc):
        """One reviewer fails (invocation error) → synthesizer is
        skipped → run_result.synth_result is None. Preserves the "no
        silent N-1 degradation" rule from PR-5."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(
                canned_exception=ReviewerInvocationError(
                    "auth failed", returncode=1, stdout="", stderr=""
                )
            ),
        ]
        synth_adapter = FakeSynthesizerAdapter()
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        # Reviewer failure → exit 40 (reviewer failure), synth skipped.
        assert result.exit_code == 40
        assert result.synth_result is None
        # The synthesizer was never invoked.
        assert len(synth_adapter.invocations) == 0

    def test_synthesizer_skipped_when_reviewer_parse_fails(self, repo_with_pr_doc):
        """One reviewer's output didn't parse (ReviewerOutputError) →
        synthesizer skipped → exit 70, not synthesizer-influenced."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_exception=ReviewerOutputError("unparseable")),
        ]
        synth_adapter = FakeSynthesizerAdapter()
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        assert result.exit_code == 70
        assert result.synth_result is None
        assert len(synth_adapter.invocations) == 0

    def test_synthesizer_parse_failure_maps_to_exit_70(self, repo_with_pr_doc):
        """Both reviewers succeed → synthesizer runs → synth returns
        unparseable output (e.g. invented findings, unanimous-blocker
        dismissal) → exit 70. The synth result is preserved so
        persistence (PR-7 task 7) can write the error context."""
        from syncade.synthesis import SynthesizerOutputError

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        synth_adapter = FakeSynthesizerAdapter(
            canned_exception=SynthesizerOutputError(
                "synthesizer output had no parseable SynthesizerOutput JSON"
            )
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        assert result.exit_code == 70
        assert result.synth_result is not None
        assert isinstance(result.synth_result.error, SynthesizerOutputError)
        assert result.synth_result.output is None

    def test_synthesizer_subprocess_failure_maps_to_exit_40(self, repo_with_pr_doc):
        """Both reviewers succeed → synthesizer runs → synth's codex
        subprocess fails (auth, network, model unavailable) → exit 40.
        Same bucket as reviewer subprocess failures."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        synth_adapter = FakeSynthesizerAdapter(
            canned_exception=ReviewerInvocationError(
                "codex auth failed", returncode=1, stdout="", stderr=""
            )
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        assert result.exit_code == 40
        assert result.synth_result is not None
        assert isinstance(result.synth_result.error, ReviewerInvocationError)

    def test_synthesizer_sees_reviewer_outputs_in_prompt(self, repo_with_pr_doc):
        """The reviewer outputs reach the synthesizer's prompt as a
        labeled JSON blob (one section per reviewer). Captured via the
        fake's recorded prompt."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_no_ship()),
        ]
        synth_adapter = FakeSynthesizerAdapter()
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        recorded_prompt = synth_adapter.invocations[0][2]
        # Each reviewer's name appears as a labeled section header
        assert "rv1:" in recorded_prompt
        assert "rv2:" in recorded_prompt
        # The reviewer outputs are serialized inside the prompt — verdict
        # strings are visible.
        assert '"verdict": "SHIP"' in recorded_prompt
        assert '"verdict": "NO-SHIP"' in recorded_prompt
        # The synthesizer template's design invariants are in the prompt.
        assert "do not see the diff" in recorded_prompt.lower() or "do not receive" in (
            recorded_prompt.lower()
        )

    def test_synthesizer_runs_in_isolated_tempdir_workspace(self, repo_with_pr_doc):
        """QA fix P0.3: the synthesizer subprocess runs from an
        ISOLATED tempdir workspace, NOT from repo_root. The codex
        flags ``-C`` and ``--add-dir`` (built from this workspace_path
        arg) scope to the tempdir — codex's relative-file-access
        defaults stay out of the repo. This is the cold-isolation
        invariant the synthesizer-architecture-decision demands;
        without it, the synth has trivial read access to the diff
        and source files reviewers see, contradicting the "cold =
        no diff access" rule.

        The tempdir is cleaned up by run_synthesizer's `with
        tempfile.TemporaryDirectory(...)` block on exit, so the
        recorded path may not exist by the time we assert on it —
        we check it's NOT repo_root, has the syncade-synth- prefix
        (the prefix run_synthesizer uses), and is NOT under the
        repo's directory tree.

        R2.1 tightened this from advisory isolation to structural
        isolation: trusted permissions keep the codex sandbox active
        and scoped to this temp workspace.
        """
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        synth_adapter = FakeSynthesizerAdapter()
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        # The synth fake recorded the path passed as worktree_path —
        # that's the workspace tempdir, NOT repo_root.
        _, recorded_path, recorded_prompt = synth_adapter.invocations[0]
        assert recorded_path != repo.resolve(), "synth worktree_path leaked the repo root"
        # The workspace uses the syncade-synth- prefix
        assert "syncade-synth-" in str(recorded_path), (
            f"synth workspace doesn't carry the expected prefix; got {recorded_path!r}"
        )
        # And it's NOT located under the repo tree
        assert not str(recorded_path).startswith(str(repo.resolve())), (
            f"synth workspace landed under repo_root: {recorded_path!r}"
        )
        # The prompt's pr_doc_path placeholder points at the
        # WORKSPACE copy of the PR doc, not the original — cold
        # isolation means the model reads from the workspace.
        assert str(recorded_path) in recorded_prompt, (
            "prompt's pr_doc_path placeholder doesn't reference the "
            "workspace; the synth may still be pointed at the "
            "original repo location"
        )
        # The original PR doc path is NOT directly referenced in the
        # prompt (the synth shouldn't be pointed at it).
        assert str(pr_doc) not in recorded_prompt or str(pr_doc).startswith(str(recorded_path)), (
            "prompt still references the original repo-side pr_doc path"
        )
        # Sanity: no .syncade-side worktree dir was created for the synth.
        assert not (DEFAULT_WORKTREE_BASE / "synthesizer").exists()

    def test_synth_mechanical_verdict_ignores_reviewer_verdict_strings(self, repo_with_pr_doc):
        """Mechanical verdict invariant: even when both reviewers
        vote SHIP, the synth's mechanical decision drives the exit
        code.

        QA fix P0.2: the synth must reference real reviewer findings
        (provenance validator). To exercise "reviewers SHIP, synth
        elevates to blocker", use ``_no_ship()`` on one reviewer so
        a real finding exists for the synth to elevate — but the
        reviewer's `verdict` string is still ignored by the
        mechanical exit-code computation. The interesting invariant
        is "verdict comes from `consolidated_findings`, not from
        `reviewer.verdict`", which this test now exercises with a
        synth that ELEVATES the reviewer's blocker (severity matches,
        so no severity_change_rationale needed) and a SHIP reviewer
        whose verdict the orchestrator ignores."""
        repo, pr_doc = repo_with_pr_doc
        # One reviewer with a real finding (so the synth has something
        # to reference); the other SHIPs cleanly. The SHIP reviewer's
        # verdict is what the test pins as ignored.
        adapters = [
            FakeAdapter(canned_output=_no_ship()),  # has 1 finding at index 0
            FakeAdapter(canned_output=_ship()),  # SHIP, 0 findings — its verdict is ignored
        ]
        synth_adapter = FakeSynthesizerAdapter(
            canned_output=_synth_with_blocker(reviewer_name="rv1"),
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=synth_adapter,
        )
        # rv2 said SHIP; rv1 said NO-SHIP. The invariant the test pins is:
        # the verdict comes from
        # `synth.consolidated_findings`, not from any reviewer's verdict
        # string. The synth has one active blocker → exit 30.
        assert result.exit_code == 30

    def test_synth_dismissed_blockers_count_as_ship(self, repo_with_pr_doc):
        """Mechanical verdict: a blocker the synth DISMISSED with
        rationale does not count toward NO-SHIP.

        QA fix P0.2: rv1 needs a real finding for the synth's
        provenance to be valid. Using ``_no_ship()`` gives rv1 one
        finding at index 0.
        """
        from syncade.synthesis import (
            ConsolidatedFinding,
            FindingProvenance,
            SynthesizerOutput,
        )

        dismissed_synth = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="single-reviewer blocker (rv1)",
                    file="src/x.py",
                    severity="blocker",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="rv1",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing thing",
                        )
                    ],
                    dismissed=True,
                    dismissal_rationale="spec exempts this case in §3.2",
                )
            ],
            synthesis_summary="one finding, dismissed with rationale",
        )
        repo, pr_doc = repo_with_pr_doc
        # rv1 has 1 finding (index 0) → synth provenance is valid.
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=dismissed_synth),
        )
        # Dismissed blocker does not count → SHIP → exit 0.
        assert result.exit_code == 0

    def test_synth_active_minor_does_not_block_ship(self, repo_with_pr_doc):
        """Mechanical verdict: only ACTIVE BLOCKERS produce NO-SHIP.
        A minor or nit finding does not.

        QA fix P0.2: rv1 needs a real finding so the synth
        provenance is valid; use ``_no_ship()`` (which produces 1
        blocker finding at index 0). The synth then downgrades it
        to minor (severity differs from rv1's original blocker, but
        matches rv1's call type — wait: the synth's severity is
        "minor", rv1's original is "blocker". Synth differs from
        EVERY reviewer's original, so severity_change_rationale is
        required.).
        """
        from syncade.synthesis import (
            ConsolidatedFinding,
            FindingProvenance,
            SynthesizerOutput,
        )

        minor_synth = SynthesizerOutput(
            consolidated_findings=[
                ConsolidatedFinding(
                    description="downgraded to minor: comment-style issue",
                    file="src/x.py",
                    severity="minor",
                    provenance=[
                        FindingProvenance(
                            reviewer_name="rv1",
                            original_severity="blocker",
                            original_index=0,
                            original_description="missing thing",
                        )
                    ],
                    severity_change_rationale=(
                        "downgraded from blocker: the affected code path is only hit in dev mode"
                    ),
                )
            ],
            synthesis_summary="one minor finding, no blockers",
        )
        repo, pr_doc = repo_with_pr_doc
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=minor_synth),
        )
        # Minor-only consolidated findings → SHIP.
        assert result.exit_code == 0
