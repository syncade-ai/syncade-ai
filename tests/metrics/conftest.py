from __future__ import annotations

import sqlite3

import pytest

import syncade.metrics.schema as metrics_schema


@pytest.fixture(autouse=True)
def close_open_db_connections(monkeypatch, request):
    connections: list[sqlite3.Connection] = []
    real_open_db = metrics_schema.open_db

    def tracked_open_db(*args, **kwargs):
        conn = real_open_db(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(metrics_schema, "open_db", tracked_open_db)
    if getattr(request.module, "open_db", None) is real_open_db:
        monkeypatch.setattr(request.module, "open_db", tracked_open_db, raising=False)

    yield

    for conn in reversed(connections):
        conn.close()
