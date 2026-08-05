"""Tests for :mod:`syncade.persistence`.

Constructs :class:`ReviewerRunResult` / :class:`DispatchResult` /
:class:`Snapshot` / :class:`SubprocessResult` directly — no real
subprocess calls or git operations. The orchestrator is the only
production caller; these tests target the persistence module in
isolation so a future regression in file-layout, JSON shape, or
manifest schema fails here rather than at the integration boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.persistence._helpers import _FIXED_STARTED_AT


class TestPersistRunInit:
    """PR-16: ``persist_run_init`` writes ``<run_dir>/run-init.json``
    at the very start of every fresh ``syncade <pr-doc>`` invocation
    so ``--resume <run-id>`` has enough context to validate eligibility
    and to surface what the original run was launched with.
    """

    def _make_run_dir(self, tmp_path: Path) -> Path:
        run_dir = tmp_path / ".syncade" / "runs" / "2026-05-28T10-15-23"
        run_dir.mkdir(parents=True)
        return run_dir

    def _config(self):
        # Construct with all defaults; the test pins the SHAPE of the
        # serialized snapshot, not the operator's specific config.
        from syncade.config import SyncadeConfig

        return SyncadeConfig()

    def test_persist_run_init_writes_documented_schema(self, tmp_path: Path):
        """Every top-level field documented in the brief's schema
        appears with the expected type. Acts as the schema-shape pin
        — if a future PR renames a field, this test flags it."""
        from syncade.persistence import persist_run_init

        run_dir = self._make_run_dir(tmp_path)
        _BASE_OID = "a" * 40
        path = persist_run_init(
            run_dir,
            syncade_version="0.1.0",
            started_at=_FIXED_STARTED_AT,
            pr_doc_path=Path("path/to/pr.md"),
            base_ref="1dbbca3",
            base_oid=_BASE_OID,
            starting_sha="3442e72ab60b43153cdbee83f3187203b289f14a",
            operator_branch="main",
            max_rounds=3,
            config=self._config(),
        )
        assert path == run_dir / "run-init.json"
        record = json.loads(path.read_text())
        # All top-level keys present.
        expected_keys = {
            "syncade_version",
            "started_at_utc",
            "pr_doc_path",
            "base_ref",
            "base_oid",
            "starting_sha",
            "operator_branch",
            "max_rounds",
            "config_snapshot",
        }
        assert set(record.keys()) == expected_keys
        # Per-field type + value pins.
        assert record["syncade_version"] == "0.1.0"
        assert record["started_at_utc"] == "2026-05-12T15:30:04Z"
        assert record["pr_doc_path"] == "path/to/pr.md"
        assert record["base_ref"] == "1dbbca3"
        assert record["base_oid"] == _BASE_OID
        assert record["starting_sha"] == "3442e72ab60b43153cdbee83f3187203b289f14a"
        assert record["operator_branch"] == "main"
        assert record["max_rounds"] == 3
        assert isinstance(record["config_snapshot"], dict)

    def test_persist_run_init_records_pr_doc_path_as_string(self, tmp_path: Path):
        """PR doc path is a Path() but must serialize as a plain str
        (JSON has no Path type). Pins the str() cast in the writer
        so a future refactor that drops the cast doesn't write an
        unparseable record."""
        from syncade.persistence import persist_run_init

        run_dir = self._make_run_dir(tmp_path)
        # Use an absolute Path; the writer must still serialize as str.
        pr_doc_path = tmp_path / "docs" / "prs" / "pr-16-resume.md"
        pr_doc_path.parent.mkdir(parents=True)
        pr_doc_path.write_text("# stub", encoding="utf-8")
        path = persist_run_init(
            run_dir,
            syncade_version="0.1.0",
            started_at=_FIXED_STARTED_AT,
            pr_doc_path=pr_doc_path,
            base_ref=None,
            base_oid=None,
            starting_sha="a" * 40,
            operator_branch=None,
            max_rounds=1,
            config=self._config(),
        )
        record = json.loads(path.read_text())
        assert isinstance(record["pr_doc_path"], str)
        assert record["pr_doc_path"] == str(pr_doc_path)
        # base_ref=None, base_oid=None, and operator_branch=None must serialize
        # as JSON null, not omitted.
        assert record["base_ref"] is None
        assert record["base_oid"] is None
        assert record["operator_branch"] is None

    def test_persist_run_init_includes_config_snapshot(self, tmp_path: Path):
        """The config snapshot must include the major sub-blocks the
        brief calls out (loop / reviewers / producer / review).
        Pydantic's model_dump produces JSON-safe
        primitives — enums flatten to their string values."""
        from syncade.persistence import persist_run_init

        run_dir = self._make_run_dir(tmp_path)
        path = persist_run_init(
            run_dir,
            syncade_version="0.1.0",
            started_at=_FIXED_STARTED_AT,
            pr_doc_path=Path("path/to/pr.md"),
            base_ref=None,
            base_oid=None,
            starting_sha="b" * 40,
            operator_branch="main",
            max_rounds=3,
            config=self._config(),
        )
        record = json.loads(path.read_text())
        cfg = record["config_snapshot"]
        # The sub-blocks the brief enumerates.
        assert "loop" in cfg
        assert "reviewers" in cfg
        assert "producer" in cfg
        # SyncadeConfig also serializes review.
        assert "review" in cfg
        # max_rounds is recoverable from the nested loop block AND
        # from the top-level max_rounds — they must agree at write
        # time even though the orchestrator only reads the top-level
        # version.
        assert cfg["loop"]["max_rounds"] == record["max_rounds"]
        # Round-trip: re-loading the dumped record back into
        # SyncadeConfig must succeed (proves model_dump produced a
        # valid JSON-safe shape).
        from syncade.config import SyncadeConfig

        rehydrated = SyncadeConfig(**cfg)
        assert rehydrated.loop.max_rounds == 3

    def test_persist_run_init_omits_empty_checks(self, tmp_path: Path):
        """PR-22 blocker 2: with no [[checks]] configured, config_snapshot must
        NOT serialize a ``checks`` key — restoring zero-config byte-identity to
        the pre-PR-21 baseline (the producer's doc-only 30e9a6e is superseded by
        this real byte fix)."""
        from syncade.persistence import persist_run_init

        run_dir = self._make_run_dir(tmp_path)
        path = persist_run_init(
            run_dir,
            syncade_version="0.1.0",
            started_at=_FIXED_STARTED_AT,
            pr_doc_path=Path("path/to/pr.md"),
            base_ref=None,
            base_oid=None,
            starting_sha="d" * 40,
            operator_branch="main",
            max_rounds=1,
            config=self._config(),  # SyncadeConfig() → checks defaults to []
        )
        cfg = json.loads(path.read_text())["config_snapshot"]
        assert "checks" not in cfg
        # Round-trip still works (omitted checks defaults back to []).
        from syncade.config import SyncadeConfig

        assert SyncadeConfig(**cfg).checks == []

    def test_persist_run_init_includes_configured_checks(self, tmp_path: Path):
        """A configured [[checks]] list is still echoed in config_snapshot for
        diagnostics — only the empty default is omitted."""
        from syncade.config import SyncadeConfig
        from syncade.persistence import persist_run_init

        run_dir = self._make_run_dir(tmp_path)
        config = SyncadeConfig(
            checks=[{"name": "loc", "command": "scripts/check.sh", "severity": "advisory"}]
        )
        path = persist_run_init(
            run_dir,
            syncade_version="0.1.0",
            started_at=_FIXED_STARTED_AT,
            pr_doc_path=Path("path/to/pr.md"),
            base_ref=None,
            base_oid=None,
            starting_sha="e" * 40,
            operator_branch="main",
            max_rounds=1,
            config=config,
        )
        cfg = json.loads(path.read_text())["config_snapshot"]
        assert [c["name"] for c in cfg["checks"]] == ["loc"]

    def test_persist_run_init_refuses_missing_run_dir(self, tmp_path: Path):
        """Defensive: run_dir must already exist (run_review's
        ``mkdir`` happens before this call). A missing dir is a
        caller bug and surfaces as FileNotFoundError, NOT a silently
        created file."""
        from syncade.persistence import persist_run_init

        with pytest.raises(FileNotFoundError, match="run_dir does not exist"):
            persist_run_init(
                tmp_path / "does-not-exist",
                syncade_version="0.1.0",
                started_at=_FIXED_STARTED_AT,
                pr_doc_path=Path("path/to/pr.md"),
                base_ref=None,
                base_oid=None,
                starting_sha="c" * 40,
                operator_branch="main",
                max_rounds=1,
                config=self._config(),
            )
