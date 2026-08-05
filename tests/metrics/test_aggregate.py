"""Aggregator contract (PR-v2-03 Task 2): map the run-artifact corpus to rows.

Reads ``loop-manifest.json`` + ``run-init.json`` + handoff/decision presence,
tolerating legacy/partial runs (missing fields default, never crash), and
backfills idempotently. Fixtures mirror the real artifact schemas verified in
the brief.
"""

from __future__ import annotations

import json
import shutil

from syncade.metrics.aggregate import backfill, read_run
from syncade.metrics.schema import fetch_runs, open_db


def _round(*, exit_code, blockers=0, minors=0, nits=0, dismissed=0, reviewers=None):
    return {
        "round": 0,
        "round_exit_code": exit_code,
        "snapshot": {"commit_sha": "s"},
        "reviewers": reviewers or [],
        "synthesizer": {
            "outcome": "success",
            "active_blocker_count": blockers,
            "active_minor_count": minors,
            "active_nit_count": nits,
            "dismissed_count": dismissed,
        },
        "producer": None,
    }


def _write_run(runs_root, run_id, *, exit_code, rounds, handoff=False, config_reviewers=None):
    d = runs_root / run_id
    d.mkdir(parents=True)
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "final_exit_code": exit_code,
                "final_round": len(rounds) - 1,
                "max_rounds": 3,
                "started_at_utc": "2026-07-01T09:12:47",
                "syncade_version": "0.1.0",
                "termination_reason": "ship" if exit_code == 0 else "findings_present",
                "rounds": rounds,
            }
        )
    )
    (d / "run-init.json").write_text(
        json.dumps(
            {
                "operator_branch": "main",
                "base_ref": "main",
                "pr_doc_path": "docs/x.md",
                "starting_sha": "abc123",
                "config_snapshot": {"reviewers": config_reviewers or []},
            }
        )
    )
    if handoff:
        (d / "handoff.md").write_text("# handoff")
    return d


def test_read_run_maps_ship_verdict_and_sums_severity_across_rounds(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R-ship",
        exit_code=0,
        rounds=[_round(exit_code=30, blockers=2, minors=4), _round(exit_code=0, minors=1, nits=3)],
    )
    row, _ = read_run(runs / "R-ship")
    assert row.run_id == "R-ship"
    assert row.verdict == "SHIP"
    assert row.rounds_executed == 2
    assert row.blockers == 2
    assert row.minors == 5  # 4 + 1
    assert row.nits == 3
    assert row.final_exit_code == 0
    assert row.termination_reason == "ship"
    assert row.operator_branch == "main"


def test_read_run_maps_no_ship(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs, "R-noship", exit_code=30, rounds=[_round(exit_code=30, blockers=1)])
    row, _ = read_run(runs / "R-noship")
    assert row.verdict == "NO-SHIP"
    assert row.blockers == 1


def test_read_run_maps_no_change_verdict(tmp_path):
    """Regression: a no_changes_to_review run must map to NO-CHANGE, not SHIP."""
    runs = tmp_path / "runs"
    d = runs / "R-nochange"
    d.mkdir(parents=True)
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "R-nochange",
                "final_exit_code": 0,
                "final_round": 0,
                "max_rounds": 3,
                "started_at_utc": "2026-08-01T09:00:00",
                "syncade_version": "0.1.0",
                "termination_reason": "no_changes_to_review",
                "rounds": [_round(exit_code=0)],
            }
        )
    )
    (d / "run-init.json").write_text(
        json.dumps(
            {
                "operator_branch": "work",
                "base_ref": "HEAD",
                "pr_doc_path": "pr.md",
                "starting_sha": "abc123",
                "config_snapshot": {"reviewers": []},
            }
        )
    )
    row, _ = read_run(d)
    assert row.verdict == "NO-CHANGE", (
        f"expected NO-CHANGE verdict for no_changes_to_review run, got {row.verdict!r}"
    )
    assert row.final_exit_code == 0
    assert row.termination_reason == "no_changes_to_review"


def test_read_run_flags_handoff_and_decision(tmp_path):
    runs = tmp_path / "runs"
    d = _write_run(runs, "R-h", exit_code=10, rounds=[_round(exit_code=10)], handoff=True)
    (d / "decision-needed.md").write_text("# decide")
    row, _ = read_run(d)
    assert row.handoff == 1
    assert row.decision_needed == 1


def test_read_run_tolerates_missing_run_init_and_partial_rounds(tmp_path):
    runs = tmp_path / "runs"
    d = runs / "R-partial"
    d.mkdir(parents=True)
    # no run-init.json; a round with no synthesizer block at all
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "R-partial",
                "final_exit_code": 0,
                "rounds": [{"round": 0, "round_exit_code": 0}],
            }
        )
    )
    row, stats = read_run(d)  # must NOT raise
    assert row.run_id == "R-partial"
    assert row.blockers == 0  # missing synth -> 0
    assert row.operator_branch is None  # missing run-init -> None
    assert stats == []


def test_backfill_upserts_all_runs_idempotently(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs, "R1", exit_code=0, rounds=[_round(exit_code=0)])
    _write_run(runs, "R2", exit_code=30, rounds=[_round(exit_code=30, blockers=3)])
    conn = open_db(tmp_path / "metrics.db")
    backfill(conn, runs)
    backfill(conn, runs)  # second pass must not duplicate
    rows = fetch_runs(conn)
    assert [r.run_id for r in rows] == ["R1", "R2"]
    assert rows[1].blockers == 3


def test_read_run_aggregates_reviewer_stats_with_model_join(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R-rev",
        exit_code=0,
        rounds=[
            _round(
                exit_code=0,
                reviewers=[
                    {
                        "name": "codex-reviewer",
                        "provider": "openai",
                        "verdict": "SHIP",
                        "finding_count": 1,
                        "duration_seconds": 200.0,
                    }
                ],
            )
        ],
        config_reviewers=[{"name": "codex-reviewer", "provider": "openai", "model": "gpt-5.5"}],
    )
    _, stats = read_run(runs / "R-rev")
    assert len(stats) == 1
    assert stats[0].name == "codex-reviewer"
    assert stats[0].provider == "openai"
    assert stats[0].finding_count == 1
    # model is NOT in the manifest reviewer entry — it comes from the run-init
    # config_snapshot roster, joined by name.
    assert stats[0].model == "gpt-5.5"
    # wall-clock is summed across rounds (folds in the usefulness headline)
    assert stats[0].duration_s == 200.0


def test_read_run_returns_none_on_corrupt_manifest(tmp_path):
    d = tmp_path / "runs" / "R-bad"
    d.mkdir(parents=True)
    (d / "loop-manifest.json").write_text("{not valid json")  # present but unparseable
    # corrupt (not merely partial) -> skip signal, not a bogus UNKNOWN run
    assert read_run(d) is None


def test_backfill_skips_corrupt_manifest_run(tmp_path, capsys):
    runs = tmp_path / "runs"
    _write_run(runs, "R-ok", exit_code=0, rounds=[_round(exit_code=0)])
    bad = runs / "R-bad"
    bad.mkdir(parents=True)
    (bad / "loop-manifest.json").write_text("{corrupt")
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    assert {r.run_id for r in fetch_runs(conn)} == {"R-ok"}  # corrupt run not counted
    assert "R-bad" in capsys.readouterr().err  # and warned


def test_backfill_prunes_runs_removed_from_corpus(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs, "R1", exit_code=0, rounds=[_round(exit_code=0)])
    _write_run(runs, "R2", exit_code=30, rounds=[_round(exit_code=30, blockers=3)])
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    assert {r.run_id for r in fetch_runs(conn)} == {"R1", "R2"}
    shutil.rmtree(runs / "R2")  # e.g. --gc pruned it from the corpus
    backfill(conn, runs)
    assert {r.run_id for r in fetch_runs(conn)} == {"R1"}  # stale row pruned, not left behind
    assert conn.execute("SELECT COUNT(*) FROM reviewer_stats WHERE run_id='R2'").fetchone()[0] == 0


def test_backfill_refreshes_reviewer_roster_for_a_run(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R1",
        exit_code=0,
        rounds=[
            _round(
                exit_code=0,
                reviewers=[
                    {"name": "rev-a", "provider": "openai", "finding_count": 1},
                    {"name": "rev-b", "provider": "openai", "finding_count": 0},
                ],
            )
        ],
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    assert conn.execute("SELECT COUNT(*) FROM reviewer_stats WHERE run_id='R1'").fetchone()[0] == 2
    # rev-b removed from the run's artifacts; the stale reviewer row must drop
    (runs / "R1" / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "R1",
                "final_exit_code": 0,
                "rounds": [
                    {
                        "round": 0,
                        "round_exit_code": 0,
                        "reviewers": [{"name": "rev-a", "provider": "openai", "finding_count": 1}],
                    }
                ],
            }
        )
    )
    backfill(conn, runs)
    names = [r[0] for r in conn.execute("SELECT name FROM reviewer_stats WHERE run_id='R1'")]
    assert names == ["rev-a"]


def test_read_run_tolerates_malformed_scalar_fields(tmp_path):
    # A PARSEABLE manifest with wrong-typed scalars must be coerced, not crash
    # (one bad historical artifact must never take down the whole dashboard).
    d = tmp_path / "runs" / "R-weird"
    d.mkdir(parents=True)
    (d / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": ["not", "a", "string"],
                "final_exit_code": "oops",
                "termination_reason": {"nested": 1},
                "rounds": [
                    {
                        "round": 0,
                        "synthesizer": {"active_blocker_count": "5", "active_minor_count": [1]},
                    },
                    "not-a-round-dict",
                ],
            }
        )
    )
    row, stats = read_run(d)  # must NOT raise
    assert row.run_id == "R-weird"  # non-str run_id -> falls back to dir name
    assert row.verdict == "UNKNOWN"  # non-int exit -> None -> UNKNOWN
    assert row.blockers == 0  # "5" (str) and [1] (list) defaulted, not summed
    assert row.minors == 0
    assert row.final_exit_code is None
    assert row.termination_reason is None
    assert stats == []


def test_read_run_tolerates_non_iterable_config_reviewers(tmp_path):
    d = tmp_path / "runs" / "R2"
    d.mkdir(parents=True)
    (d / "loop-manifest.json").write_text(
        json.dumps({"run_id": "R2", "final_exit_code": 0, "rounds": []})
    )
    (d / "run-init.json").write_text(
        json.dumps({"config_snapshot": {"reviewers": 123}})
    )  # not a list
    row, stats = read_run(d)  # must NOT raise on a non-iterable reviewer roster
    assert row.run_id == "R2"
    assert stats == []


def test_backfill_survives_one_malformed_run(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs, "R-ok", exit_code=0, rounds=[_round(exit_code=0, blockers=2)])
    bad = runs / "R-weird"
    bad.mkdir(parents=True)
    (bad / "loop-manifest.json").write_text(
        json.dumps(
            {
                "run_id": "R-weird",
                "final_exit_code": "x",
                "rounds": [{"synthesizer": {"active_blocker_count": [1, 2]}}],
            }
        )
    )
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)  # one bad artifact must not take down the whole aggregation
    rows = {r.run_id: r for r in fetch_runs(conn)}
    assert rows["R-ok"].blockers == 2  # good run aggregates correctly
    assert rows["R-weird"].blockers == 0  # malformed scalars defaulted; run still counted


def test_read_run_captures_dismissed_count(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R1",
        exit_code=0,
        rounds=[_round(exit_code=30, dismissed=2), _round(exit_code=0, dismissed=1)],
    )
    row, _ = read_run(runs / "R1")
    assert row.dismissed == 3  # summed across rounds


def test_read_run_counts_producer_commits(tmp_path):
    runs = tmp_path / "runs"
    r0 = _round(exit_code=30)
    r0["producer"] = {"outcome": "committed", "provider": "anthropic"}
    r1 = _round(exit_code=0)
    r1["producer"] = {"outcome": "committed"}
    _write_run(runs, "R1", exit_code=0, rounds=[r0, r1])
    row, _ = read_run(runs / "R1")
    assert row.producer_commits == 2  # both rounds' producers committed


def test_read_run_tracks_all_producer_outcomes(tmp_path):
    # Regression: stalled/subprocess_error/escalated rounds were silently dropped.
    runs = tmp_path / "runs"
    r0 = _round(exit_code=30)
    r0["producer"] = {"outcome": "committed"}
    r1 = _round(exit_code=30)
    r1["producer"] = {"outcome": "stalled"}
    r2 = _round(exit_code=30)
    r2["producer"] = {"outcome": "subprocess_error"}
    r3 = _round(exit_code=30)
    r3["producer"] = {"outcome": "escalated"}
    _write_run(runs, "R1", exit_code=30, rounds=[r0, r1, r2, r3])
    row, _ = read_run(runs / "R1")
    assert row.producer_commits == 1
    assert row.producer_stalled == 1
    assert row.producer_errors == 1
    assert row.producer_escalated == 1


def test_read_runs_includes_partial_run_with_only_run_init(tmp_path):
    # Regression: read_runs() filtered out dirs with no loop-manifest.json before
    # read_run() could apply its missing-manifest fallback, so interrupted runs
    # (run-init.json present, loop-manifest absent) were silently omitted.
    runs = tmp_path / "runs"
    d = runs / "R-partial"
    d.mkdir(parents=True)
    (d / "run-init.json").write_text(
        json.dumps({"operator_branch": "main", "config_snapshot": {"reviewers": []}})
    )
    from syncade.metrics.aggregate import read_runs

    results = read_runs(runs)
    assert len(results) == 1
    row, _ = results[0]
    assert row.verdict == "UNKNOWN"


def test_backfill_warns_on_present_but_malformed_run_init(tmp_path, capsys):
    runs = tmp_path / "runs"
    d = _write_run(runs, "R1", exit_code=0, rounds=[_round(exit_code=0)])
    (d / "run-init.json").write_text("{not valid json")  # present but unparseable
    conn = open_db(tmp_path / "m.db")
    backfill(conn, runs)
    assert {r.run_id for r in fetch_runs(conn)} == {"R1"}  # run still counted (tolerated)
    assert "R1" in capsys.readouterr().err  # but the bad join source is warned, not silent


# --- PR-v2-04: token/cost aggregation ---------------------------------------


def _usage_round(exit_code=0):
    return {
        "round": 0,
        "round_exit_code": exit_code,
        "snapshot": {"commit_sha": "s"},
        "reviewers": [
            {
                "name": "cdx",
                "provider": "openai",
                "finding_count": 1,
                "duration_seconds": 1.0,
                "tokens": 1000,
                "cost_usd": 0.01,
                "cost_source": "estimated",
            }
        ],
        "synthesizer": {
            "outcome": "success",
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


def test_read_run_sums_usage_across_reviewer_synth_producer(tmp_path):
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "R-usage",
        exit_code=0,
        rounds=[_usage_round()],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )
    row, revs = read_run(runs / "R-usage")
    assert row.tokens == 1000 + 300 + 2000
    assert round(row.cost_usd, 6) == round(0.01 + 0.005 + 0.02, 6)
    assert len(revs) == 1
    assert revs[0].tokens == 1000 and revs[0].cost_usd == 0.01


def test_read_run_rejects_negative_persisted_usage(tmp_path):
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
        exit_code=0,
        rounds=[rnd],
        config_reviewers=[{"name": "cdx", "model": "gpt-5.5"}],
    )

    row, revs = read_run(runs / "R-negative")

    assert row.tokens == 1000
    assert row.cost_usd is None
    assert len(revs) == 1
    assert revs[0].tokens == 1000
    assert revs[0].cost_usd is None
    assert revs[0].cost_source == "unknown"


def test_read_run_treats_scalar_reviewers_as_empty_for_usage_sum(tmp_path):
    runs = tmp_path / "runs"
    rnd = _usage_round()
    rnd["reviewers"] = 7
    _write_run(
        runs,
        "R-scalar-reviewers",
        exit_code=0,
        rounds=[rnd],
    )

    row, revs = read_run(runs / "R-scalar-reviewers")

    assert row.tokens == 300 + 2000
    assert round(row.cost_usd, 6) == round(0.005 + 0.02, 6)
    assert revs == []


def test_read_run_legacy_no_usage_stays_null(tmp_path):
    runs = tmp_path / "runs"
    _write_run(runs, "R-legacy", exit_code=0, rounds=[_round(exit_code=0)])
    row, _ = read_run(runs / "R-legacy")
    assert row.tokens is None and row.cost_usd is None


def test_schema_bump_rebuilds_stale_v4_db(tmp_path):
    import sqlite3

    from syncade.metrics.schema import _SCHEMA_VERSION

    db = tmp_path / "m.db"
    # A pre-PR-4 (v4) reviewer_stats table WITHOUT the usage columns + a stale row.
    raw = sqlite3.connect(str(db))
    raw.execute("CREATE TABLE reviewer_stats (run_id TEXT, name TEXT, PRIMARY KEY (run_id, name))")
    raw.execute("INSERT INTO reviewer_stats (run_id, name) VALUES ('old', 'x')")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()
    # Opening at the current version drops + recreates: stale row gone, usage cols present.
    conn = open_db(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM reviewer_stats").fetchone()[0] == 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reviewer_stats)")}
    assert {"tokens", "cost_usd", "cost_source"} <= cols
    actor_cols = {r[1] for r in conn.execute("PRAGMA table_info(actor_stats)")}
    assert {
        "role",
        "name",
        "provider",
        "model",
        "tokens",
        "cost_usd",
        "cost_incomplete_tokens",
        "cost_source",
    } <= actor_cols
