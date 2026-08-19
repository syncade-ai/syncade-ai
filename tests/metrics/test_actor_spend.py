from __future__ import annotations

import json

from syncade.cli.metrics_mode import render_report
from syncade.metrics.aggregate import backfill
from syncade.metrics.schema import open_db


def _write_run(runs_root, run_id, *, rounds, config_reviewers=None):
    d = runs_root / run_id
    d.mkdir(parents=True)
    (d / "loop-manifest.json").write_text(
        json.dumps({"run_id": run_id, "final_exit_code": 0, "rounds": rounds})
    )
    (d / "run-init.json").write_text(
        json.dumps(
            {"operator_branch": "main", "config_snapshot": {"reviewers": config_reviewers or []}}
        )
    )


def _usage_round():
    return {
        "round": 0,
        "round_exit_code": 0,
        "snapshot": {"commit_sha": "s"},
        "reviewers": [
            {
                "name": "cdx",
                "provider": "openai",
                "model": "gpt-5.5",
                "finding_count": 1,
                "duration_seconds": 1.0,
                "tokens": 1000,
                "cost_usd": 0.01,
                "cost_source": "estimated",
            }
        ],
        "synthesizer": {
            "outcome": "success",
            "provider": "openai",
            "model": "gpt-5.5",
            "active_blocker_count": 0,
            "active_minor_count": 0,
            "active_nit_count": 0,
            "dismissed_count": 0,
            "tokens": 300,
            "cost_usd": 0.005,
            "cost_source": "estimated",
        },
        "producer": {
            "outcome": "stalled",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "tokens": 2000,
            "cost_usd": 0.02,
            "cost_source": "estimated",
        },
    }


def test_backfill_writes_actor_usage_for_reviewer_synth_and_producer(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R-usage",
        rounds=[_usage_round()],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    rows = conn.execute(
        "SELECT role, name, provider, model, tokens, cost_usd FROM actor_stats "
        "WHERE run_id='R-usage' ORDER BY role, name"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("producer", "producer", "anthropic", "claude-sonnet-4-6", 2000, 0.02),
        ("reviewer", "cdx", "openai", "gpt-5.5", 1000, 0.01),
        ("synthesizer", "synthesizer", "openai", "gpt-5.5", 300, 0.005),
    ]


def test_metrics_use_artifact_model_identity_when_actor_model_changes(tmp_path):
    runs = tmp_path / "runs"
    old = _usage_round()
    old["reviewers"][0]["model"] = "gpt-artifact-old"
    old["reviewers"][0]["tokens"] = 100
    old["synthesizer"]["provider"] = "openai"
    old["synthesizer"]["model"] = "synth-artifact-old"
    old["synthesizer"]["tokens"] = 10
    old["producer"]["model"] = "producer-artifact-old"
    old["producer"]["tokens"] = 1000

    new = _usage_round()
    new["reviewers"][0]["model"] = "gpt-artifact-new"
    new["reviewers"][0]["tokens"] = 200
    new["synthesizer"]["provider"] = "openai"
    new["synthesizer"]["model"] = "synth-artifact-new"
    new["synthesizer"]["tokens"] = 20
    new["producer"]["model"] = "producer-artifact-new"
    new["producer"]["tokens"] = 2000

    _write_run(
        runs,
        "R-drift",
        rounds=[old, new],
        config_reviewers=[{"name": "cdx", "provider": "openai", "model": "wrong-current"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    actors = conn.execute(
        "SELECT role, name, provider, model, tokens FROM actor_stats "
        "WHERE run_id='R-drift' ORDER BY role, model"
    ).fetchall()
    assert [tuple(r) for r in actors] == [
        ("producer", "producer", "anthropic", "producer-artifact-new", 2000),
        ("producer", "producer", "anthropic", "producer-artifact-old", 1000),
        ("reviewer", "cdx", "openai", "gpt-artifact-new", 200),
        ("reviewer", "cdx", "openai", "gpt-artifact-old", 100),
        ("synthesizer", "synthesizer", "openai", "synth-artifact-new", 20),
        ("synthesizer", "synthesizer", "openai", "synth-artifact-old", 10),
    ]
    reviewer_rows = conn.execute(
        "SELECT name, provider, model, tokens FROM reviewer_stats "
        "WHERE run_id='R-drift' ORDER BY model"
    ).fetchall()
    assert [tuple(r) for r in reviewer_rows] == [
        ("cdx", "openai", "gpt-artifact-new", 200),
        ("cdx", "openai", "gpt-artifact-old", 100),
    ]
    out = render_report(conn)
    assert "wrong-current" not in out
    assert "openai/gpt-artifact-old" in out
    assert "openai/gpt-artifact-new" in out
    assert "openai/synth-artifact-old" in out
    assert "openai/synth-artifact-new" in out


def test_render_report_shows_spend_and_by_model_cost(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R-usage",
        rounds=[_usage_round()],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    out = render_report(conn)
    assert "billed:" in out  # was `spend:` — summing valuations and calling it spend WAS the bug
    assert "3300" in out
    assert "by model" in out
    assert "openai/gpt-5.5" in out and "roles=reviewer,synthesizer" in out
    assert "anthropic/claude-sonnet-4-6" in out and "roles=producer" in out


def test_render_report_flags_unpriced_runs_not_free(tmp_path):
    runs = tmp_path / "runs"
    rnd = {
        "round": 0,
        "round_exit_code": 0,
        "snapshot": {"commit_sha": "s"},
        "reviewers": [
            {
                "name": "cdx",
                "provider": "openai",
                "finding_count": 0,
                "duration_seconds": 1.0,
                "tokens": 5000,
                "cost_usd": None,
                "cost_source": "unknown",
            }
        ],
        "synthesizer": {
            "outcome": "success",
            "active_blocker_count": 0,
            "active_minor_count": 0,
            "active_nit_count": 0,
            "dismissed_count": 0,
        },
        "producer": None,
    }
    _write_run(
        runs,
        "R-unpriced",
        rounds=[rnd],
        config_reviewers=[{"name": "cdx", "model": "unknown-model"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    out = render_report(conn)
    assert "tokens:     5000" in out
    assert "cost-incomplete tokens" in out and "NOT free" in out
    assert "known_cost=$0.00" in out


def test_render_report_labels_legacy_null_usage_as_unavailable(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R-legacy",
        rounds=[
            {
                "round": 0,
                "round_exit_code": 0,
                "snapshot": {"commit_sha": "s"},
                "reviewers": [],
                "synthesizer": {
                    "outcome": "success",
                    "active_blocker_count": 0,
                    "active_minor_count": 0,
                    "active_nit_count": 0,
                    "dismissed_count": 0,
                },
                "producer": None,
            }
        ],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    row = conn.execute("SELECT tokens, cost_usd FROM runs WHERE run_id='R-legacy'").fetchone()
    assert tuple(row) == (None, None)
    out = render_report(conn)
    assert "unavailable (legacy/no usage recorded)" in out
    # A legacy run with NO usage at all must NOT be rendered as "0 tokens / $0.00".
    # Unknown is not zero, and quietly printing zero would be the same class of lie this
    # PR exists to delete -- just pointing the other way.
    assert "billed:" not in out
    assert "tokens:     0" not in out


def test_malformed_usage_fields_do_not_fabricate_zero_cost_rows(tmp_path):
    runs = tmp_path / "runs"
    rnd = _usage_round()
    rnd["reviewers"][0]["tokens"] = "oops"
    rnd["reviewers"][0]["cost_usd"] = "oops"
    rnd["synthesizer"]["tokens"] = "oops"
    rnd["synthesizer"]["cost_usd"] = "oops"
    rnd["producer"]["tokens"] = "oops"
    rnd["producer"]["cost_usd"] = "oops"
    _write_run(
        runs,
        "R-malformed",
        rounds=[rnd],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    row = conn.execute("SELECT tokens, cost_usd FROM runs WHERE run_id='R-malformed'").fetchone()
    reviewer = conn.execute(
        "SELECT tokens, cost_usd, cost_source FROM reviewer_stats WHERE run_id='R-malformed'"
    ).fetchone()
    assert tuple(row) == (None, None)
    assert tuple(reviewer) == (None, None, "")
    assert (
        conn.execute("SELECT COUNT(*) FROM actor_stats WHERE run_id='R-malformed'").fetchone()[0]
        == 0
    )


def test_negative_usage_fields_do_not_reduce_spend_or_tokens(tmp_path):
    runs = tmp_path / "runs"
    rnd = _usage_round()
    rnd["reviewers"][0]["cost_usd"] = -0.01
    rnd["synthesizer"]["tokens"] = -300
    rnd["synthesizer"]["cost_usd"] = -0.005
    rnd["producer"]["tokens"] = -2000
    rnd["producer"]["cost_usd"] = 0.02
    _write_run(
        runs,
        "R-negative",
        rounds=[rnd],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)

    row = conn.execute("SELECT tokens, cost_usd FROM runs WHERE run_id='R-negative'").fetchone()
    reviewer = conn.execute(
        "SELECT tokens, cost_usd, cost_source FROM reviewer_stats WHERE run_id='R-negative'"
    ).fetchone()
    actors = conn.execute(
        "SELECT role, tokens, cost_usd, cost_incomplete_tokens, cost_source "
        "FROM actor_stats WHERE run_id='R-negative'"
    ).fetchall()
    out = render_report(conn)

    assert tuple(row) == (1000, None)
    assert tuple(reviewer) == (1000, None, "unknown")
    assert [tuple(r) for r in actors] == [("reviewer", 1000, None, 1000, "unknown")]
    assert "-300" not in out
    assert "-2000" not in out
    assert "$-" not in out
    assert "1000 cost-incomplete tokens" in out and "NOT free" in out


def test_mixed_known_unknown_actor_cost_preserves_exact_incomplete_tokens(tmp_path):
    runs = tmp_path / "runs"
    known = _usage_round()
    known["reviewers"][0]["tokens"] = 1000
    known["reviewers"][0]["cost_usd"] = 0.01
    known["reviewers"][0]["cost_source"] = "estimated"
    known["synthesizer"]["tokens"] = None
    known["synthesizer"]["cost_usd"] = None
    known["producer"] = None
    unknown = _usage_round()
    unknown["reviewers"][0]["tokens"] = 500
    unknown["reviewers"][0]["cost_usd"] = None
    unknown["reviewers"][0]["cost_source"] = "unknown"
    unknown["synthesizer"]["tokens"] = None
    unknown["synthesizer"]["cost_usd"] = None
    unknown["producer"] = None
    _write_run(
        runs,
        "R-mixed",
        rounds=[known, unknown],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    actor = conn.execute(
        "SELECT tokens, cost_usd, cost_incomplete_tokens, cost_source "
        "FROM actor_stats WHERE run_id='R-mixed'"
    ).fetchone()
    assert tuple(actor) == (1500, 0.01, 500, "unknown")
    out = render_report(conn)
    assert "500 cost-incomplete tokens" in out and "NOT free" in out
    assert "cost-incomplete-tok=500" in out
    assert "cost-incomplete-tok=1500" not in out


def test_reviewer_cost_source_merge_is_order_independent(tmp_path):
    known = _usage_round()
    known["reviewers"][0]["tokens"] = 1000
    known["reviewers"][0]["cost_usd"] = 0.01
    known["reviewers"][0]["cost_source"] = "estimated"
    known["synthesizer"]["tokens"] = None
    known["synthesizer"]["cost_usd"] = None
    known["producer"] = None
    unknown = _usage_round()
    unknown["reviewers"][0]["tokens"] = 500
    unknown["reviewers"][0]["cost_usd"] = None
    unknown["reviewers"][0]["cost_source"] = "unknown"
    unknown["synthesizer"]["tokens"] = None
    unknown["synthesizer"]["cost_usd"] = None
    unknown["producer"] = None
    runs = tmp_path / "runs"
    for run_id, rounds in (("R-forward", [known, unknown]), ("R-reverse", [unknown, known])):
        _write_run(
            runs,
            run_id,
            rounds=rounds,
            config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
        )

    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    rows = conn.execute(
        "SELECT run_id, tokens, cost_usd, cost_source FROM reviewer_stats ORDER BY run_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("R-forward", 1500, 0.01, "unknown"),
        ("R-reverse", 1500, 0.01, "unknown"),
    ]


def test_render_report_flags_partial_unknown_cost_not_complete_total(tmp_path):
    runs = tmp_path / "runs"
    rnd = _usage_round()
    rnd["synthesizer"]["cost_usd"] = None
    rnd["synthesizer"]["cost_source"] = "unknown"
    _write_run(
        runs,
        "R-partial-cost",
        rounds=[rnd],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    out = render_report(conn)
    assert "3300" in out
    # The guarantee (unchanged): a partially-unpriced total is NEVER presented as a
    # complete one. The top line flags the unpriced tokens; the per-model line that owns
    # them is labelled `known_cost=`, not `cost=`.
    assert "cost-incomplete tokens" in out and "NOT free" in out
    assert "known_cost=" in out
    assert "300 cost-incomplete tokens" in out and "NOT free" in out


def test_render_report_labels_cost_source_and_scopes_by_model(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R",
        rounds=[_usage_round()],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    out = render_report(conn)
    assert "(estimated)" in out
    assert "by model" in out
    assert "roles=reviewer,synthesizer" in out
    assert "roles=producer" in out


def test_render_report_does_not_round_tiny_nonzero_model_cost_to_zero(tmp_path):
    runs = tmp_path / "runs"
    rnd = _usage_round()
    rnd["reviewers"][0]["cost_usd"] = 0.001
    rnd["synthesizer"]["cost_usd"] = None
    rnd["synthesizer"]["tokens"] = None
    rnd["producer"] = None
    _write_run(
        runs,
        "R-tiny",
        rounds=[rnd],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    out = render_report(conn)
    assert "cost=<$0.01" in out
    assert "cost=$0.00" not in out


def test_render_report_last_n_scopes_spend(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R1",
        rounds=[_usage_round()],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    out = render_report(conn, last_n=1)
    assert "last 1 run(s)" in out


def test_model_less_synth_artifact_gets_unknown_model_not_current_pin(tmp_path):
    """A synth artifact that recorded no model predates model provenance; it must
    be attributed to "" (unknown), NOT to whatever SYNTHESIZER_MODEL currently
    says. Otherwise moving the judge's pin silently rewrites historical
    spend-by-model for every old run in the corpus."""
    from syncade.synthesizer.constants import SYNTHESIZER_MODEL

    runs = tmp_path / "runs"
    rnd = _usage_round()
    del rnd["synthesizer"]["provider"]
    del rnd["synthesizer"]["model"]
    _write_run(runs, "R-legacy", rounds=[rnd])
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    rows = conn.execute(
        "SELECT model FROM actor_stats WHERE run_id='R-legacy' AND role='synthesizer'"
    ).fetchall()
    assert rows, "synthesizer actor_stats row missing"
    assert rows[0][0] != SYNTHESIZER_MODEL, (
        f"model-less synth artifact was attributed to the current pin "
        f"{SYNTHESIZER_MODEL!r}; historical spend grouping must stay stable"
    )
    assert rows[0][0] == ""
