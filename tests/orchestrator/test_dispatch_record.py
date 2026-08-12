"""A round that dies mid-dispatch still leaves evidence — PR-h-field-02 item 1.

Three field runs were killed in `round-2: reviewing` and left the round directory EMPTY: no
stdout, no stderr, no manifest. The post-mortem had a terminal log line and a `status.json`
phase, and nothing else — not the provider's error, not the child pids, not even proof that
dispatch had begun.

The cause is timing, not error handling. `persist_reviewer_result` writes `.stdout`/`.stderr`
for a FAILED reviewer perfectly well (asserted below, because the original bug report assumed
otherwise). But every artifact in a round is written AFTER the whole panel returns, so a process
that dies while reviewers are still running writes nothing at all.

`dispatch.json` closes that: one small file, written before the panel starts, recording what was
in flight and since when. It never changes a verdict — it exists so the next unexplained death
is diagnosable instead of inferred.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _synth_clean,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)

_REVIEWERS = [
    {"name": "rv1", "provider": "fake1", "model": "x"},
    {"name": "rv2", "provider": "fake2", "model": "y"},
]


class _Boom(RuntimeError):
    pass


def _run(repo, pr_doc, *adapters, synth=None):
    return run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 1}),
        adapter_factory=_factory_returning(*adapters),
        synthesizer_adapter=synth or _RoundCyclingSynth(_synth_clean()),
    )


def test_dispatch_record_exists_and_names_what_was_in_flight(repo_with_pr_doc):
    repo, pr_doc = repo_with_pr_doc
    result = _run(
        repo, pr_doc, FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
    )
    record = result.artifacts.round_dir / "dispatch.json"
    assert record.is_file(), "no dispatch.json — a mid-dispatch death would leave nothing"

    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["round"] == 0
    assert data["timeout_seconds"] > 0
    assert isinstance(data["parent_pid"], int) and data["parent_pid"] > 0
    assert [r["name"] for r in data["reviewers"]] == ["rv1", "rv2"], (
        "the record must name the reviewers actually dispatched"
    )
    assert data["dispatched_at_utc"].endswith("Z")


def test_the_record_is_written_BEFORE_the_panel_returns(repo_with_pr_doc):
    """The whole point. If it were written afterwards it would be worthless for a kill.

    Proven by having a reviewer observe the filesystem from inside its own invocation: at that
    moment the panel has not returned, so anything on disk was written before it.
    """
    repo, pr_doc = repo_with_pr_doc
    seen: dict[str, bool] = {}

    class _Observing(FakeAdapter):
        def build_invocation(self, *a, **kw):
            # Round dir is <run_dir>/round-0; find it from the repo we were handed.
            runs = sorted((repo / ".syncade" / "runs").glob("*/round-0/dispatch.json"))
            seen["present"] = bool(runs)
            return super().build_invocation(*a, **kw)

    _run(repo, pr_doc, _Observing(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship()))
    assert seen.get("present") is True, (
        "dispatch.json did not exist while a reviewer was still running — it is being written "
        "after the panel, which is exactly the timing that loses everything on a kill"
    )


def test_an_ordinary_reviewer_failure_still_writes_its_streams(repo_with_pr_doc):
    """Pins what the original bug report got wrong, so nobody 'fixes' a working path.

    The report said a failing reviewer produces an empty round directory. It does not: the
    failure path writes .stdout, .stderr and .error.txt. Only a death mid-dispatch loses them.
    """
    repo, pr_doc = repo_with_pr_doc
    result = _run(
        repo,
        pr_doc,
        FakeAdapter(canned_output=_no_ship()),
        FakeAdapter(canned_exception=_Boom("simulated reviewer failure")),
    )
    names = {p.name for p in result.artifacts.round_dir.iterdir()}
    assert {"rv2.stdout", "rv2.stderr", "rv2.error.txt"} <= names, (
        f"the failure path lost artifacts it used to write: {sorted(names)}"
    )
    assert "dispatch.json" in names
