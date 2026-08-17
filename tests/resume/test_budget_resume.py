"""PR-v2-11 Issue 4: a budget-aborted run (exit 25) is ``--resume``-eligible, and the plan
resumes at the correct round for BOTH abort shapes.

- before-producer stop: the crossing round's review bundle is complete (no producer block because
  the budget tripped before dispatch). plan_resume detects the budget_exceeded termination_reason
  in the loop manifest and rehydrates the review bundle into completed_rounds so resume dispatches
  only the producer under the fresh budget tally.
- before-round stop: the prior round completed (producer committed) → resume the next round.
"""

from __future__ import annotations

from pathlib import Path

from syncade.orchestrator.resume import find_resumable_runs, plan_resume, resolve_resume_target
from tests.resume._helpers import _write_loop_manifest, _write_round_manifest, _write_run_init

_RID = "2026-07-18T10-00-00"


def _runs_root(tmp_path: Path) -> Path:
    runs = tmp_path / ".syncade" / "runs"
    runs.mkdir(parents=True)
    return runs


def test_budget_aborted_run_is_resume_eligible(tmp_path):
    """C2: exit 25 is eligible (0/20/30 are not) — the run is offered by find/resolve."""
    runs = _runs_root(tmp_path)
    run_dir = runs / _RID
    _write_run_init(run_dir, max_rounds=2)
    _write_round_manifest(run_dir, 0, round_exit_code=30, producer_outcome=None)
    _write_loop_manifest(
        run_dir, final_exit_code=25, termination_reason="budget_exceeded", final_round=0
    )
    assert find_resumable_runs(runs) == [_RID]
    assert resolve_resume_target(runs, _RID, None) == _RID


def test_before_producer_abort_rehydrates_review_bundle(tmp_path):
    """A budget-abort before the producer rehydrates the review bundle rather than dropping it.

    plan_resume should put round 0 in completed_rounds (so the review bundle is
    rehydrated via load_completed_round) and set budget_aborted_before_producer_round=0
    so the loop dispatches only the producer on the fresh budget — not re-running reviewers.
    """
    runs = _runs_root(tmp_path)
    run_dir = runs / _RID
    _write_run_init(run_dir, max_rounds=2)
    _write_round_manifest(run_dir, 0, round_exit_code=30, producer_outcome=None)
    _write_loop_manifest(
        run_dir, final_exit_code=25, termination_reason="budget_exceeded", final_round=0
    )
    plan = plan_resume(tmp_path, run_dir)
    assert plan.resumed_round == 0
    assert plan.completed_rounds == [0]
    assert plan.budget_aborted_before_producer_round == 0


def test_genuine_interrupt_before_producer_drops_and_retries(tmp_path):
    """A genuine interrupt (no loop manifest) still drops and retries the whole round."""
    runs = _runs_root(tmp_path)
    run_dir = runs / _RID
    _write_run_init(run_dir, max_rounds=2)
    _write_round_manifest(run_dir, 0, round_exit_code=30, producer_outcome=None)
    # No loop manifest — the process was killed before loop_finalize ran
    plan = plan_resume(tmp_path, run_dir)
    assert plan.resumed_round == 0
    assert plan.completed_rounds == []
    assert plan.budget_aborted_before_producer_round is None


def test_before_round_abort_resumes_at_next_round(tmp_path):
    """The prior round completed (producer committed) → resume starts the next round."""
    runs = _runs_root(tmp_path)
    run_dir = runs / _RID
    _write_run_init(run_dir, max_rounds=2)
    _write_round_manifest(
        run_dir, 0, round_exit_code=30, producer_outcome="committed", producer_ending_sha="b" * 40
    )
    _write_loop_manifest(
        run_dir, final_exit_code=25, termination_reason="budget_exceeded", final_round=0
    )
    plan = plan_resume(tmp_path, run_dir)
    assert plan.resumed_round == 1
    assert plan.completed_rounds == [0]


def test_original_budget_tolerates_malformed_snapshot(tmp_path):
    """Robustness: a structurally malformed config_snapshot must not crash budget inheritance.

    Chained ``.get`` on a non-dict would AttributeError; _original_budget swallows it and
    returns {} (inherit no budget from this snapshot)."""
    from syncade.cli.resume_mode import _original_budget

    for i, payload in enumerate(
        ('{"config_snapshot": "not-a-dict"}', '{"config_snapshot": {"loop": 42}}', "[1,2,3]")
    ):
        p = tmp_path / f"run-init-{i}.json"
        p.write_text(payload, encoding="utf-8")
        assert _original_budget(p) == {}


def test_original_budget_rejects_type_malformed_values(tmp_path):
    """Type-malformed budget values in a well-structured snapshot must be rejected.

    A corrupted value like False (disables ceiling), a string (crashes in over_budget),
    or a negative int (immediate bogus stop) must not bypass LoopConfig validators via
    model_copy — _original_budget must drop them rather than pass them through."""
    from syncade.cli.resume_mode import _original_budget

    def _snap(loop_dict):
        return f'{{"config_snapshot": {{"loop": {json.dumps(loop_dict)}}}}}'

    import json

    # Non-numeric, bool, negative, and non-finite values are rejected for both keys.
    import math

    bad_for_tokens = [False, "50000000", -1, [], {}, 0.0, 9.99, float("nan"), float("inf")]
    for v in bad_for_tokens:
        p = tmp_path / f"bad-tokens-{id(v)}.json"
        p.write_text(_snap({"budget_tokens": v}), encoding="utf-8")
        assert _original_budget(p) == {}, f"expected empty for budget_tokens={v!r}"

    bad_for_usd = [False, "50000000", -1, [], {}, float("nan"), float("inf")]
    for v in bad_for_usd:
        p = tmp_path / f"bad-usd-{id(v)}.json"
        p.write_text(_snap({"budget_usd": v}), encoding="utf-8")
        assert _original_budget(p) == {}, f"expected empty for budget_usd={v!r}"

    # Valid budget_tokens: plain int, non-negative (including 0, the opt-out sentinel).
    for v in [0, 1000, 50_000_000]:
        p = tmp_path / f"good-tokens-{id(v)}.json"
        p.write_text(_snap({"budget_tokens": v, "budget_usd": 5.0}), encoding="utf-8")
        result = _original_budget(p)
        assert result["budget_tokens"] == v
        assert result["budget_usd"] == 5.0

    # budget_usd allows finite floats; those same values must NOT pass as budget_tokens.
    for v in [0.0, 9.99]:
        p = tmp_path / f"float-{id(v)}.json"
        p.write_text(_snap({"budget_tokens": v, "budget_usd": v}), encoding="utf-8")
        result = _original_budget(p)
        assert "budget_tokens" not in result, f"float {v!r} must not be inherited as budget_tokens"
        assert math.isclose(result["budget_usd"], v), f"float {v!r} must pass through as budget_usd"
