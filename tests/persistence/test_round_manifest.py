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

from syncade import __version__
from syncade.adapters.base import ReviewerInvocationError
from syncade.dispatcher import DispatchResult, ReviewerRunResult
from syncade.findings import ReviewerOutput
from syncade.persistence import (
    persist_findings_md,
    persist_reviewer_result,
    persist_round_manifest,
    persist_run_summary,
    persist_synthesizer_result,
)
from tests.persistence._helpers import (
    _FIXED_STARTED_AT,
    _make_round_dir,
    _no_ship_with_finding,
    _ship,
    _snapshot,
    _subprocess_result,
)


class TestPersistRoundManifest:
    def _dispatch_two_reviewers(
        self,
        outcomes: list[tuple[str, ReviewerOutput | None, Exception | None]],
    ) -> DispatchResult:
        """Helper: build a DispatchResult from a list of
        (name, output, error) tuples."""
        results = []
        for name, output, error in outcomes:
            results.append(
                ReviewerRunResult(
                    reviewer_name=name,
                    provider="anthropic" if "claude" in name else "openai",
                    output=output,
                    error=error,
                    duration_seconds=2.5,
                    raw_subprocess_result=_subprocess_result(),
                )
            )
        return DispatchResult(results=results, total_duration_seconds=2.5)

    def test_retried_folds_producer_retries(self, tmp_path: Path):
        """PR-v2-22 Issue 1: the round manifest's ``retried`` folds in the producer's retries
        (alongside reviewers + synth); it stays 0 (byte-identical happy path) when the producer
        didn't retry."""
        from syncade.adapters.producer import ProducerOutput
        from syncade.producer_result import ProducerResult

        dispatch = self._dispatch_two_reviewers([("claude-reviewer", _ship(), None)])  # 0 retries

        def _committed(retries: int):
            return ProducerResult(
                outcome="committed",
                starting_sha="a" * 40,
                ending_sha="b" * 40,
                duration_seconds=1.0,
                output=ProducerOutput(narrative_text="fix"),
                error=None,
                retries=retries,
            )

        p = persist_round_manifest(
            _make_round_dir(tmp_path),
            _snapshot(),
            dispatch,
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            producer_result=_committed(2),
        )
        assert json.loads(p.read_text())["retried"] == 2  # 0 reviewers + 0 synth + 2 producer

        # C5: a producer that didn't retry (default 0) leaves ``retried`` at 0.
        p0 = persist_round_manifest(
            _make_round_dir(tmp_path, run_id="2026-05-12T15-30-05"),
            _snapshot(),
            dispatch,
            exit_code=0,
            started_at=_FIXED_STARTED_AT,
            producer_result=_committed(0),
        )
        assert json.loads(p0.read_text())["retried"] == 0

    def test_manifest_is_valid_json_with_documented_schema(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        dispatch = self._dispatch_two_reviewers(
            [
                ("claude-reviewer", _ship(), None),
                ("codex-reviewer", _no_ship_with_finding(), None),
            ]
        )
        path = persist_round_manifest(
            round_dir, snap, dispatch, exit_code=30, started_at=_FIXED_STARTED_AT
        )
        assert path == round_dir / "manifest.json"

        manifest = json.loads(path.read_text())
        # Top-level keys from the brief's schema. PR-8 polish R1.T1:
        # the per-round exit_code key was renamed round_exit_code
        # for consistency with loop-manifest.json's rounds[]
        # entries.
        for key in (
            "syncade_version",
            "run_id",
            "round",
            "started_at_utc",
            "snapshot",
            "reviewers",
            "round_exit_code",
        ):
            assert key in manifest, f"manifest missing {key!r}"
        assert manifest["syncade_version"] == __version__
        assert manifest["run_id"] == "2026-05-12T15-30-04"
        # started_at_utc renders the run-start instant the orchestrator
        # passed in (deterministic here via _FIXED_STARTED_AT) — not a
        # write-time datetime.now().
        assert manifest["started_at_utc"] == "2026-05-12T15:30:04Z"
        assert manifest["round"] == 0
        # PR-8 polish R1.T1: per-round manifest key renamed
        # ``exit_code`` → ``round_exit_code`` for consistency with
        # ``loop-manifest.json``'s ``rounds[].round_exit_code``.
        assert manifest["round_exit_code"] == 30
        # Snapshot section
        snap_section = manifest["snapshot"]
        assert snap_section["commit_sha"] == "a" * 40
        assert snap_section["branch"] == "main"
        assert snap_section["base_ref"] is None
        assert snap_section["base_oid"] is None
        assert snap_section["diff_present"] is False
        # Reviewers section: one entry per ReviewerRunResult
        rs = manifest["reviewers"]
        assert len(rs) == 2
        assert rs[0]["name"] == "claude-reviewer"
        assert rs[0]["outcome"] == "success"
        assert rs[0]["verdict"] == "SHIP"
        assert rs[0]["finding_count"] == 0
        assert rs[0]["error_type"] is None
        assert rs[1]["verdict"] == "NO-SHIP"
        assert rs[1]["finding_count"] == 1

    def test_manifest_failure_entry_has_null_verdict_and_error_type(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot()
        dispatch = self._dispatch_two_reviewers(
            [
                ("claude-reviewer", _ship(), None),
                (
                    "codex-reviewer",
                    None,
                    ReviewerInvocationError(
                        "auth failed",
                        returncode=1,
                        stdout="",
                        stderr="",
                    ),
                ),
            ]
        )
        path = persist_round_manifest(
            round_dir, snap, dispatch, exit_code=40, started_at=_FIXED_STARTED_AT
        )
        manifest = json.loads(path.read_text())
        # success entry
        assert manifest["reviewers"][0]["outcome"] == "success"
        # failure entry
        failure = manifest["reviewers"][1]
        assert failure["outcome"] == "failure"
        assert failure["verdict"] is None
        assert failure["finding_count"] is None
        assert failure["error_type"] == "ReviewerInvocationError"

    def test_manifest_reflects_base_ref_and_diff_present(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot(base_ref="HEAD~1", diff_text="diff --git a/x b/x\n+x\n")
        dispatch = self._dispatch_two_reviewers([("claude-reviewer", _ship(), None)])
        path = persist_round_manifest(
            round_dir, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
        )
        manifest = json.loads(path.read_text())
        assert manifest["snapshot"]["base_ref"] == "HEAD~1"
        assert manifest["snapshot"]["diff_present"] is True

    def test_manifest_persists_base_oid_when_present(self, tmp_path: Path):
        """base_oid in the snapshot section pins the exact reviewed commit range
        even if the symbolic base_ref moves after the run."""
        round_dir = _make_round_dir(tmp_path)
        oid = "b" * 40
        snap = _snapshot(base_ref="main", base_oid=oid, diff_text="diff --git a/x b/x\n+x\n")
        dispatch = self._dispatch_two_reviewers([("claude-reviewer", _ship(), None)])
        path = persist_round_manifest(
            round_dir, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
        )
        manifest = json.loads(path.read_text())
        assert manifest["snapshot"]["base_oid"] == oid

    def test_manifest_records_detached_head_branch_null(self, tmp_path: Path):
        round_dir = _make_round_dir(tmp_path)
        snap = _snapshot(branch=None)
        dispatch = self._dispatch_two_reviewers([("rv", _ship(), None)])
        path = persist_round_manifest(
            round_dir, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
        )
        manifest = json.loads(path.read_text())
        assert manifest["snapshot"]["branch"] is None

    def test_manifest_round_dir_must_exist(self, tmp_path: Path):
        bogus = tmp_path / "missing"
        snap = _snapshot()
        dispatch = self._dispatch_two_reviewers([("rv", _ship(), None)])
        with pytest.raises(FileNotFoundError):
            persist_round_manifest(bogus, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT)


def test_public_surface_has_docstrings():
    import inspect

    assert inspect.getdoc(persist_reviewer_result)
    assert inspect.getdoc(persist_round_manifest)
    assert inspect.getdoc(persist_run_summary)
    assert inspect.getdoc(persist_synthesizer_result)
    assert inspect.getdoc(persist_findings_md)


def test_persist_round_manifest_docstring_matches_emitted_schema(tmp_path: Path):
    """Pin the ``persist_round_manifest`` docstring example's top-
    level keys against the actual emitted manifest's keys.

    PR-8.5 Task 1 — the first real-CLI dogfood found drift in this
    exact docstring (the example listed ``"exit_code"`` after PR-8
    R1.T1 had renamed the emitted key to ``"round_exit_code"``). The
    example was also missing the ``test_skip_reason`` and
    ``producer`` sections PR-8 added. This test pins the contract so
    a future schema change to the emitted dict fails here rather
    than as drift discovered by another dogfood.
    """
    import inspect

    docstring = inspect.getdoc(persist_round_manifest)
    assert docstring is not None

    # Locate the JSON code-block, then balance braces to extract the
    # outermost object. Regex can't balance, but the docstring has
    # exactly one ``.. code-block:: json`` so a linear scan is fine.
    directive = ".. code-block:: json"
    directive_idx = docstring.index(directive)
    brace_start = docstring.index("{", directive_idx)
    depth = 0
    brace_end = None
    for i in range(brace_start, len(docstring)):
        c = docstring[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                brace_end = i
                break
    assert brace_end is not None, "unbalanced braces in docstring example"
    example_json = docstring[brace_start : brace_end + 1]
    example_dict = json.loads(example_json)

    # Emit a real manifest. Sections left null (synth/test/producer)
    # still result in the keys being present in the top-level dict —
    # which is what we're pinning.
    round_dir = _make_round_dir(tmp_path)
    snap = _snapshot()
    dispatch = DispatchResult(
        results=[
            ReviewerRunResult(
                reviewer_name="claude-reviewer",
                provider="anthropic",
                output=_ship(),
                error=None,
                duration_seconds=1.0,
                raw_subprocess_result=_subprocess_result(),
            )
        ],
        total_duration_seconds=1.0,
    )
    path = persist_round_manifest(
        round_dir, snap, dispatch, exit_code=0, started_at=_FIXED_STARTED_AT
    )
    emitted = json.loads(path.read_text())

    assert set(example_dict.keys()) == set(emitted.keys()), (
        f"docstring example keys {sorted(example_dict.keys())!r} do not "
        f"match emitted manifest keys {sorted(emitted.keys())!r}"
    )
