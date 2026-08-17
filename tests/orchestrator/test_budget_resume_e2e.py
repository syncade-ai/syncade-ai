"""PR-v2-11 Issue 4 end-to-end: resuming a budget-aborted run.

- C6 (fresh tally): the resumed run applies the budget to the RESUMED spend — the tally starts
  empty, so the run makes progress instead of being refused or instantly re-aborted.
- CLI wiring: ``--resume --budget-usd X`` overrides the reloaded config (resume reloads from
  disk and would otherwise drop it), and the fresh-budget notice is announced.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.exit_codes import BUDGET_EXCEEDED
from syncade.logging import Logger
from syncade.usage import Usage
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _ship,
    _synth_clean,
    _synth_with_blocker,
)
from tests.orchestrator._resume_fixtures import _prepare_aborted_run

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def test_resume_of_budget_abort_progresses_under_a_fresh_budget(repo_with_pr_doc, monkeypatch):
    """C6: a budget-aborted run resumes eligibly and runs the resumed round under a FRESH
    budget (empty tally — it bounds only the resumed spend), reaching SHIP rather than being
    refused or instantly re-aborted."""
    from syncade.orchestrator import run_review
    from syncade.orchestrator.resume import plan_resume

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
    run_dir, _ = _prepare_aborted_run(
        repo_root, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=25
    )
    plan = plan_resume(repo_root, run_dir)
    assert plan.resumed_round == 1  # round 0 completed; resume the next round

    # The resumed round's live reviewers report cost, staying UNDER the fresh budget.
    import syncade.dispatcher as disp_mod

    monkeypatch.setattr(
        disp_mod,
        "usage_for",
        lambda *a, **k: Usage(
            model="m", input_tokens=1000, output_tokens=0, cost_usd=0.10, auth_mode="api"
        ),
    )

    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 2, "budget_usd": 0.50},
    )
    result = run_review(
        repo_root=repo_root,
        pr_doc_path=pr_doc,
        config=config,
        adapter_factory=_factory_returning(FakeAdapter(_ship()), FakeAdapter(_ship())),
        logger=Logger(level="quiet"),
        resume_plan=plan,
        force_drift=True,
    )
    assert result.exit_code == 0  # resumed round ran + shipped: progress under the fresh budget
    assert result.termination_reason == "ship"
    assert len(result.rounds) == 2  # round 0 rehydrated + round 1 run


def _resume_args(repo_root, **overrides):
    base = {
        "repo_root": str(repo_root),
        "resume": "latest",
        "max_rounds": None,
        "budget_tokens": None,
        "budget_usd": None,
        "quiet": False,
        "allow_default_branch": True,  # the fixture run is on main; skip the commit guard
        "timeout": None,
        "force_dirty": False,
        "force_drift": True,
        "preset": None,  # global flag; resume threads it to load_config (parser always sets it)
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cli_resume_applies_budget_override_and_announces_fresh_tally(
    repo_with_pr_doc, monkeypatch, capsys
):
    """CLI: `--resume --budget-usd 5` reaches the resumed run's config (resume reloads config
    from disk, so without the override it would be dropped), and the fresh-budget notice fires."""
    from syncade.cli import resume_mode

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
    _prepare_aborted_run(
        repo_root, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=25
    )

    captured = {}

    def _capture(**kwargs):
        captured["budget_usd"] = kwargs["config"].loop.budget_usd
        captured["budget_tokens"] = kwargs["config"].loop.budget_tokens
        return SimpleNamespace(exit_code=0)

    # auth_gate probes providers; short-circuit it. run_review is captured, not run.
    monkeypatch.setattr(resume_mode, "auth_gate", lambda *a, **k: None)
    monkeypatch.setattr("syncade.orchestrator.run_review", _capture)

    rc = resume_mode._run_resume(_resume_args(repo_root, budget_usd=5.0, budget_tokens=250000))

    assert rc == 0
    assert captured["budget_usd"] == 5.0  # the override reached the resumed config
    assert captured["budget_tokens"] == 250000
    notice = "".join(capsys.readouterr())
    assert "FRESH budget" in notice
    assert "can exceed a single budget" in notice


def _capture_resume_config(monkeypatch):
    """Patch auth_gate + run_review so a driven _run_resume captures the resumed config."""
    from syncade.cli import resume_mode

    captured = {}

    def _capture(**kwargs):
        captured["budget_tokens"] = kwargs["config"].loop.budget_tokens
        captured["budget_usd"] = kwargs["config"].loop.budget_usd
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(resume_mode, "auth_gate", lambda *a, **k: None)
    monkeypatch.setattr("syncade.orchestrator.run_review", _capture)
    return captured


def test_cli_resume_inherits_original_budget_when_none_given(repo_with_pr_doc, monkeypatch, capsys):
    """Finding 2: a run launched with a (CLI-only) budget recorded in run-init, resumed with NO
    --budget-*, INHERITS the original ceiling instead of silently resuming unguarded. The tally
    is still fresh; only the ceiling is inherited, and the notice says so."""
    from syncade.cli import resume_mode

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
    _prepare_aborted_run(
        repo_root,
        pr_doc,
        completed_round_count=1,
        max_rounds=2,
        aborted_exit_code=25,
        budget_tokens=3500,  # the original run's CLI-only budget, folded into run-init
    )
    captured = _capture_resume_config(monkeypatch)

    rc = resume_mode._run_resume(_resume_args(repo_root))  # no --budget-* on resume
    assert rc == 0
    assert captured["budget_tokens"] == 3500  # inherited from the original run — guardrail kept
    assert captured["budget_usd"] is None
    notice = "".join(capsys.readouterr())
    assert "inherited" in notice
    assert "budget_tokens=3500" in notice


def test_cli_resume_budget_arg_overrides_original(repo_with_pr_doc, monkeypatch, capsys):
    """A --budget-* on resume OVERRIDES the original run's recorded budget (does not inherit)."""
    from syncade.cli import resume_mode

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
    _prepare_aborted_run(
        repo_root,
        pr_doc,
        completed_round_count=1,
        max_rounds=2,
        aborted_exit_code=25,
        budget_tokens=3500,
    )
    captured = _capture_resume_config(monkeypatch)

    rc = resume_mode._run_resume(_resume_args(repo_root, budget_tokens=9999))
    assert rc == 0
    assert captured["budget_tokens"] == 9999  # CLI wins, not the inherited 3500
    notice = "".join(capsys.readouterr())
    assert "inherited" not in notice  # explicit override → not an inheritance


def test_before_producer_budget_resume_runs_only_the_producer(repo_with_pr_doc, monkeypatch):
    """End-to-end verification of the producer's da05777 resume redesign: resuming a
    BEFORE-PRODUCER budget abort rehydrates the complete review bundle and dispatches ONLY the
    producer under the fresh budget — it does NOT re-run the reviewers/judge.

    The producer's own tests only checked ``plan_resume``'s OUTPUT; this drives the real loop.
    Proof that the reviewers are not re-run: the resume is given EXACTLY two reviewer adapters
    (enough for the NEXT round only). If round 0 wrongly re-dispatched, the factory would
    exhaust and raise ``factory exhausted``.
    """
    from syncade.orchestrator import run_review
    from syncade.orchestrator.resume import plan_resume

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)

    # --- Phase 1: drive a real before-producer budget abort (exit 25) -----------------------
    import syncade.dispatcher as disp_mod
    import syncade.synthesizer.driver as synth_mod

    big = Usage(model="m", input_tokens=5000, output_tokens=0, cost_usd=0.10, auth_mode="api")
    monkeypatch.setattr(disp_mod, "usage_for", lambda *a, **k: big)
    monkeypatch.setattr(synth_mod, "usage_for", lambda *a, **k: big)

    reviewers = [
        {"name": "rv1", "provider": "fake1", "model": "x"},
        {"name": "rv2", "provider": "fake2", "model": "y"},
    ]
    phase1 = run_review(
        repo_root=repo_root,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=reviewers, loop={"max_rounds": 2, "budget_tokens": 1}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_with_blocker()),
        producer_adapter=FakeProducerAdapter(commit_message="phase-1 producer must not run"),
        logger=Logger(level="quiet"),
    )
    assert phase1.exit_code == BUDGET_EXCEEDED  # before-producer abort
    run_dir = phase1.artifacts.run_dir

    # --- Phase 2: resume it. plan detects the before-producer budget abort. -----------------
    plan = plan_resume(repo_root, run_dir)
    assert plan.budget_aborted_before_producer_round == 0
    assert plan.resumed_round == 0 and plan.completed_rounds == [0]

    producer = FakeProducerAdapter(commit_message="fix: round-0 producer on resume")
    phase2 = run_review(
        repo_root=repo_root,
        pr_doc_path=pr_doc,
        # No budget on the resumed config → round 0's producer runs, then round 1 re-reviews.
        config=SyncadeConfig(reviewers=reviewers, loop={"max_rounds": 2}),
        # EXACTLY two reviewer adapters — for round 1 ONLY. If round 0 re-dispatched (the bug),
        # the factory would exhaust.
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
        producer_adapter=producer,
        resume_plan=plan,
        force_drift=True,
        logger=Logger(level="quiet"),
    )
    assert phase2.exit_code == 0  # round-0 producer fixed it → round 1 SHIP (reviewers not re-run)
    assert producer.invocations  # the round-0 producer DID run on resume
    assert len(phase2.rounds) == 2  # round 0 (rehydrated) + round 1 (fresh review)


def test_resume_inherits_a_tighter_original_ceiling_over_the_DEFAULT(
    repo_with_pr_doc, monkeypatch, capsys
):
    """PR-h-field-06 regression: a default ceiling must not quietly loosen a resumed run.

    Inheritance used to key on `config.loop.budget_tokens is None`, which was equivalent to
    "the operator did not configure one" — until budget_tokens gained a default. Then it was
    never None, inheritance stopped firing, and a run launched at 3,500 tokens resumed at
    50,000,000. Not unguarded, but 14,000x looser, which is the same surprise the inheritance
    rule exists to prevent.

    Asserted against the DEFAULT explicitly rather than just `== 3500`, so the test states
    which failure it is guarding against.
    """
    from syncade.cli import resume_mode

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
    _prepare_aborted_run(
        repo_root,
        pr_doc,
        completed_round_count=1,
        max_rounds=2,
        aborted_exit_code=25,
        budget_tokens=3500,
    )
    captured = _capture_resume_config(monkeypatch)
    assert resume_mode._run_resume(_resume_args(repo_root)) == 0
    assert captured["budget_tokens"] == 3500
    assert captured["budget_tokens"] != 50_000_000, "the default silently replaced the original"


def test_fresh_budget_notice_suppressed_when_all_ceilings_are_zero(
    repo_with_pr_doc, monkeypatch, capsys
):
    """No fresh-budget notice when both ceilings are explicitly 0 (the opt-out sentinel).

    budget_tokens=0 and budget_usd=0 both mean 'no ceiling'; printing the notice in that case
    contradicts the contract and confuses operators who set the sentinel to disable ceilings.
    """
    from syncade.cli import resume_mode

    repo_root, pr_doc = repo_with_pr_doc
    subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)
    _prepare_aborted_run(
        repo_root, pr_doc, completed_round_count=1, max_rounds=2, aborted_exit_code=25
    )

    monkeypatch.setattr(resume_mode, "auth_gate", lambda *a, **k: None)
    monkeypatch.setattr("syncade.orchestrator.run_review", lambda **_: SimpleNamespace(exit_code=0))

    resume_mode._run_resume(_resume_args(repo_root, budget_tokens=0, budget_usd=0.0))

    notice = "".join(capsys.readouterr())
    assert "FRESH budget" not in notice, (
        "fresh-budget notice must not fire when both ceilings are the opt-out sentinel (0)"
    )
