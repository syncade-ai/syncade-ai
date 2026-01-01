"""PR-v2-11 budget guardrails (Issue 2): the tally + phase-boundary abort.

Two layers:

- Unit tests for :mod:`syncade.orchestrator.budget` — all of the decision logic
  (``over_budget``'s token/dollar branches and ``>=`` semantics; the lower-bound
  dollar tally; ``round_usages`` / ``review_usages`` collection).
- Integration tests that drive the REAL ``run_review`` loop and prove the wiring:
  a crossed ceiling breaks the loop with exit 25 (``BUDGET_EXCEEDED``), the
  round is persisted whole, and overshoot is bounded to one review-bundle or one
  producer (the producer is skipped when the reviewers already blew the budget;
  the judge stays IN the review-bundle so the aborted round keeps its verdict).

Actor usage is injected by patching ``usage_for`` at its three call sites — the
fake reviewers use provider ``fake1``/``fake2``, for which the real ``usage_for``
returns ``None``. The extraction itself is covered by ``tests/test_usage.py``;
here we only need deterministic usage so the tally logic is under test, not the
envelope parsing.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from syncade import billing
from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.config_loop import LoopConfig
from syncade.exit_codes import BUDGET_EXCEEDED
from syncade.orchestrator import run_review
from syncade.orchestrator.budget import over_budget, review_usages, round_usages
from syncade.usage import Usage
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_clean,
    _synth_with_blocker,
)

# The integration tests spawn a real worktree/branch loop; skip if git is absent
# (mirrors the guard the other loop-integration modules use).
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)

_ABSENT = object()  # sentinel: the sub-result object itself is None (no synth/producer ran)


def _u(tokens: int = 0, cost: float | None = None, source: str = "estimated") -> Usage:
    """A Usage carrying ``tokens`` total (all input) and an optional known cost."""
    return Usage(model="m", input_tokens=tokens, output_tokens=0, cost_usd=cost, cost_source=source)


# --- Unit: over_budget -------------------------------------------------------


class TestOverBudget:
    def test_no_budget_configured_is_noop(self):
        # No ceiling set → never trips, even for an enormous tally. This is what makes the
        # loop wiring a no-op for the vast majority of (unbudgeted) runs.
        assert over_budget([_u(tokens=10**9, cost=10.0**9)], LoopConfig()) is None

    def test_empty_usages_never_trips(self):
        assert over_budget([], LoopConfig(budget_tokens=1, budget_usd=0.01)) is None

    def test_token_ceiling_trips_at_or_over(self):
        # ``>=``: exactly at the ceiling stops (the next phase would spend past it).
        assert over_budget([_u(tokens=99)], LoopConfig(budget_tokens=100)) is None
        assert over_budget([_u(tokens=100)], LoopConfig(budget_tokens=100)) == "budget_tokens"
        assert over_budget([_u(tokens=101)], LoopConfig(budget_tokens=100)) == "budget_tokens"

    def test_token_tally_sums_across_actors(self):
        usages = [_u(tokens=60), _u(tokens=60)]
        assert over_budget(usages, LoopConfig(budget_tokens=100)) == "budget_tokens"

    def test_dollar_ceiling_is_a_lower_bound(self):
        # An unpriced actor (cost_usd=None) contributes nothing, so the dollar tally is a
        # LOWER BOUND: a run whose UNKNOWN cost would exceed the ceiling is NOT caught.
        assert over_budget([_u(cost=0.40), _u(cost=None)], LoopConfig(budget_usd=0.50)) is None
        # Known cost alone crosses → trips.
        assert (
            over_budget([_u(cost=0.40), _u(cost=0.20)], LoopConfig(budget_usd=0.50)) == "budget_usd"
        )

    def test_tokens_checked_before_dollars(self):
        # Both ceilings would trip; the token one is reported first.
        both = LoopConfig(budget_tokens=100, budget_usd=1.0)
        assert over_budget([_u(tokens=100, cost=100.0)], both) == "budget_tokens"

    def test_c1_dollar_tally_equals_billing_api_equiv(self):
        """C1: the number ``--budget-usd`` compares against IS ``billing.api_equiv`` — the same
        accounting ``--metrics`` and ``summary.md`` render — so the budget can't trip at a
        total those surfaces would report differently (incl. the unpriced/lower-bound case)."""
        usages = [_u(cost=0.30), _u(cost=0.30), _u(cost=None, source="unknown")]
        budget_sum = sum(u.cost_usd for u in usages if u.cost_usd is not None)
        assert budget_sum == pytest.approx(billing.from_usages(usages).api_equiv)


# --- Unit: round_usages / review_usages --------------------------------------


def _round_result(reviewer_usages, synth_usage, producer_usage):
    """A minimal RoundResult stand-in exposing only what the collectors read."""
    disp = type("D", (), {"results": [type("R", (), {"usage": u})() for u in reviewer_usages]})()
    synth = None if synth_usage is _ABSENT else type("S", (), {"usage": synth_usage})()
    prod = None if producer_usage is _ABSENT else type("P", (), {"usage": producer_usage})()
    return type(
        "RR", (), {"dispatch_result": disp, "synth_result": synth, "producer_result": prod}
    )()


class TestUsageCollection:
    def test_review_excludes_producer_round_includes_it(self):
        r1, r2, s, p = _u(1), _u(2), _u(3), _u(99)
        rr = _round_result([r1, r2], s, p)
        assert review_usages(rr) == [r1, r2, s]
        assert round_usages(rr) == [r1, r2, s, p]

    def test_none_usage_values_are_skipped(self):
        # rv2 reported no usage; no producer ran.
        r1, s = _u(1), _u(3)
        rr = _round_result([r1, None], s, _ABSENT)
        assert review_usages(rr) == [r1, s]
        assert round_usages(rr) == [r1, s]

    def test_absent_synth_and_producer(self):
        # synth_result / producer_result themselves are None (phase never produced a result).
        r1 = _u(1)
        rr = _round_result([r1], _ABSENT, _ABSENT)
        assert review_usages(rr) == [r1]
        assert round_usages(rr) == [r1]

    def test_synth_result_present_but_usage_none(self):
        r1 = _u(1)
        rr = _round_result([r1], None, _ABSENT)  # synth ran but reported no usage
        assert round_usages(rr) == [r1]


# --- Integration: the real loop aborts at a phase boundary -------------------


def _budget_config(max_rounds: int, **budget) -> SyncadeConfig:
    return SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": max_rounds, **budget},
    )


def _patch_usage(monkeypatch, *, reviewer: Usage, synth: Usage, producer: Usage) -> None:
    """Force each actor's ``usage_for`` to return a fixed Usage regardless of envelope."""
    import syncade.dispatcher as disp_mod
    import syncade.producer as prod_mod
    import syncade.synthesizer.driver as synth_mod

    monkeypatch.setattr(disp_mod, "usage_for", lambda *a, **k: reviewer)
    monkeypatch.setattr(synth_mod, "usage_for", lambda *a, **k: synth)
    monkeypatch.setattr(prod_mod, "usage_for", lambda *a, **k: producer)


class TestBudgetAbortsLoop:
    def test_pre_producer_abort_on_tokens(self, repo_with_pr_doc, monkeypatch):
        """Round 0's reviewers blow ``--budget-tokens`` → abort BEFORE the producer:
        exit 25, producer never invoked (overshoot bounded to the review phase), no round-1."""
        repo, pr_doc = repo_with_pr_doc
        _patch_usage(
            monkeypatch, reviewer=_u(tokens=1000), synth=_u(tokens=500), producer=_u(tokens=9)
        )
        producer = FakeProducerAdapter(commit_message="fix: must not run")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_budget_config(max_rounds=2, budget_tokens=1),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_no_ship()),
            ),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
            producer_adapter=producer,
        )
        assert result.exit_code == BUDGET_EXCEEDED
        assert result.termination_reason == "budget_exceeded"
        assert result.final_round == 0
        assert producer.invocations == []  # producer skipped
        assert producer.parse_output_calls == 0
        round_dirs = sorted(d.name for d in result.artifacts.run_dir.glob("round-*"))
        assert round_dirs == ["round-0"]
        # Finding 5, before-PRODUCER path: the crossed ceiling (tokens) is named + marked.
        summary = (result.artifacts.run_dir / "loop-summary.md").read_text()
        assert "budget exceeded (token ceiling)" in summary  # headline names the ceiling too
        assert "TOKEN tally crossed" in summary
        assert "reported no usage)  ← CROSSED" in summary

    def test_pre_round_abort_on_dollars(self, repo_with_pr_doc, monkeypatch):
        """Round 0's reviewers are UNDER ``--budget-usd`` (so the producer runs), but the
        producer's cost pushes the tally over; round 1 is refused at its boundary → exit 25.
        Proves the before-ROUND check (a prior round's producer crossed the ceiling) and the
        dollar path end-to-end."""
        repo, pr_doc = repo_with_pr_doc
        # reviewers 0.10 each (0.20) + synth 0.05 = 0.25 < 0.50 → producer runs;
        # producer 1.00 → round-0 total 1.25 >= 0.50 → round 1 refused.
        _patch_usage(
            monkeypatch, reviewer=_u(cost=0.10), synth=_u(cost=0.05), producer=_u(cost=1.00)
        )
        producer = FakeProducerAdapter(commit_message="fix: round 0 producer")
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_budget_config(max_rounds=2, budget_usd=0.50),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_ship()),
                FakeAdapter(canned_output=_ship()),
            ),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
            producer_adapter=producer,
        )
        assert result.exit_code == BUDGET_EXCEEDED
        assert result.termination_reason == "budget_exceeded"
        assert result.final_round == 0
        assert len(producer.invocations) == 1  # round-0 producer DID run
        round_dirs = sorted(d.name for d in result.artifacts.run_dir.glob("round-*"))
        assert round_dirs == ["round-0"]  # round 1 never started
        # Finding 5, before-ROUND path: the crossed ceiling ($) is named in the Budget section.
        summary = (result.artifacts.run_dir / "loop-summary.md").read_text()
        assert "budget exceeded (cost ceiling)" in summary  # headline names the ceiling too
        assert "COST tally crossed" in summary
        assert "LOWER-BOUND tally)  ← CROSSED" in summary

    def test_budget_summary_and_terminal_are_honest(self, repo_with_pr_doc, monkeypatch, capsys):
        """C4 + surfacing: a SUBSCRIPTION run whose judge cost is UNPRICED renders the
        loop-summary Budget section as a LOWER BOUND (naming the unpriced tokens), never
        fake-exact spend or implied billing, and the terminal prints the API-equivalent notice."""
        repo, pr_doc = repo_with_pr_doc
        _patch_usage(
            monkeypatch,
            reviewer=Usage(
                model="gpt-5.5",
                input_tokens=9000,
                output_tokens=3000,
                cost_usd=0.30,
                cost_source="estimated",
                auth_mode="subscription",
            ),
            synth=Usage(
                model="gpt-5.5",
                input_tokens=4000,
                output_tokens=1000,
                cost_usd=None,  # unpriced → its tokens can't be priced into the dollar tally
                cost_source="unknown",
                auth_mode="subscription",
            ),
            producer=_u(tokens=1),
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_budget_config(max_rounds=2, budget_usd=0.50),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_no_ship()),
            ),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
            producer_adapter=FakeProducerAdapter(commit_message="fix: must not run"),
        )
        assert result.exit_code == BUDGET_EXCEEDED

        summary = (result.artifacts.run_dir / "loop-summary.md").read_text()
        assert "## Budget" in summary
        assert "budget_usd" in summary  # names the configured ceiling
        assert "LOWER BOUND" in summary  # C4: unpriced judge → the dollar tally is a lower bound
        assert "5000 tokens unpriced" in summary  # names the incomplete tokens
        assert "$0.0000" in summary  # billed: subscription spent no real money
        assert "$0.6000" in summary  # API-equiv valuation (2 reviewers x 0.30)

        combined = "".join(capsys.readouterr())  # (out, err)
        assert "budget exceeded" in combined
        assert "not billed money" in combined  # the terminal never implies real spend

        # C2 surface: status.json is finalized to the budget reason (not left "running").
        status = json.loads((result.artifacts.run_dir / "status.json").read_text())
        assert status["state"] == "terminated"
        assert status["reason"] == "budget_exceeded"
        assert status["exit_code"] == BUDGET_EXCEEDED

    def test_unbudgeted_run_is_unaffected(self, repo_with_pr_doc, monkeypatch):
        """C5: same enormous usage, NO budget configured → the loop runs normally (round 0
        ships, exit 0), AND the only artifact delta is the run-init config-echo. Non-config
        artifacts (loop-summary, loop-manifest) gain NO budget field on the no-budget path."""
        repo, pr_doc = repo_with_pr_doc
        _patch_usage(
            monkeypatch, reviewer=_u(tokens=10**9), synth=_u(tokens=10**9), producer=_u(tokens=9)
        )
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_budget_config(max_rounds=2),  # no budget_tokens / budget_usd
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_ship()),
                FakeAdapter(canned_output=_ship()),
            ),
            synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
        )
        assert result.exit_code == 0
        assert result.termination_reason == "ship"

        run_dir = result.artifacts.run_dir
        # Non-config artifacts stay clean: no Budget section, no budget key in the loop manifest.
        assert "## Budget" not in (run_dir / "loop-summary.md").read_text()
        assert "budget" not in (run_dir / "loop-manifest.json").read_text().lower()
        # The ONE benign delta (C5): run-init's config_snapshot echoes the unset budget fields
        # as null — the same full-schema model_dump that already echoed test_timeout_seconds,
        # and load-bearing for resume budget inheritance (Finding 2).
        snap_loop = json.loads((run_dir / "run-init.json").read_text())["config_snapshot"]["loop"]
        assert snap_loop["budget_tokens"] is None and snap_loop["budget_usd"] is None
