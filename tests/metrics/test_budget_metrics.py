"""PR-v2-11 Issue 3: a budget abort is recorded in metrics, and the metrics API-equivalent
total is the SAME number the budget tally tripped on (C1, surface 3).
"""

from __future__ import annotations

import json

import pytest

from syncade.cli.metrics_mode import _billing_totals
from syncade.metrics.aggregate import backfill
from syncade.metrics.schema import open_db


def _write_budget_run(runs_root, run_id, reason="budget_exceeded"):
    """A budget-abort run: exit 25, two SUBSCRIPTION reviewers priced at $0.30 each, an
    UNPRICED judge (cost null) contributing 5000 lower-bound tokens."""
    d = runs_root / run_id
    d.mkdir(parents=True)
    actor = lambda name, tokens, cost: {  # noqa: E731 - terse local row builder
        "name": name,
        "provider": "openai",
        "finding_count": 1,
        "duration_seconds": 1.0,
        "tokens": tokens,
        "cost_usd": cost,
        "cost_source": "estimated" if cost is not None else "unknown",
        "auth_mode": "subscription",
    }
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "final_exit_code": 25,
                "termination_reason": reason,
                "rounds": [
                    {
                        "round": 0,
                        "round_exit_code": 30,
                        "snapshot": {"commit_sha": "s"},
                        "reviewers": [actor("rv1", 12000, 0.30), actor("rv2", 12000, 0.30)],
                        "synthesizer": {
                            "outcome": "success",
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "active_blocker_count": 1,
                            "active_minor_count": 0,
                            "active_nit_count": 0,
                            "dismissed_count": 0,
                            "tokens": 5000,
                            "cost_usd": None,
                            "cost_source": "unknown",
                            "auth_mode": "subscription",
                        },
                    }
                ],
            }
        )
    )
    (d / "run-init.json").write_text(
        json.dumps({"operator_branch": "feature", "config_snapshot": {"reviewers": []}})
    )


def test_metrics_records_budget_abort_and_agrees_on_api_equiv(tmp_path):
    runs = tmp_path / "runs"
    _write_budget_run(runs, "R-budget")
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    # The abort is recorded, with a verdict distinct from max-rounds.
    row = conn.execute(
        "SELECT verdict, termination_reason FROM runs WHERE run_id='R-budget'"
    ).fetchone()
    assert tuple(row) == ("BUDGET", "budget_exceeded")

    # C1: the metrics API-equivalent total IS the $0.60 the dollar budget tripped on
    # (2 reviewers x 0.30); the unpriced judge is an honest lower-bound token count, not $0.
    b = _billing_totals(conn, ["R-budget"])
    assert b.api_equiv == pytest.approx(0.60)
    assert b.billed == pytest.approx(0.0)  # subscription: no real money left the account
    assert b.total_unpriced_tokens == 5000


def test_a_provider_quota_stop_is_not_reported_as_the_operators_budget(tmp_path):
    """Both exit 25, and `--metrics` used to call both "BUDGET".

    They want opposite responses — raise the cap, versus wait for the window to reset — so a
    corpus that conflates them tells the operator to change a setting that was never the cause.
    """
    runs = tmp_path / "runs"
    _write_budget_run(runs, "R-quota", reason="provider_usage_limit")
    _write_budget_run(runs, "R-budget")
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    verdicts = dict(conn.execute("SELECT run_id, verdict FROM runs").fetchall())
    assert verdicts["R-quota"] == "QUOTA", (
        "a provider refusing to serve was reported as the operator's own ceiling"
    )
    assert verdicts["R-budget"] == "BUDGET", "the real budget abort must still say BUDGET"
