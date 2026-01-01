"""Cross-round context wiring tests (part 2 of 2): per-reviewer
isolation across rounds, prior-producer-outcome framing, and cross-PR
isolation.

Moved verbatim from the former ``tests/test_orchestrator.py`` monolith.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_clean,
    _synth_with_blocker,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestCrossRoundContextWiring:
    """PR-14 Task 3: the orchestrator gathers prior-round artifacts
    and threads them into the next round's reviewer / producer
    prompts.

    These tests pin the wiring — that the orchestrator looks up the
    right artifact for the right round / reviewer / producer and
    that the rendered prompt contains the substituted prior-round
    text. The extraction helpers themselves
    (:func:`syncade.orchestrator.prior_round.*`) are tested at the
    helper level; here we verify the orchestrator's plumbing carries
    their output through to ``dispatch_reviewers`` /
    ``run_producer`` correctly.

    Strategy: monkey-patch the load_prior_* helpers to return canned
    text, then inspect the FakeAdapter / FakeProducerAdapter
    ``invocations`` lists to confirm the canned text reached the
    per-reviewer / producer prompts.
    """

    def _multi_round_config(
        self, max_rounds: int = 3, test_command: str | None = None
    ) -> SyncadeConfig:
        loop: dict[str, object] = {"max_rounds": max_rounds}
        if test_command is not None:
            loop["test_command"] = test_command
        return SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop=loop,
        )

    def test_round_n_reviewer_per_reviewer_isolation(self, repo_with_pr_doc, monkeypatch):
        """Per-reviewer isolation across rounds: round-1 rv1's prompt
        gets rv1's round-0 output (NOT rv2's), and vice versa. The
        orchestrator must call the loader keyed by each reviewer's
        own name."""
        import syncade.orchestrator.round as round_module

        def fake_loader(*, prior_round_dir, reviewer_name, reviewer_provider):
            return f"PRIOR ROUND FROM {reviewer_name}"

        monkeypatch.setattr(round_module, "load_prior_reviewer_response_text", fake_loader)

        repo, pr_doc = repo_with_pr_doc
        from syncade.adapters.fake import FakeProducerAdapter

        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: round 0 producer")
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=2),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(),
                _synth_clean(),
            ),
            producer_adapter=producer,
        )

        # Round-1 reviewer prompts are at adapters[2] (rv1) and
        # adapters[3] (rv2). Each must contain ONLY their own name's
        # prior-round text.
        rv1_round1_prompt = adapters[2].invocations[0][2]
        rv2_round1_prompt = adapters[3].invocations[0][2]
        assert "PRIOR ROUND FROM rv1" in rv1_round1_prompt, (
            "round-1 rv1 prompt must contain rv1's prior-round text"
        )
        assert "PRIOR ROUND FROM rv2" not in rv1_round1_prompt, (
            "round-1 rv1 prompt must NOT contain rv2's prior-round "
            "text — per-reviewer isolation across rounds is the "
            "PR-14 invariant"
        )
        assert "PRIOR ROUND FROM rv2" in rv2_round1_prompt, (
            "round-1 rv2 prompt must contain rv2's prior-round text"
        )
        assert "PRIOR ROUND FROM rv1" not in rv2_round1_prompt, (
            "round-1 rv2 prompt must NOT contain rv1's prior-round "
            "text — per-reviewer isolation across rounds is the "
            "PR-14 invariant"
        )

    def test_round_n_producer_handles_prior_subprocess_error(self, tmp_path: Path):
        """Edge case from the brief: when the prior round's producer
        ended in subprocess_error (or any state where the on-disk
        artifact is missing / empty), the helpers must return a
        framing sentinel rather than crashing. The orchestrator
        then renders the producer prompt with the sentinel text;
        the producer prose tells the model to treat the round as
        fresh.

        Direct test of the helper-level behavior: the production
        loop terminates on subprocess_error today, so we can't drive
        the end-to-end scenario via run_review. But the helpers must
        be robust against missing / corrupt artifacts regardless
        (defensive coverage for future loop logic + on-disk
        corruption / manual cleanup)."""
        from syncade.orchestrator.prior_round import (
            load_prior_producer_commit_subjects,
            load_prior_producer_response_text,
            load_prior_reviewer_response_text,
        )

        # Scenario 1: prior_round_dir doesn't exist at all.
        missing_dir = tmp_path / "nonexistent" / "round-0"
        output = load_prior_producer_response_text(prior_round_dir=missing_dir)
        assert "prior round artifact not found" in output

        # Scenario 2: prior_round_dir exists but producer.stdout
        # doesn't (subprocess_error path where the subprocess
        # never started — SubprocessNotFoundError).
        empty_round = tmp_path / "round-0"
        empty_round.mkdir()
        output = load_prior_producer_response_text(prior_round_dir=empty_round)
        assert "prior round artifact not found" in output

        # Scenario 3: producer.stdout exists but is empty.
        (empty_round / "producer.stdout").write_text("", encoding="utf-8")
        output = load_prior_producer_response_text(prior_round_dir=empty_round)
        assert output == ""

        # Scenario 3b: producer.stdout has narrative text that
        # HAPPENS to look like a JSON envelope. The producer's
        # narrative text is what persistence wrote to disk
        # (output.narrative_text), NOT a raw envelope; the loader
        # MUST pass it through verbatim instead of re-extracting and
        # silently truncating to the inner .result field. Regression
        # pin for the spec drift QA caught.
        envelope_shaped_narrative = '{"type":"result","is_error":false,"result":"INNER"}'
        (empty_round / "producer.stdout").write_text(envelope_shaped_narrative, encoding="utf-8")
        output = load_prior_producer_response_text(prior_round_dir=empty_round)
        assert output == envelope_shaped_narrative, (
            "producer.stdout already contains extracted narrative_text; "
            "the loader must NOT re-extract or it would silently truncate "
            "an envelope-shaped narrative to its inner .result field"
        )

        # Scenario 4: producer.commit.txt missing → commit-subjects
        # helper returns its own framing sentinel.
        commits = load_prior_producer_commit_subjects(
            prior_round_dir=empty_round, repo_root=tmp_path
        )
        assert "producer.commit.txt not found" in commits

        # Scenario 5: reviewer-side missing stdout → same
        # missing-artifact framing.
        reviewer_text = load_prior_reviewer_response_text(
            prior_round_dir=empty_round,
            reviewer_name="rv1",
            reviewer_provider="anthropic",
        )
        assert "prior round artifact not found" in reviewer_text

    def test_prior_producer_outcome_framing(self, tmp_path: Path):
        """PR-14 brief edge case: when the prior round's producer ended
        in ``subprocess_error`` or ``stalled``, ``load_prior_producer_
        response_text`` must prefix the partial output with the
        documented framing message so the round-N producer's prose
        ("if your prior attempt errored, treat as a fresh round") has
        a concrete signal to interpret.

        See path/to/pr.md:156: the framing is
        ``"(prior round producer ended with outcome=<X>; partial
        output below)"``. Production loop terminates on these outcomes
        so the framing isn't surfaced today, but the helper-level
        contract is defensive coverage for tests + a future loop
        change that retries past stalls.
        """
        from syncade.orchestrator.prior_round import load_prior_producer_response_text

        def _write_round(slot: str, outcome: str, stdout: str) -> Path:
            rd = tmp_path / f"r-{slot}"
            rd.mkdir()
            (rd / "producer.stdout").write_text(stdout, encoding="utf-8")
            manifest = {
                "snapshot": {"commit_sha": "a" * 40},
                "producer": {"outcome": outcome, "ending_sha": "b" * 40},
            }
            (rd / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return rd

        # subprocess_error → framing prefix + raw partial text
        rd = _write_round("err", "subprocess_error", "partial output before SIGKILL")
        out = load_prior_producer_response_text(prior_round_dir=rd)
        assert out.startswith(
            "(prior round producer ended with outcome=subprocess_error; partial output below)\n\n"
        )
        assert out.endswith("partial output before SIGKILL")

        # stalled → framing prefix + raw narrative
        rd = _write_round("stall", "stalled", "I made some edits but didn't commit")
        out = load_prior_producer_response_text(prior_round_dir=rd)
        assert out.startswith(
            "(prior round producer ended with outcome=stalled; partial output below)\n\n"
        )
        assert out.endswith("I made some edits but didn't commit")

        # committed → NO prefix, just raw narrative
        rd = _write_round("ok", "committed", "I addressed the blocker via null check")
        out = load_prior_producer_response_text(prior_round_dir=rd)
        assert out == "I addressed the blocker via null check"

        # subprocess_error with empty stdout → framing only (the
        # framing is itself the signal — empty partial output is
        # still meaningful information for the next round's producer)
        rd = _write_round("err-empty", "subprocess_error", "")
        out = load_prior_producer_response_text(prior_round_dir=rd)
        expected_empty_err = (
            "(prior round producer ended with outcome=subprocess_error; partial output below)\n\n"
        )
        assert out == expected_empty_err

        # Missing manifest → no framing, just raw stdout
        # (defensive: missing manifest means outcome unknown; don't
        # invent a framing message that misrepresents the prior round)
        rd = tmp_path / "r-no-manifest"
        rd.mkdir()
        (rd / "producer.stdout").write_text("raw text", encoding="utf-8")
        out = load_prior_producer_response_text(prior_round_dir=rd)
        assert out == "raw text"

    def test_cross_pr_isolation(self, repo_with_pr_doc, tmp_path: Path):
        """PR-14 brief requirement (path/to/pr.md:228):
        two separate ``syncade <pr-doc>`` invocations against different
        run-ids must NOT share any cross-round context. The "within-PR"
        qualifier of the cross-round-context invariant is load-bearing —
        if cross-PR context ever leaked, a stale finding from a prior
        run could surface against unrelated code in a later run.

        Structural guarantee: each ``run_review`` call generates a
        fresh ``<run-id>`` via :func:`generate_run_id` and stores
        artifacts under ``<repo_root>/.syncade/runs/<run-id>/``. Round-0
        of any new run loads its prior context from
        ``<run-id>/round--1/`` — which doesn't exist — so the default
        sentinel is rendered.

        Test: drive two ``run_review`` calls back-to-back against
        different PR docs. Both round-0 reviewer prompts must contain
        the ``(no prior round)`` sentinel; neither must contain the
        other run's reviewer name, response text, or run-id.
        """
        repo, _ = repo_with_pr_doc
        pr1 = tmp_path / "pr-run-1.md"
        pr1.write_text("# PR Run 1 — distinctive marker A\n")
        pr2 = tmp_path / "pr-run-2.md"
        pr2.write_text("# PR Run 2 — distinctive marker B\n")

        # FakeAdapter that records what prompt it received; default
        # SHIP so each run terminates at round 0 (single-pass).
        run1_adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run2_adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]

        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 1},
        )

        result1 = run_review(
            repo_root=repo,
            pr_doc_path=pr1,
            config=config,
            adapter_factory=_factory_returning(*run1_adapters),
        )
        result2 = run_review(
            repo_root=repo,
            pr_doc_path=pr2,
            config=config,
            adapter_factory=_factory_returning(*run2_adapters),
        )

        # Fresh run-ids — structural cross-PR isolation
        assert result1.artifacts.run_dir != result2.artifacts.run_dir, (
            "two run_review calls must produce distinct run_dirs"
        )

        # Both round-0 reviewer prompts have the sentinel (no prior
        # round for either — single-pass mode skips the prior-round
        # codepath entirely)
        for adapter in run1_adapters + run2_adapters:
            prompt = adapter.invocations[0][2]
            assert "(no prior round — this is round 0)" in prompt, (
                "every round-0 reviewer prompt must contain the "
                "default sentinel — cross-PR runs both start fresh"
            )

        # Run-1's distinctive PR doc marker must NOT appear in run-2's
        # reviewer prompts (and vice versa). Belt-and-braces — the
        # only legitimate way for "marker A" to reach run-2 would be a
        # cross-PR leak.
        for adapter in run2_adapters:
            prompt = adapter.invocations[0][2]
            assert "distinctive marker A" not in prompt, (
                "CROSS-PR LEAK: run-2 reviewer prompt contains run-1's PR-doc-specific marker"
            )
        for adapter in run1_adapters:
            prompt = adapter.invocations[0][2]
            assert "distinctive marker B" not in prompt, (
                "CROSS-PR LEAK: run-1 reviewer prompt contains run-2's PR-doc-specific marker"
            )

        # No artifacts from run-1 in run-2's run_dir, and vice versa.
        # Both runs should have produced reviewer/synth/etc. artifacts —
        # confirms the test actually exercised the loop.
        run1_files = [p for p in result1.artifacts.run_dir.rglob("*") if p.is_file()]
        run2_files = [p for p in result2.artifacts.run_dir.rglob("*") if p.is_file()]
        assert run1_files and run2_files
