"""Schema + sink contract for the metrics DB (PR-v2-03 Task 1).

The sink is stdlib sqlite3 only. It stores one row per run plus per-reviewer
stats, and re-aggregation must be idempotent (keyed on run_id / (run_id, name))
plus artifact provider/model identity) so `syncade --metrics` can rebuild from
the corpus without duplicating rows.
"""

from __future__ import annotations

import gc
import sqlite3

import pytest

from syncade.metrics.schema import (
    ActorStatRow,
    ReviewerStatRow,
    RunRow,
    fetch_runs,
    open_db,
    upsert_actor_stat,
    upsert_reviewer_stat,
    upsert_run,
)


def test_open_db_creates_expected_tables(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "reviewer_stats", "actor_stats"} <= tables


def test_open_db_reserves_nullable_token_cost_columns(tmp_path):
    # PR-4 populates these later; v1 must ship the columns as NULL-able so the
    # schema is forward-compatible without a migration.
    conn = open_db(tmp_path / "metrics.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"tokens", "cost_usd"} <= cols


def test_open_db_creates_actor_usage_table(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(actor_stats)")}
    assert {
        "run_id",
        "role",
        "name",
        "provider",
        "model",
        "tokens",
        "cost_usd",
        "cost_incomplete_tokens",
    } <= cols


def test_open_db_rebuilds_when_schema_is_stale(tmp_path):
    # The DB is a derived, rebuildable view. A db written by an older syncade
    # (missing newer columns) must be rebuilt on open, not crash later on upsert.
    import sqlite3

    db = tmp_path / "m.db"
    stale = sqlite3.connect(str(db))
    stale.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, verdict TEXT)"
    )  # pre-migration shape
    stale.commit()
    stale.close()

    conn = open_db(db)
    upsert_run(conn, RunRow(run_id="R1", verdict="SHIP", blockers=3))  # uses current columns
    rows = fetch_runs(conn)
    assert len(rows) == 1
    assert rows[0].blockers == 3


def test_open_db_does_not_wipe_a_newer_schema_db(tmp_path):
    # A NEWER syncade may hold data not rebuildable from artifacts (e.g. PR-4's
    # live token/cost). An older binary opening that db must NOT drop its tables.
    import sqlite3

    db = tmp_path / "m.db"
    newer = sqlite3.connect(str(db))
    newer.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, verdict TEXT); PRAGMA user_version = 999;"
    )
    newer.execute("INSERT INTO runs (run_id, verdict) VALUES ('KEEP', 'SHIP')")
    newer.commit()
    newer.close()

    open_db(db)  # this (older) binary's version is < 999
    check = sqlite3.connect(str(db))
    try:
        assert check.execute("SELECT run_id FROM runs WHERE run_id = 'KEEP'").fetchone() is not None
    finally:
        check.close()


def test_open_db_closes_connection_when_schema_check_fails(tmp_path):
    db = tmp_path / "corrupt.db"
    db.write_text("not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        open_db(db)
    gc.collect()


def test_upsert_run_roundtrips(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    upsert_run(
        conn,
        RunRow(
            run_id="2026-07-01T09-12-47",
            verdict="SHIP",
            rounds_executed=3,
            blockers=2,
            minors=4,
            nits=0,
            final_exit_code=0,
        ),
    )
    rows = fetch_runs(conn)
    assert len(rows) == 1
    assert rows[0].run_id == "2026-07-01T09-12-47"
    assert rows[0].verdict == "SHIP"
    assert rows[0].rounds_executed == 3
    assert rows[0].blockers == 2
    # unset numeric reserved columns default to None, not 0
    assert rows[0].tokens is None
    assert rows[0].cost_usd is None


def test_upsert_run_is_idempotent_by_run_id(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    upsert_run(conn, RunRow(run_id="R1", verdict="NO-SHIP", blockers=1))
    # same run re-aggregated with corrected numbers -> replace, not duplicate
    upsert_run(conn, RunRow(run_id="R1", verdict="SHIP", blockers=5))
    rows = fetch_runs(conn)
    assert len(rows) == 1
    assert rows[0].blockers == 5
    assert rows[0].verdict == "SHIP"


def test_upsert_reviewer_stat_is_idempotent_by_run_name_and_model(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    upsert_run(conn, RunRow(run_id="R1"))
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(run_id="R1", name="codex-reviewer", provider="openai", finding_count=1),
    )
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(run_id="R1", name="codex-reviewer", provider="openai", finding_count=9),
    )
    n, fc = conn.execute(
        "SELECT COUNT(*), MAX(finding_count) FROM reviewer_stats "
        "WHERE run_id='R1' AND name='codex-reviewer'"
    ).fetchone()
    assert n == 1
    assert fc == 9


def test_upsert_reviewer_stat_splits_same_name_by_model(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    upsert_run(conn, RunRow(run_id="R1"))
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(
            run_id="R1",
            name="codex-reviewer",
            provider="openai",
            model="gpt-5.4",
            tokens=100,
        ),
    )
    upsert_reviewer_stat(
        conn,
        ReviewerStatRow(
            run_id="R1",
            name="codex-reviewer",
            provider="openai",
            model="gpt-5.5",
            tokens=300,
        ),
    )
    rows = conn.execute(
        "SELECT model, tokens FROM reviewer_stats "
        "WHERE run_id='R1' AND name='codex-reviewer' ORDER BY model"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("gpt-5.4", 100), ("gpt-5.5", 300)]


def test_upsert_actor_stat_is_idempotent_by_run_role_name_and_model(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    upsert_run(conn, RunRow(run_id="R1"))
    upsert_actor_stat(
        conn,
        ActorStatRow(
            run_id="R1",
            role="synthesizer",
            name="synthesizer",
            provider="openai",
            model="gpt-5.5",
            tokens=100,
        ),
    )
    upsert_actor_stat(
        conn,
        ActorStatRow(
            run_id="R1",
            role="synthesizer",
            name="synthesizer",
            provider="openai",
            model="gpt-5.5",
            tokens=300,
        ),
    )
    n, tok = conn.execute(
        "SELECT COUNT(*), MAX(tokens) FROM actor_stats "
        "WHERE run_id='R1' AND role='synthesizer' AND name='synthesizer'"
    ).fetchone()
    assert n == 1
    assert tok == 300


def test_upsert_actor_stat_splits_same_name_by_model(tmp_path):
    conn = open_db(tmp_path / "metrics.db")
    upsert_run(conn, RunRow(run_id="R1"))
    upsert_actor_stat(
        conn,
        ActorStatRow(
            run_id="R1",
            role="synthesizer",
            name="synthesizer",
            provider="openai",
            model="gpt-5.4",
            tokens=100,
        ),
    )
    upsert_actor_stat(
        conn,
        ActorStatRow(
            run_id="R1",
            role="synthesizer",
            name="synthesizer",
            provider="openai",
            model="gpt-5.5",
            tokens=300,
        ),
    )
    rows = conn.execute(
        "SELECT model, tokens FROM actor_stats "
        "WHERE run_id='R1' AND role='synthesizer' AND name='synthesizer' ORDER BY model"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("gpt-5.4", 100), ("gpt-5.5", 300)]
