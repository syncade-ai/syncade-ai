"""Metrics sink for syncade runs (PR-v2-03).

A derived, rebuildable sqlite view over the ``.syncade/runs/`` artifact corpus.
Read-only over run artifacts; stdlib ``sqlite3`` only (no new dependency).

Public surface is re-exported here, but monkeypatches/tests should target the
concrete submodule that owns the lookup (CLAUDE.md Decomposition Rule).
"""

from __future__ import annotations

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

__all__ = [
    "ActorStatRow",
    "ReviewerStatRow",
    "RunRow",
    "fetch_runs",
    "open_db",
    "upsert_actor_stat",
    "upsert_reviewer_stat",
    "upsert_run",
]
