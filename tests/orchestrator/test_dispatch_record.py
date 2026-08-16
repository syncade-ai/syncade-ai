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
import sys
import threading
import time
from dataclasses import replace

import pytest

from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter
from syncade.config import SyncadeConfig
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _synth_clean,
    _synth_with_blocker,
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


def test_reviewer_output_reaches_disk_BEFORE_the_panel_returns(repo_with_pr_doc):
    """PR-h-field-03 item 1, end-to-end: streaming is actually wired to the reviewer leg.

    `tests/test_streamed_capture.py` proves the choke point can stream. This proves the
    ORCHESTRATOR asks it to — the gap that would leave the whole change inert in production
    while every unit test stayed green.

    The timing IS the assertion. rv1 runs a real child that emits and then blocks; rv2 reads
    rv1's `.stdout` from inside its own invocation, so the panel provably has not returned and
    `persist_reviewer_result` provably has not run. Anything readable at that instant was put
    there by the OS as the child produced it.
    """
    repo, pr_doc = repo_with_pr_doc
    seen: dict[str, str] = {}

    class _EmitsThenBlocks(FakeAdapter):
        def build_invocation(self, config, worktree, prompt):
            base = super().build_invocation(config, worktree, prompt)
            return replace(
                base,
                argv=[
                    sys.executable,
                    "-c",
                    "import sys,time; sys.stdout.write('STREAMED-MID-FLIGHT');"
                    " sys.stdout.flush(); time.sleep(3)",
                ],
            )

    class _ReadsTheOther(FakeAdapter):
        def build_invocation(self, config, worktree, prompt):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                hits = sorted((repo / ".syncade" / "runs").glob("*/round-0/rv1.stdout"))
                if hits and hits[0].read_bytes():
                    seen["text"] = hits[0].read_text(encoding="utf-8")
                    break
                time.sleep(0.05)
            return super().build_invocation(config, worktree, prompt)

    _run(
        repo,
        pr_doc,
        _EmitsThenBlocks(canned_output=_no_ship()),
        _ReadsTheOther(canned_output=_no_ship()),
    )
    assert seen.get("text") == "STREAMED-MID-FLIGHT", (
        f"rv1's output was not on disk while rv1 was still running (saw {seen!r}) — the "
        f"orchestrator is not passing capture_dir, so a mid-panel death still loses everything"
    )


def test_persistence_does_not_contradict_what_was_streamed(repo_with_pr_doc):
    """Two writers now target `<name>.{stdout,stderr}`: the OS during the run, and
    `persist_reviewer_result` after it. If they ever disagreed, the durable artifact would be
    a different account of the run from the one that survives a crash — and no test would say
    which is real. Asserted for both streams of both reviewers against the captured result.
    """
    repo, pr_doc = repo_with_pr_doc

    class _Emits(FakeAdapter):
        def build_invocation(self, config, worktree, prompt):
            base = super().build_invocation(config, worktree, prompt)
            return replace(
                base,
                argv=[
                    sys.executable,
                    "-c",
                    f"import sys; sys.stdout.write('OUT-{config.name}');"
                    f" sys.stderr.write('ERR-{config.name}')",
                ],
            )

    result = _run(repo, pr_doc, _Emits(canned_output=_no_ship()), _Emits(canned_output=_no_ship()))
    round_dir = result.artifacts.round_dir
    for run_result in result.dispatch_result.results:
        name = run_result.reviewer_name
        captured = run_result.raw_subprocess_result
        assert captured is not None
        assert (round_dir / f"{name}.stdout").read_text(encoding="utf-8") == captured.stdout
        assert (round_dir / f"{name}.stderr").read_text(encoding="utf-8") == captured.stderr


def test_each_reviewer_records_the_pid_of_a_child_that_really_existed(repo_with_pr_doc):
    """PR-h-field-03 item 2. The pid is the half that answers the other open question.

    Whether the children OUTLIVED the parent separates "something killed syncade and orphaned
    them" from "they died and it followed" — and a post-mortem can only ask that of a real pid.
    So this asserts the recorded number against the pid the OS actually assigned, captured by
    the child reporting its own `os.getpid()` on stdout. A number that merely looks plausible
    would pass a weaker test and be useless in the only situation it exists for.
    """
    repo, pr_doc = repo_with_pr_doc

    class _ReportsOwnPid(FakeAdapter):
        def build_invocation(self, config, worktree, prompt):
            base = super().build_invocation(config, worktree, prompt)
            return replace(
                base,
                argv=[sys.executable, "-c", "import os,sys; sys.stdout.write(str(os.getpid()))"],
            )

    result = _run(
        repo,
        pr_doc,
        _ReportsOwnPid(canned_output=_no_ship()),
        _ReportsOwnPid(canned_output=_no_ship()),
    )
    round_dir = result.artifacts.round_dir
    record = json.loads((round_dir / "dispatch.json").read_text(encoding="utf-8"))
    recorded = {entry["name"]: entry.get("pid") for entry in record["reviewers"]}

    for run_result in result.dispatch_result.results:
        name = run_result.reviewer_name
        actual = int(run_result.raw_subprocess_result.stdout)
        assert recorded[name] == actual, (
            f"{name}: dispatch.json says pid {recorded[name]}, the child reported {actual}"
        )
    assert recorded["rv1"] != recorded["rv2"], "both reviewers recorded the same pid"
    assert record["parent_pid"] not in recorded.values(), "recorded the parent's pid, not a child's"


def test_a_broken_dispatch_record_never_fails_the_review(repo_with_pr_doc):
    """A breadcrumb must not be able to end a run that is otherwise about to succeed.

    dispatch.json is clobbered with garbage before the panel starts, so every pid write hits
    unparseable JSON. The round must still complete normally.
    """
    repo, pr_doc = repo_with_pr_doc

    class _CorruptsTheRecord(FakeAdapter):
        def build_invocation(self, config, worktree, prompt):
            for hit in (repo / ".syncade" / "runs").glob("*/round-0/dispatch.json"):
                hit.write_text("{not json at all", encoding="utf-8")
            return super().build_invocation(config, worktree, prompt)

    corrupted = _run(
        repo,
        pr_doc,
        _CorruptsTheRecord(canned_output=_no_ship()),
        FakeAdapter(canned_output=_no_ship()),
    )
    control = _run(
        repo, pr_doc, FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
    )
    # Compared against a CONTROL rather than a hardcoded exit 0: these fixtures reach a verdict
    # of their own, and the claim under test is that corrupting the breadcrumb changes nothing
    # about it. Asserting 0 would have tested the fixture, not the code.
    assert corrupted.exit_code == control.exit_code
    assert corrupted.termination_reason == control.termination_reason
    assert all(r.output is not None for r in corrupted.dispatch_result.results), (
        "a corrupt breadcrumb file broke the reviewers themselves"
    )


def test_concurrent_pid_writes_do_not_lose_each_other(tmp_path):
    """Reviewers spawn in parallel threads and every pid write is a read-modify-write.

    Not a theoretical race: with the lock removed, 39 of 40 concurrent writes are lost, because
    each thread reads the file before the others have written and the last writer wins. A
    barrier makes the overlap deterministic rather than hoping for an unlucky schedule.
    """
    from syncade.persistence import record_child_pid

    count = 40
    (tmp_path / "dispatch.json").write_text(
        json.dumps({"round": 0, "reviewers": [{"name": f"rv{i}"} for i in range(count)]}),
        encoding="utf-8",
    )
    barrier = threading.Barrier(count)

    def write(index: int) -> None:
        barrier.wait()
        record_child_pid(tmp_path, f"rv{index}", 1000 + index)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    record = json.loads((tmp_path / "dispatch.json").read_text(encoding="utf-8"))
    recorded = {entry["name"]: entry.get("pid") for entry in record["reviewers"]}
    assert recorded == {f"rv{i}": 1000 + i for i in range(count)}


def test_the_orchestrator_streams_the_PRODUCER_too(repo_with_pr_doc, monkeypatch):
    """The producer leg was left on pipes when streaming landed for the reviewers.

    That gap has a measured cost: a real producer hit its 2400s timeout and left
    `producer.stdout` at ZERO bytes — forty minutes of model work with no record. Streaming it
    is one argument, but only if the ORCHESTRATOR actually passes it, which the producer's own
    unit tests cannot show (they call run_producer directly). This asserts the wiring.
    """
    import syncade.orchestrator.producer_phase as pp_module

    seen: dict[str, object] = {}
    real_run_producer = pp_module.run_producer

    def spy(**kwargs):
        seen["capture_dir"] = kwargs.get("capture_dir")
        return real_run_producer(**kwargs)

    monkeypatch.setattr(pp_module, "run_producer", spy)

    repo, pr_doc = repo_with_pr_doc
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=SyncadeConfig(reviewers=_REVIEWERS, loop={"max_rounds": 2}),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()), FakeAdapter(canned_output=_no_ship())
        ),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
        producer_adapter=FakeProducerAdapter(commit_message="fix: round-0"),
    )
    assert "capture_dir" in seen, "the producer never ran, so the wiring is unproven"
    assert seen["capture_dir"] == result.artifacts.run_dir / "round-0", (
        f"producer capture_dir was {seen['capture_dir']!r}; a producer timeout would leave "
        f"producer.stdout empty again"
    )
