"""The budget stop tells the operator what to do — PR-h-field-06 item 2.

The old notice named no numbers and pointed at a file for the tally. That was tolerable while
a ceiling was something you opted into: you knew your own number. It is not tolerable now that
`budget_tokens` has a DEFAULT, because the first time most operators see this message they will
not have chosen the ceiling it is enforcing.

Asserted on the emitted TEXT rather than on internal state, because the claim is about what a
person reads — and under `--quiet` too, since a terminal condition is not progress chatter.
Same four questions the provider-quota notice answers (PR-h-field-02): what happened, whether
the work survived, what to change, and the exact command to continue.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _synth_with_blocker,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _run_into_the_ceiling(repo, pr_doc, level: str, monkeypatch, **loop):
    """A run whose first round's usage exceeds a tiny ceiling."""
    from syncade import dispatcher as disp_mod
    from syncade.synthesizer import driver as synth_mod
    from syncade.usage import Usage

    def usage(*_a, **_kw):
        return Usage(
            model="m",
            input_tokens=9_000,
            output_tokens=1_000,
            cached_input_tokens=0,
            reasoning_output_tokens=0,
            cost_usd=1.25,
            cost_source="estimated",
            auth_mode="subscription",
        )

    monkeypatch.setattr(disp_mod, "usage_for", usage)
    monkeypatch.setattr(synth_mod, "usage_for", usage)

    logger = Logger(level)
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(
            reviewers=[
                {"name": "rv1", "provider": "fake1", "model": "x"},
                {"name": "rv2", "provider": "fake2", "model": "y"},
            ],
            loop={"max_rounds": 2, **loop},
        ),
        # NO-SHIP so the loop reaches round 1's pre-dispatch budget check. A round-0 SHIP is
        # terminal and is never relabeled 25 — deliberate, so the ceiling cannot rewrite a
        # verdict that was already reached.
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
        logger=logger,
    )
    logger.summary(result)
    return result


@pytest.mark.parametrize("level", ["normal", "quiet"])
def test_the_operator_is_told_all_four_things(level, repo_with_pr_doc, monkeypatch, capsys):
    repo, pr_doc = repo_with_pr_doc
    result = _run_into_the_ceiling(repo, pr_doc, level, monkeypatch, budget_tokens=1_000)
    out = capsys.readouterr().out

    assert result.termination_reason == "budget_exceeded"
    # 1. which ceiling, and the number they would change
    assert "token ceiling (budget_tokens = 1,000)" in out
    # 2. what was actually spent, as tokens AND a flagged valuation
    assert "Spent:" in out and "tokens" in out
    assert "valuation, not billed money" in out
    # 3. the work survived
    assert "preserved" in out
    # 4. how to continue, with the id that makes it usable
    assert f"syncade --resume {result.artifacts.run_dir.name}" in out
    # and how to stop being stopped
    assert "budget_tokens = 0" in out


def test_the_numbers_match_the_artifact(repo_with_pr_doc, monkeypatch, capsys):
    """One number, three surfaces. The notice must not disagree with loop-summary.md — which
    is why the tally is CARRIED on the result rather than recomputed in the logger (on a
    --resume the enforcement tally is the fresh spend, not the rehydrated rounds)."""
    repo, pr_doc = repo_with_pr_doc
    result = _run_into_the_ceiling(repo, pr_doc, "normal", monkeypatch, budget_tokens=1_000)
    out = capsys.readouterr().out

    spent = sum(u.total_tokens for u in result.budget_usages)
    # Guard against the tautology this test would otherwise be: `spent` is derived from the
    # same field the message renders, so an EMPTY tally would print "0 tokens" and satisfy a
    # naive equality against itself. Mutation-checked — dropping the carried tally must fail
    # this test, and without this line it did not.
    assert spent >= 10_000, f"the enforcement tally was not carried onto the result ({spent})"
    assert f"{spent:,} tokens" in out
    summary = (result.artifacts.run_dir / "loop-summary.md").read_text(encoding="utf-8")
    assert f"{spent:,}" in summary, "the notice and the artifact report different tallies"


def test_a_dollar_ceiling_names_the_dollar_ceiling(repo_with_pr_doc, monkeypatch, capsys):
    """Both ceilings can be set; the notice must name the one that actually crossed."""
    repo, pr_doc = repo_with_pr_doc
    _run_into_the_ceiling(repo, pr_doc, "normal", monkeypatch, budget_usd=0.5, budget_tokens=0)
    out = capsys.readouterr().out
    assert "cost ceiling (budget_usd = $0.50)" in out
    assert "token ceiling" not in out


def test_dollar_ceiling_stop_remedy_names_budget_usd(repo_with_pr_doc, monkeypatch, capsys):
    """When the USD ceiling fires, the remedy must name budget_usd — not budget_tokens.

    The old message used a single unconditional remedy line that told the operator to set
    `budget_tokens = 0`. When the ACTIVE ceiling was budget_usd, that instruction was wrong:
    it leaves the dollar ceiling intact, so `syncade --resume` hits the same stop immediately.
    The fix branches on budget_ceiling; this test guards the branch.
    """
    repo, pr_doc = repo_with_pr_doc
    _run_into_the_ceiling(repo, pr_doc, "normal", monkeypatch, budget_usd=0.5, budget_tokens=0)
    out = capsys.readouterr().out
    assert "budget_usd" in out
    # The token opt-out instruction is wrong when the active ceiling is budget_usd
    assert "budget_tokens = 0" not in out


def test_no_notice_on_an_ordinary_run(repo_with_pr_doc, monkeypatch, capsys):
    """A notice on every run is a notice nobody reads."""
    repo, pr_doc = repo_with_pr_doc
    result = _run_into_the_ceiling(repo, pr_doc, "normal", monkeypatch, budget_tokens=0)
    assert result.termination_reason != "budget_exceeded"
    assert "stopped early" not in capsys.readouterr().out


def test_budget_tokens_zero_opt_out_not_rendered_as_ceiling():
    """budget_tokens=0 is the no-ceiling sentinel, not a real limit of zero.

    When a dollar budget trips and budget_tokens=0, the summary must not render
    'total tokens <= 0' — that would imply a zero-token limit was in effect.
    """
    from syncade.persistence.loop_summary_text import _budget_section
    from syncade.usage import Usage

    u = Usage(
        model="m",
        input_tokens=1000,
        output_tokens=0,
        cached_input_tokens=0,
        reasoning_output_tokens=0,
        cost_usd=1.0,
        cost_source="estimated",
        auth_mode="subscription",
    )
    lines = _budget_section([u], budget_tokens=0, budget_usd=0.5, budget_ceiling="budget_usd")
    text = "\n".join(str(ln) for ln in lines)
    assert "total tokens ≤ 0" not in text, "opt-out sentinel must not render as a zero ceiling"
    assert "0.50" in text  # dollar ceiling still renders


def test_budget_usd_zero_opt_out_not_rendered_as_ceiling():
    """budget_usd=0 is the no-ceiling sentinel, not an active $0.0000 limit.

    When a token budget trips and budget_usd=0, the summary must not render
    'API-equivalent cost <= $0.0000' — that contradicts the '0 = no ceiling' contract
    and implies a zero-dollar limit was in effect.
    """
    from syncade.persistence.loop_summary_text import _budget_section
    from syncade.usage import Usage

    u = Usage(
        model="m",
        input_tokens=1000,
        output_tokens=0,
        cached_input_tokens=0,
        reasoning_output_tokens=0,
        cost_usd=1.0,
        cost_source="estimated",
        auth_mode="subscription",
    )
    lines = _budget_section([u], budget_tokens=1_000, budget_usd=0, budget_ceiling="budget_tokens")
    text = "\n".join(str(ln) for ln in lines)
    assert "API-equivalent cost ≤ $0.0000" not in text, (
        "opt-out sentinel must not render as a zero dollar ceiling"
    )
    assert "1,000" in text  # token ceiling still renders


def test_stop_message_shows_lower_bound_when_cost_source_unknown():
    """A Usage with cost_usd set but cost_source='unknown' has unpriced tokens.

    `cost_usd is not None` was the old check — it classified this as fully priced and omitted
    the lower-bound warning, contradicting loop-summary.md which uses cost_incomplete_tokens.
    The stop message now calls billing.from_usages() so both surfaces use the same rule.
    """
    import types

    from syncade.logging import _budget_stop_line
    from syncade.usage import Usage

    u = Usage(
        model="m",
        input_tokens=9_000,
        output_tokens=1_000,
        cached_input_tokens=0,
        reasoning_output_tokens=0,
        cost_usd=1.23,
        cost_source="unknown",  # non-None cost but unclassified → cost_incomplete_tokens > 0
        auth_mode="api",
    )
    run_dir = types.SimpleNamespace(name="2026-01-01T00-00-00")
    result = types.SimpleNamespace(
        budget_usages=[u, u],
        budget_ceiling="budget_tokens",
        budget_tokens=1_000,
        budget_usd=None,
        artifacts=types.SimpleNamespace(run_dir=run_dir),
    )

    line = _budget_stop_line(result)
    assert "LOWER BOUND" in line, "partial-cost tally must be flagged as a lower bound"
    assert "$" in line  # the amount still appears
