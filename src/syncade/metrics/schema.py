"""Metrics DB schema + sink (stdlib ``sqlite3`` only).

Three tables — one row per run (``runs``), one per run/reviewer/model
(``reviewer_stats``), and one per usage-emitting actor/model (``actor_stats``).
Writes are idempotent upserts keyed on the primary key,
so re-aggregating the corpus rebuilds the same rows rather than duplicating
them. ``tokens`` / ``cost_usd`` (on both tables) are populated by PR-4 from each
run's manifests; legacy runs with no usage data stay NULL.

The row shapes are stdlib dataclasses (not pydantic): the data comes from
syncade's own artifacts, not an untrusted boundary, so validation buys nothing
here. Columns are generated from the dataclass fields, so the DDL below and the
dataclasses must stay aligned — the schema tests fail loudly if they drift.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path


@dataclasses.dataclass
class RunRow:
    """One aggregated row per syncade run. ``run_id`` is the primary key."""

    run_id: str
    verdict: str = ""
    rounds_executed: int = 0
    blockers: int = 0
    minors: int = 0
    nits: int = 0
    dismissed: int = 0
    final_exit_code: int | None = None
    termination_reason: str | None = None
    operator_branch: str | None = None
    handoff: int = 0
    decision_needed: int = 0
    producer_commits: int = 0
    producer_stalled: int = 0
    producer_errors: int = 0
    producer_escalated: int = 0
    tokens: int | None = None  # reserved for PR-4
    cost_usd: float | None = None  # reserved for PR-4


@dataclasses.dataclass
class ReviewerStatRow:
    """One row per (run, reviewer). PK is (``run_id``, ``name``)."""

    run_id: str
    name: str
    provider: str = ""
    model: str = ""
    finding_count: int = 0
    duration_s: float = 0.0
    tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str = ""  # "provider" | "estimated" | "unknown" | "" (no usage)


@dataclasses.dataclass
class ActorStatRow:
    """One row per usage-emitting actor. PK is
    (``run_id``, ``role``, ``name``, ``provider``, ``model``, ``auth_mode``) — auth_mode is
    part of the key so one actor that ran under two modes keeps a row per mode (PR-v2-24)."""

    run_id: str
    role: str
    name: str
    provider: str = ""
    model: str = ""
    tokens: int | None = None
    cost_usd: float | None = None
    cost_incomplete_tokens: int = 0
    cost_source: str = ""  # "provider" | "estimated" | "unknown" | "" (no usage)
    auth_mode: str = ""  # "subscription" | "api" | "none" | "unknown" | "" (legacy run)
    rounds_with_usage: int = 0  # rounds with any usage reported; 0 = legacy/unknown


_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    verdict TEXT,
    rounds_executed INTEGER,
    blockers INTEGER,
    minors INTEGER,
    nits INTEGER,
    dismissed INTEGER,
    final_exit_code INTEGER,
    termination_reason TEXT,
    operator_branch TEXT,
    handoff INTEGER,
    decision_needed INTEGER,
    producer_commits INTEGER,
    producer_stalled INTEGER,
    producer_errors INTEGER,
    producer_escalated INTEGER,
    tokens INTEGER,
    cost_usd REAL
)
"""

_REVIEWER_STATS_DDL = """
CREATE TABLE IF NOT EXISTS reviewer_stats (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    finding_count INTEGER,
    duration_s REAL,
    tokens INTEGER,
    cost_usd REAL,
    cost_source TEXT,
    PRIMARY KEY (run_id, name, provider, model)
)
"""

_ACTOR_STATS_DDL = """
CREATE TABLE IF NOT EXISTS actor_stats (
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    tokens INTEGER,
    cost_usd REAL,
    cost_incomplete_tokens INTEGER,
    cost_source TEXT,
    -- PR-v2-24. cost_usd is an API-EQUIVALENT VALUATION, not spend: it is fiction
    -- whenever the call rode a subscription. Only the resolved auth mode knows which,
    -- and it is orthogonal to cost_source -- claude reports total_cost_usd even on an
    -- OAuth session. Legacy rows stay NULL, and NULL reports as neither billed nor
    -- free, because cost is never fabricated.
    -- auth_mode is part of the KEY, not a merged attribute. Keyed without it, one actor
    -- that ran under two modes (a run resumed with a changed env) collapsed to "mixed"
    -- and its REAL API SPEND became unclassified before billing ever saw it. Fixing the
    -- reader was not enough while the writer still destroyed the split -- the key is what
    -- makes the loss impossible rather than merely unlikely.
    auth_mode TEXT NOT NULL DEFAULT '',
    -- Rounds where this actor reported any usage (tokens or cost non-null). Used by
    -- the cost preview to detect incomplete multi-round coverage (0 = legacy/unknown).
    rounds_with_usage INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, role, name, provider, model, auth_mode)
)
"""


# Bump when the table shapes change. The DB is a derived, rebuildable view, so an
# OLDER on-disk schema is dropped + recreated (backfill repopulates from the
# corpus) rather than ALTER-migrated. A NEWER on-disk schema is left untouched —
# it may hold data this binary can't rebuild (e.g. PR-4's live token/cost).
_SCHEMA_VERSION = 12


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open the metrics DB, creating/rebuilding tables to the current schema.

    Creates parent dirs + tables if absent; if the on-disk schema version is
    stale (an older syncade wrote it), the derived tables are dropped and
    recreated so a later ``upsert`` never hits a missing column.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
            # Older schema only: rebuild. A newer on-disk version is left as-is so an
            # older binary never destroys data it cannot rebuild (see _SCHEMA_VERSION).
            conn.executescript(
                "DROP TABLE IF EXISTS runs; "
                "DROP TABLE IF EXISTS reviewer_stats; "
                "DROP TABLE IF EXISTS actor_stats;"
            )
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")  # int constant, not user input
        conn.execute(_RUNS_DDL)
        conn.execute(_REVIEWER_STATS_DDL)
        conn.execute(_ACTOR_STATS_DDL)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def _upsert(conn: sqlite3.Connection, table: str, row: object) -> None:
    # ``table`` is a hardcoded literal and the column names come from our own
    # dataclass fields (never user input); values are bound parameters. No
    # injection surface.
    data = dataclasses.asdict(row)
    cols = ", ".join(data)
    placeholders = ", ".join(f":{k}" for k in data)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", data)
    conn.commit()


def upsert_run(conn: sqlite3.Connection, row: RunRow) -> None:
    _upsert(conn, "runs", row)


def upsert_reviewer_stat(conn: sqlite3.Connection, row: ReviewerStatRow) -> None:
    _upsert(conn, "reviewer_stats", row)


def upsert_actor_stat(conn: sqlite3.Connection, row: ActorStatRow) -> None:
    _upsert(conn, "actor_stats", row)


def fetch_runs(conn: sqlite3.Connection) -> list[RunRow]:
    """Return all run rows as :class:`RunRow`, ordered by ``run_id``."""
    return [RunRow(**dict(r)) for r in conn.execute("SELECT * FROM runs ORDER BY run_id")]


def fetch_actor_stats(conn: sqlite3.Connection) -> list[ActorStatRow]:
    """Return all per-actor rows as :class:`ActorStatRow`, ordered by ``run_id``. Lets a
    consumer separate the per-role cost of a run (e.g. reviewers + judge, which run every
    round, from the producer, which runs only on NO-SHIP rounds)."""
    return [
        ActorStatRow(**dict(r)) for r in conn.execute("SELECT * FROM actor_stats ORDER BY run_id")
    ]
