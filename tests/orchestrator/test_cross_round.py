"""Cross-round context wiring tests (part 1 of 2): round-0 sentinels,
adversarial-lens routing, and round-1 reviewer/producer prior-round
threading.

Moved verbatim from the former ``tests/test_orchestrator.py`` monolith.
"""

from __future__ import annotations

import subprocess

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

    def test_round_0_reviewer_gets_default_prior_round_sentinel(self, repo_with_pr_doc):
        """Round 0 has no prior round. Each reviewer's prompt must
        contain the documented sentinel
        ``"(no prior round — this is round 0)"`` substituted into the
        ``{prior_round_output}`` placeholder. Belt-and-braces on the
        wiring: round 0 omits ``prior_round_output`` when rendering each
        reviewer's per-provider prompt, so the renderer's default
        sentinel is what lands."""
        repo, pr_doc = repo_with_pr_doc
        # Single round, NO-SHIP — only round 0 runs
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=1),
            adapter_factory=_factory_returning(*adapters),
        )
        # Each reviewer's adapter saw one invocation; the prompt is
        # the third element of the recorded tuple.
        for adapter in adapters:
            assert len(adapter.invocations) == 1, (
                "expected exactly one build_invocation per reviewer on round 0"
            )
            prompt = adapter.invocations[0][2]
            assert "(no prior round — this is round 0)" in prompt, (
                "round 0 reviewer prompt must contain the documented "
                "default sentinel substituted into the "
                "{prior_round_output} placeholder"
            )

    def test_each_provider_gets_its_own_reviewer_template(self, repo_with_pr_doc):
        """The anthropic reviewer renders ``reviewer_adversarial.md`` and the openai
        reviewer renders ``reviewer_codex.md`` — distinct role framing per
        provider. Adapters are consumed in config order, so adapters[0] sees
        the adversarial prompt and adapters[1] the codex prompt."""
        repo, pr_doc = repo_with_pr_doc
        config = SyncadeConfig(
            reviewers=[
                {"name": "claude-reviewer", "provider": "anthropic", "model": "x"},
                {"name": "codex-reviewer", "provider": "openai", "model": "y"},
            ],
            loop={"max_rounds": 1},
        )
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*adapters),
        )
        claude_prompt = adapters[0].invocations[0][2]
        codex_prompt = adapters[1].invocations[0][2]
        assert "adversarial acceptance audit" in claude_prompt
        assert "high-signal adversarial review" in codex_prompt
        assert "high-signal adversarial review" not in claude_prompt
        assert "adversarial acceptance audit" not in codex_prompt

    def test_adversarial_lens_only_reaches_flagged_reviewer(self, repo_with_pr_doc):
        """Round-0 wiring for the adversarial lens: a reviewer configured with
        ``adversarial_lens=True`` gets the enumeration block in its dispatched
        prompt; the other (the control) does NOT. Order-independent: assert
        EXACTLY ONE prompt carries the block."""
        repo, pr_doc = repo_with_pr_doc
        config = SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x", "adversarial_lens": True},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 1},
        )
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*adapters),
        )
        prompts = [a.invocations[0][2] for a in adapters]
        with_block = [p for p in prompts if "Adversarial edge enumeration" in p]
        assert len(with_block) == 1, (
            "exactly one reviewer (the adversarial_lens one) must receive the "
            "enumeration block on round 0"
        )

    def test_no_lens_round0_neither_prompt_has_block(self, repo_with_pr_doc):
        """Control / back-compat: with no reviewer flagged, round 0 renders the
        shared prompt and NEITHER reviewer sees the adversarial block (codex's
        prompt is unchanged from pre-fix)."""
        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=1),
            adapter_factory=_factory_returning(*adapters),
        )
        for adapter in adapters:
            assert "Adversarial edge enumeration" not in adapter.invocations[0][2]

    def test_codex_reviewer_adv_renders_adversarial_prompt_with_lens(self, repo_with_pr_doc):
        """A codex (openai) reviewer pinned to reviewer_adversarial.md with
        adversarial_lens=true dispatches the acceptance-audit content WITH the
        edge-enumeration block; the plain codex reviewer gets neither."""
        repo, pr_doc = repo_with_pr_doc
        config = SyncadeConfig(
            reviewers=[
                {"name": "codex-reviewer", "provider": "openai", "model": "gpt-5.5"},
                {
                    "name": "codex-reviewer-adv",
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "template": "reviewer_adversarial.md",
                    "adversarial_lens": True,
                },
            ],
            loop={"max_rounds": 1},
        )
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*adapters),
        )
        plain = adapters[0].invocations[0][2]
        adv = adapters[1].invocations[0][2]
        assert "high-signal adversarial review" in plain
        assert "Adversarial edge enumeration" not in plain
        assert "adversarial acceptance audit" in adv
        assert "Adversarial edge enumeration" in adv

    def test_round_1_reviewer_gets_round_0_response(self, repo_with_pr_doc, monkeypatch):
        """Round 1 reviewers must receive their OWN prior-round
        response text substituted into the prompt. Monkey-patches the
        helper to return canned text; verifies the round-1 reviewer's
        prompt carries the canned text."""
        import syncade.orchestrator.round as round_module

        recorded_calls: list[dict] = []
        canned_prior = "ROUND-0 REVIEWER RESPONSE TEXT (canned for the test)"

        def fake_loader(*, prior_round_dir, reviewer_name, reviewer_provider):
            recorded_calls.append(
                {
                    "round_dir_basename": prior_round_dir.name,
                    "reviewer_name": reviewer_name,
                    "reviewer_provider": reviewer_provider,
                }
            )
            return canned_prior

        monkeypatch.setattr(round_module, "load_prior_reviewer_response_text", fake_loader)

        repo, pr_doc = repo_with_pr_doc
        from syncade.adapters.fake import FakeProducerAdapter

        # Round 0 reviewers (NO-SHIP) + round 1 reviewers (SHIP) —
        # factory hands them out in order.
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

        # Round 0 must not have called the loader (round_idx == 0).
        # Round 1 must have called it once per reviewer.
        assert len(recorded_calls) == 2, (
            f"expected exactly 2 prior-loader calls (one per reviewer in "
            f"round 1); got {len(recorded_calls)}"
        )
        call_names = {c["reviewer_name"] for c in recorded_calls}
        assert call_names == {"rv1", "rv2"}, (
            f"prior-loader must be called once per reviewer by name; got {call_names}"
        )
        # Each call reads from round-0's dir.
        for c in recorded_calls:
            assert c["round_dir_basename"] == "round-0", (
                f"round-1 prior-loader must read from round-0/, got {c['round_dir_basename']}"
            )

        # Round-1 reviewer prompts (adapters[2], adapters[3]) must
        # contain the canned prior text. Round-0 (adapters[0], adapters[1])
        # must NOT (they get the default sentinel).
        for adapter in adapters[:2]:
            assert canned_prior not in adapter.invocations[0][2], (
                "round-0 reviewer prompt must NOT contain prior-round "
                "text — round 0 has no prior round"
            )
            assert "(no prior round — this is round 0)" in adapter.invocations[0][2]
        for adapter in adapters[2:]:
            assert canned_prior in adapter.invocations[0][2], (
                "round-1 reviewer prompt must contain the canned prior-round response text"
            )

    def test_round_1_producer_gets_round_0_response_and_commits(
        self, repo_with_pr_doc, monkeypatch
    ):
        """Round 1 producer must receive its OWN prior-round response
        text AND the prior round's commit subjects substituted into
        the producer prompt's ``{prior_round_output}`` and
        ``{prior_round_commits}`` placeholders."""
        import syncade.orchestrator.producer_phase as pp_module

        canned_prior_output = "ROUND-0 PRODUCER NARRATIVE (canned for the test)"
        canned_prior_commits = "fix: round-0 commit subject (canned)"

        def fake_response_loader(*, prior_round_dir):
            return canned_prior_output

        def fake_commits_loader(*, prior_round_dir, repo_root):
            return canned_prior_commits

        monkeypatch.setattr(pp_module, "load_prior_producer_response_text", fake_response_loader)
        monkeypatch.setattr(pp_module, "load_prior_producer_commit_subjects", fake_commits_loader)

        repo, pr_doc = repo_with_pr_doc
        from syncade.adapters.fake import FakeProducerAdapter

        # Three rounds; round 0 + round 1 NO-SHIP (each runs producer),
        # round 2 SHIPs. Producer is called twice; we want to inspect
        # round-1's producer prompt (second producer call).
        adapters = [
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ]
        producer = FakeProducerAdapter(commit_message="fix: producer commit")
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=self._multi_round_config(max_rounds=3),
            adapter_factory=_factory_returning(*adapters),
            synthesizer_adapter=_RoundCyclingSynth(
                _synth_with_blocker(),
                _synth_with_blocker(),
                _synth_clean(),
            ),
            producer_adapter=producer,
        )

        # Producer ran twice (round 0 + round 1). Round 0's prompt has
        # the round-0 (default) sentinels; round 1's prompt has the
        # canned prior text.
        assert len(producer.invocations) == 2, (
            f"expected 2 producer invocations (rounds 0 + 1); got {len(producer.invocations)}"
        )
        round_0_prompt = producer.invocations[0][2]
        round_1_prompt = producer.invocations[1][2]
        assert "(no prior round — this is round 0)" in round_0_prompt
        assert "(no prior commits)" in round_0_prompt
        assert canned_prior_output not in round_0_prompt
        assert canned_prior_commits not in round_0_prompt
        # Round 1 producer prompt has the canned prior text + commits.
        assert canned_prior_output in round_1_prompt, (
            "round-1 producer prompt must contain the canned prior-round producer response text"
        )
        assert canned_prior_commits in round_1_prompt, (
            "round-1 producer prompt must contain the canned prior-round commit subjects"
        )
