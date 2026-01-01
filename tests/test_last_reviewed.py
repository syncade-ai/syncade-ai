"""PR-B Task 2: per-branch last-reviewed SHA persistence.

`.syncade/last-reviewed.json` is cross-run state (sibling of `runs/`) that bounds
a `--scope since-last-review` diff to new work. Per-branch, survives across runs,
gitignored. These are pure JSON-I/O unit tests (no git needed).
"""

from __future__ import annotations

import json

from syncade.persistence import (
    LAST_REVIEWED_FILENAME,
    persist_last_reviewed,
    read_last_reviewed,
)

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


class TestLastReviewedPersistence:
    def test_round_trip(self, tmp_path):
        persist_last_reviewed(
            tmp_path, branch="main", sha=_SHA_A, run_id="r1", recorded_at_utc="2026-05-31T00:00:00Z"
        )
        assert read_last_reviewed(tmp_path, "main") == _SHA_A

    def test_file_location_under_dot_syncade(self, tmp_path):
        path = persist_last_reviewed(
            tmp_path, branch="main", sha=_SHA_A, run_id="r1", recorded_at_utc="t"
        )
        assert path == tmp_path / ".syncade" / LAST_REVIEWED_FILENAME
        assert path.is_file()
        payload = json.loads(path.read_text())
        assert payload["main"]["sha"] == _SHA_A
        assert payload["main"]["run_id"] == "r1"

    def test_per_branch_isolation(self, tmp_path):
        persist_last_reviewed(tmp_path, branch="main", sha=_SHA_A, run_id="r1", recorded_at_utc="t")
        persist_last_reviewed(
            tmp_path, branch="feature", sha=_SHA_B, run_id="r2", recorded_at_utc="t"
        )
        # Re-record main; feature must be untouched.
        persist_last_reviewed(tmp_path, branch="main", sha=_SHA_C, run_id="r3", recorded_at_utc="t")
        assert read_last_reviewed(tmp_path, "main") == _SHA_C
        assert read_last_reviewed(tmp_path, "feature") == _SHA_B

    def test_read_missing_file_returns_none(self, tmp_path):
        assert read_last_reviewed(tmp_path, "main") is None

    def test_read_unknown_branch_returns_none(self, tmp_path):
        persist_last_reviewed(tmp_path, branch="main", sha=_SHA_A, run_id="r1", recorded_at_utc="t")
        assert read_last_reviewed(tmp_path, "other") is None
