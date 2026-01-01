from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutput
from syncade.persistence import persist_producer_result, persist_round_manifest
from syncade.process import SubprocessResult
from syncade.snapshot import Snapshot
from tests.persistence._helpers import _FIXED_STARTED_AT, _make_round_dir


def _snapshot() -> Snapshot:
    return Snapshot(
        repo_root=Path("/tmp/x"),
        commit_sha="a" * 40,
        branch="main",
        base_ref=None,
        diff_text="",
        dirty_state="clean",
    )


def _dispatch() -> DispatchResult:
    return DispatchResult(
        results=[
            ReviewerRunResult(
                reviewer_name="rv",
                provider="anthropic",
                output=ReviewerOutput(
                    verdict="SHIP",
                    findings=[],
                    summary="ok",
                    priority_order=[],
                    coverage_gaps=[],
                    dismissed_concerns=[],
                ),
                error=None,
                duration_seconds=1.0,
                raw_subprocess_result=SubprocessResult(
                    returncode=0,
                    stdout="{}",
                    stderr="",
                    duration_seconds=1.0,
                ),
            )
        ],
        total_duration_seconds=1.0,
    )


def _committed_producer_result():
    from syncade.adapters.producer import ProducerOutput
    from syncade.producer import ProducerResult

    return ProducerResult(
        outcome="committed",
        starting_sha="a" * 40,
        ending_sha="b" * 40,
        duration_seconds=2.0,
        output=ProducerOutput(narrative_text="fixed"),
        error=None,
        raw_subprocess_result=SubprocessResult(
            returncode=0,
            stdout="raw",
            stderr="",
            duration_seconds=2.0,
        ),
    )


def test_round_manifest_json_write_uses_tmp_then_replace(tmp_path, monkeypatch):
    import syncade.persistence._atomic as atomic

    round_dir = _make_round_dir(tmp_path)
    manifest_path = round_dir / "manifest.json"
    manifest_path.write_text('{"old": true}\n', encoding="utf-8")
    calls: list[tuple[Path, Path, str]] = []

    def fail_replace(src, dst):
        src_path = Path(src)
        dst_path = Path(dst)
        calls.append((src_path, dst_path, src_path.read_text(encoding="utf-8")))
        raise RuntimeError("stop before replace")

    monkeypatch.setattr(atomic.os, "replace", fail_replace)

    with pytest.raises(RuntimeError, match="stop before replace"):
        persist_round_manifest(
            round_dir,
            _snapshot(),
            _dispatch(),
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
        )

    assert manifest_path.read_text(encoding="utf-8") == '{"old": true}\n'
    assert calls
    tmp_src, dst, tmp_text = calls[0]
    assert dst == manifest_path
    assert tmp_src != manifest_path
    assert tmp_src.parent == manifest_path.parent
    assert json.loads(tmp_text)["round_exit_code"] == 0


def test_producer_stdout_write_uses_tmp_then_replace(tmp_path, monkeypatch):
    import syncade.persistence._atomic as atomic

    round_dir = _make_round_dir(tmp_path)
    stdout_path = round_dir / "producer.stdout"
    stdout_path.write_text("old stdout", encoding="utf-8")
    calls: list[tuple[Path, Path, str]] = []

    def fail_replace(src, dst):
        src_path = Path(src)
        dst_path = Path(dst)
        calls.append((src_path, dst_path, src_path.read_text(encoding="utf-8")))
        raise RuntimeError("stop before replace")

    monkeypatch.setattr(atomic.os, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="stop before replace"):
        persist_producer_result(round_dir, _committed_producer_result())

    assert stdout_path.read_text(encoding="utf-8") == "old stdout"
    assert calls
    tmp_src, dst, tmp_text = calls[0]
    assert dst == stdout_path
    assert tmp_src != stdout_path
    assert tmp_src.parent == stdout_path.parent
    assert tmp_text == "fixed"
