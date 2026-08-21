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
    RoundRow,
    RunRow,
    fetch_runs,
    open_db,
    upsert_actor_stat,
    upsert_reviewer_stat,
    upsert_round,
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


def test_round_count_vector_schema_roundtrips(tmp_path):
    """The round authority stores one four-count vector under one source."""
    conn = open_db(tmp_path / "metrics.db")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(rounds)")}
    assert {"blockers", "minors", "nits", "dismissed", "counts_source"} <= columns
    assert "blockers_source" not in columns

    upsert_round(
        conn,
        RoundRow(
            run_id="R1",
            round=0,
            blockers=1,
            minors=2,
            nits=3,
            dismissed=4,
            counts_source="artifacts",
        ),
    )
    row = conn.execute(
        "SELECT blockers, minors, nits, dismissed, counts_source FROM rounds"
    ).fetchone()
    assert tuple(row) == (1, 2, 3, 4, "artifacts")


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


def test_open_db_rebuilds_v15_round_authority(tmp_path):
    """The immediately preceding derived schema is dropped, not ALTER-migrated."""
    from syncade.metrics.schema import _SCHEMA_VERSION

    db = tmp_path / "m.db"
    stale = sqlite3.connect(str(db))
    stale.executescript(
        "CREATE TABLE rounds ("
        "run_id TEXT NOT NULL, round INTEGER NOT NULL, blockers INTEGER, "
        "blockers_source TEXT, panel_size INTEGER, panel_source TEXT, "
        "PRIMARY KEY (run_id, round)); "
        "INSERT INTO rounds VALUES ('STALE', 0, 9, 'manifest', 2, 'recorded'); "
        "PRAGMA user_version = 15;"
    )
    stale.commit()
    stale.close()

    conn = open_db(db)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION == 16
    columns = {row[1] for row in conn.execute("PRAGMA table_info(rounds)")}
    assert {"minors", "nits", "dismissed", "counts_source"} <= columns
    assert "blockers_source" not in columns
    assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0


def test_open_db_drops_obsolete_reached_rounds_and_round_panels_on_rebuild(tmp_path):
    """An intermediate-schema DB may carry reached_rounds and round_panels.

    These tables were replaced by the single `rounds` authority in this PR.  A
    database that was upgraded to the current user_version while retaining those
    tables would leave stale rows that contradict the new single-authority model.
    The rebuild script must drop them so they cannot survive an upgrade.
    """
    import sqlite3

    db = tmp_path / "m.db"
    intermediate = sqlite3.connect(str(db))
    intermediate.executescript(
        "CREATE TABLE reached_rounds (run_id TEXT, round INTEGER); "
        "CREATE TABLE round_panels (run_id TEXT, round INTEGER, panel_size INTEGER); "
        "INSERT INTO reached_rounds VALUES ('R1', 0); "
        "INSERT INTO round_panels VALUES ('R1', 0, 2); "
    )
    intermediate.commit()
    intermediate.close()

    open_db(db)

    check = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "reached_rounds" not in tables, "stale reached_rounds must be dropped on rebuild"
        assert "round_panels" not in tables, "stale round_panels must be dropped on rebuild"
    finally:
        check.close()


def test_open_db_drops_obsolete_tables_even_at_current_schema_version(tmp_path):
    """A DB already at the current user_version can still carry stale reached_rounds/round_panels.

    The version-check rebuild path only runs for OLDER versions, so a DB that accumulated
    these tables before the cleanup was introduced would survive the version bump untouched.
    The unconditional DROP TABLE IF EXISTS outside the version check handles this case.
    """
    import sqlite3

    from syncade.metrics.schema import _SCHEMA_VERSION

    db = tmp_path / "m.db"
    contaminated = sqlite3.connect(str(db))
    contaminated.executescript(
        f"PRAGMA user_version = {_SCHEMA_VERSION}; "
        "CREATE TABLE reached_rounds (run_id TEXT, round INTEGER); "
        "CREATE TABLE round_panels (run_id TEXT, round INTEGER, panel_size INTEGER); "
        "INSERT INTO reached_rounds VALUES ('R1', 0); "
        "INSERT INTO round_panels VALUES ('R1', 0, 2); "
    )
    contaminated.commit()
    contaminated.close()

    open_db(db)

    check = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "reached_rounds" not in tables, (
            "stale reached_rounds must be dropped even when user_version is current"
        )
        assert "round_panels" not in tables, (
            "stale round_panels must be dropped even when user_version is current"
        )
    finally:
        check.close()


def test_open_db_refuses_a_newer_schema_db(tmp_path):
    # A NEWER syncade may hold data not rebuildable from artifacts (e.g. future
    # columns).  An older binary must refuse rather than return a writable
    # connection that backfill() can use to delete rows it cannot rebuild.
    db = tmp_path / "m.db"
    newer = sqlite3.connect(str(db))
    newer.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, verdict TEXT); PRAGMA user_version = 999;"
    )
    newer.execute("INSERT INTO runs (run_id, verdict) VALUES ('KEEP', 'SHIP')")
    newer.commit()
    newer.close()

    with pytest.raises(sqlite3.DatabaseError, match="schema version 999"):
        open_db(db)

    # The refusal must happen before any DDL: future data must survive intact.
    check = sqlite3.connect(str(db))
    try:
        row = check.execute("SELECT run_id FROM runs WHERE run_id = 'KEEP'").fetchone()
        assert row is not None, "open_db must not mutate a newer-schema DB before refusing"
        assert check.execute("PRAGMA user_version").fetchone()[0] == 999, (
            "open_db must not alter user_version of a newer-schema DB"
        )
    finally:
        check.close()


def test_open_db_refuses_newer_schema_before_unconditional_ddl(tmp_path):
    # The unconditional DROP TABLE IF EXISTS reached_rounds / round_panels must
    # not run against a newer schema — those names may be legitimate future tables.
    db = tmp_path / "m.db"
    future = sqlite3.connect(str(db))
    future.executescript(
        "CREATE TABLE reached_rounds (x INTEGER); "
        "INSERT INTO reached_rounds VALUES (42); "
        "PRAGMA user_version = 999;"
    )
    future.commit()
    future.close()

    with pytest.raises(sqlite3.DatabaseError):
        open_db(db)

    check = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "reached_rounds" in tables, "future table must survive a refused open"
        count = check.execute("SELECT COUNT(*) FROM reached_rounds").fetchone()[0]
        assert count == 1, "future table rows must survive a refused open"
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
