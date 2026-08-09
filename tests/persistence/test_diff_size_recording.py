"""Round manifests record diff size, so a future cap has history to calibrate against.

There is no diff cap anywhere today: a snapshot's `diff_text` goes to reviewers whole, and a
20-commit range in this repo renders ~1.3M chars (~330k tokens) — more than most context
windows before the PR doc, the schema, or the reviewer's own exploration. The cap brief's own
rule is MEASURE BEFORE CAPPING, and the measurement half got split out with the cap, leaving
that PR with nothing to calibrate against.

Size cannot be backfilled — the diffs are not retained — so every run before this lands is a
datapoint permanently lost. That is why this is a two-line change shipped on its own rather
than bundled into the cap PR.

TWO numbers, because they answer different questions:

- `diff_bytes` — what the repo produced.
- `diff_bytes_reviewed` — what a reviewer was actually handed, AFTER repo-context stripping.

A cap must threshold on the second: that is what reaches the model's context. Recording only
the first would calibrate the threshold against bytes nobody sends.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from syncade.dispatcher import DispatchResult
from syncade.persistence import persist_round_manifest
from tests.persistence._helpers import _make_round_dir, _snapshot

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)

_DIFF = "diff --git a/a.py b/a.py\n+x = 1\n"


def _write(tmp_path, diff: str, **kw):
    """The writer NEVER derives a size — the measuring caller supplies it, as round.py does."""
    kw.setdefault("raw_diff_bytes", len(diff.encode("utf-8")))
    round_dir = _make_round_dir(tmp_path)
    persist_round_manifest(
        round_dir,
        _snapshot(base_ref="main", base_oid="b" * 40, diff_text=diff),
        DispatchResult(results=[], total_duration_seconds=0.0),
        0,
        datetime.now(UTC),
        **kw,
    )
    return json.loads((round_dir / "manifest.json").read_text())["snapshot"]


def test_raw_diff_size_is_recorded(tmp_path):
    snap = _write(tmp_path, _DIFF)
    assert snap["diff_bytes"] == len(_DIFF.encode("utf-8"))


def test_diff_size_counts_utf8_bytes_not_python_chars(tmp_path):
    """Non-ASCII content must count UTF-8 bytes, not Python character count.

    A 2-byte UTF-8 character (e.g. é = U+00E9) has len(...) == 1 in Python
    but encodes to 2 bytes. Recording characters would undercount and make the
    history useless for calibrating a future byte-limit cap.
    """
    non_ascii = "diff --git a/a.py b/a.py\n+# café\n"
    snap = _write(tmp_path, non_ascii)
    assert snap["diff_bytes"] == len(non_ascii.encode("utf-8"))
    assert snap["diff_bytes"] > len(non_ascii), (
        "non-ASCII diff must record more bytes than Python character count"
    )


def test_reviewed_size_is_recorded_separately_from_the_raw_size(tmp_path):
    """The number a cap would actually threshold on."""
    snap = _write(
        tmp_path,
        _DIFF + "diff --git a/CLAUDE.md b/CLAUDE.md\n+secret\n",
        filtered_diff_bytes=len(_DIFF),
    )
    assert snap["diff_bytes"] > snap["diff_bytes_reviewed"], (
        "the reviewed size must reflect repo-context stripping, not the raw diff"
    )
    assert snap["diff_bytes_reviewed"] == len(_DIFF)


def test_reviewed_size_is_null_when_the_caller_did_not_compute_it(tmp_path):
    """Absent, not zero — 0 would be indistinguishable from 'an empty filtered diff'."""
    assert _write(tmp_path, _DIFF)["diff_bytes_reviewed"] is None


def test_a_writer_that_is_not_told_a_size_records_null_not_zero(tmp_path):
    """The fix for the fabrication class: no size in, no size out.

    Deriving `diff_bytes` from `snapshot.diff_text` looked free and was not — a resumed
    round's snapshot carries a SENTINEL rather than the real diff, so the derivation invented
    a byte count for a diff nobody had. `null` is the honest answer.
    """
    round_dir = _make_round_dir(tmp_path)
    persist_round_manifest(
        round_dir,
        _snapshot(base_ref="main", base_oid="b" * 40, diff_text="whatever the snapshot holds"),
        DispatchResult(results=[], total_duration_seconds=0.0),
        0,
        datetime.now(UTC),
    )
    snap = json.loads((round_dir / "manifest.json").read_text())["snapshot"]
    assert snap["diff_bytes"] is None, "the writer invented a size it was never given"
    assert snap["diff_bytes_reviewed"] is None


def test_empty_diff_records_zero_not_null(tmp_path):
    """A measured empty diff is a real datapoint, not a missing one."""
    snap = _write(tmp_path, "")
    assert snap["diff_bytes"] == 0
    assert snap["diff_present"] is False


def test_the_orchestrator_actually_populates_both_sizes(tmp_path):
    """The WIRING, not just the writer.

    `filtered_diff_bytes` defaults to `None`, so a regression that stops passing it from
    `round.py` leaves the writer's unit tests green while every future run records nothing —
    which is precisely the history this change exists to accumulate. This drives a real
    `run_review` and asserts both numbers are present AND that stripping is reflected.
    """
    import subprocess

    from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
    from syncade.logging import Logger
    from syncade.orchestrator import run_review
    from tests.orchestrator._helpers import (
        _factory_returning,
        _ship,
        _synth_clean,
        _two_reviewer_config,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(argv, cwd=repo, capture_output=True, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True, check=True)

    # A reviewable change PLUS a repo-context file, so the two sizes must differ.
    (repo / "app.py").write_text("x = 1\n")
    (repo / "CLAUDE.md").write_text("stripped\n" * 40)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, capture_output=True, check=True)

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        base_ref="HEAD~1",
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
        logger=Logger(level="quiet"),
    )

    snap = json.loads((result.artifacts.run_dir / "round-0" / "manifest.json").read_text())[
        "snapshot"
    ]
    assert snap["diff_bytes"] > 0, "a real diff recorded as 0 bytes"
    assert snap["diff_bytes_reviewed"] is not None, (
        "round.py stopped passing filtered_diff_bytes — every run now records no reviewed size"
    )
    assert snap["diff_bytes_reviewed"] < snap["diff_bytes"], (
        "the stripped CLAUDE.md is not reflected in the reviewed size"
    )


def _make_repo(tmp_path, extra_files=None):
    """Minimal git repo on a 'work' branch with faked origin/HEAD."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(argv, cwd=repo, capture_output=True, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "checkout", "-q", "-b", "work"], cwd=repo, capture_output=True, check=True
    )
    sha = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", sha],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    if extra_files:
        for name, content in extra_files.items():
            (repo / name).write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "change"], cwd=repo, capture_output=True, check=True
        )
    return repo


def test_no_changes_path_records_zero_not_null(tmp_path):
    """When all diff sections are stripped, diff_bytes_reviewed must be 0, not null,
    and diff_bytes must reflect the raw (unstripped) byte count.

    null means 'not measured'; 0 means 'measured and empty'. A run where every
    changed file is a strip target IS a measurement — the reviewer was handed an
    empty diff — and must be recorded as 0. But the raw diff (before stripping) is
    non-empty, so diff_bytes must be a positive integer, not null.
    """
    from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
    from syncade.config import SyncadeConfig
    from syncade.logging import Logger
    from syncade.orchestrator import run_review
    from tests.orchestrator._helpers import _factory_returning, _ship, _synth_clean

    repo = _make_repo(tmp_path, extra_files={"CLAUDE.md": "context-only change\n"})
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 1},
        review={"strip_repo_context_files": ["CLAUDE.md"]},
    )
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        base_ref="HEAD~1",
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
        logger=Logger(level="quiet"),
    )

    snap = json.loads((result.artifacts.run_dir / "round-0" / "manifest.json").read_text())[
        "snapshot"
    ]
    assert snap["diff_bytes_reviewed"] == 0, (
        "all-stripped path must record 0 (measured empty), not null (not measured)"
    )
    assert result.rounds[0].filtered_diff_bytes == 0, (
        "no_changes_to_review RoundResult must carry filtered_diff_bytes=0, not None"
    )
    # The raw diff (CLAUDE.md change) is non-empty — diff_bytes must be > 0.
    assert snap["diff_bytes"] is not None and snap["diff_bytes"] > 0, (
        "all-stripped path must record the raw byte count, not null"
    )
    assert result.rounds[0].raw_diff_bytes is not None and result.rounds[0].raw_diff_bytes > 0, (
        "no_changes_to_review RoundResult must carry the raw byte count"
    )


def test_no_changes_true_empty_diff_records_zero_raw(tmp_path):
    """When base == HEAD (truly no changes), diff_bytes must be 0, not null.

    A true empty diff is a measurement of 0 bytes, distinguishable from
    'not measured' (null). Both diff_bytes and diff_bytes_reviewed must be 0.
    """
    import subprocess

    from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
    from syncade.config import SyncadeConfig
    from syncade.logging import Logger
    from syncade.orchestrator import run_review
    from tests.orchestrator._helpers import _factory_returning, _ship, _synth_clean

    # Create a repo with two commits, then pass base_ref pointing at HEAD (no diff)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repo, check=True)
    # Second commit so HEAD~1 exists but diff against HEAD is empty
    (repo / "app.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True)

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 1},
    )
    # base_ref == HEAD means the diff is empty
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        base_ref="HEAD",
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
        logger=Logger(level="quiet"),
    )

    snap = json.loads((result.artifacts.run_dir / "round-0" / "manifest.json").read_text())[
        "snapshot"
    ]
    assert snap["diff_bytes"] == 0, (
        "true-empty diff must record diff_bytes=0 (measured zero), not null"
    )
    assert snap["diff_bytes_reviewed"] == 0, (
        "true-empty diff must record diff_bytes_reviewed=0, not null"
    )


def test_producer_rewrite_preserves_diff_bytes_reviewed(tmp_path):
    """The producer-phase manifest rewrite must not erase diff_bytes_reviewed.

    Before the fix, loop_round_step.py re-called persist_round_manifest without
    filtered_diff_bytes, overwriting the field with null on any NO-SHIP round
    that ran a producer.
    """

    from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter
    from syncade.config import SyncadeConfig
    from syncade.logging import Logger
    from syncade.orchestrator import run_review
    from tests.orchestrator._helpers import (
        _factory_returning,
        _no_ship,
        _RoundCyclingSynth,
        _ship,
        _synth_clean,
        _synth_with_blocker,
    )

    repo = _make_repo(tmp_path, extra_files={"app.py": "x = 1\n", "CLAUDE.md": "ctx\n"})
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n")

    config = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 2},
        review={"strip_repo_context_files": ["CLAUDE.md"]},
    )
    # Round 0: NO-SHIP so the producer runs. Round 1: SHIP.
    producer = FakeProducerAdapter(commit_message="fix: producer commit")
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        base_ref="HEAD~1",
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
        producer_adapter=producer,
        logger=Logger(level="quiet"),
    )

    # Round 0 ran a producer; its manifest was rewritten. The field must survive.
    snap = json.loads((result.artifacts.run_dir / "round-0" / "manifest.json").read_text())[
        "snapshot"
    ]
    assert snap["diff_bytes_reviewed"] is not None, (
        "producer-phase manifest rewrite erased diff_bytes_reviewed"
    )
    assert snap["diff_bytes_reviewed"] < snap["diff_bytes"], (
        "stripped CLAUDE.md must not be counted in diff_bytes_reviewed"
    )


def test_a_resumed_round_records_null_rather_than_a_number_it_did_not_measure(tmp_path):
    """The two tests that stood here asserted the OPPOSITE, and they were removed with the
    code they covered.

    They required a budget-abort resume to carry `diff_bytes_reviewed` and `diff_bytes`
    forward from the persisted round. Doing that meant trusting arbitrary on-disk values —
    hence validating booleans and negatives — and on a force-drift resume it mixed the
    drifted current snapshot with the original round's counts. Two blind review rounds kept
    finding fresh ways for the carried number to be wrong; the numbers themselves only feed a
    future size cap, so a wrong one is worse than an absent one.

    Now nothing rehydrates them. `load_completed_round` returning `None` for both is asserted
    directly in `tests/resume/test_round_trip.py`; this asserts the PERSISTED consequence.
    """
    round_dir = _make_round_dir(tmp_path)
    persist_round_manifest(
        round_dir,
        _snapshot(base_ref="main", base_oid="b" * 40, diff_text=_DIFF),
        DispatchResult(results=[], total_duration_seconds=0.0),
        0,
        datetime.now(UTC),
        filtered_diff_bytes=None,
        raw_diff_bytes=None,
    )
    snap = json.loads((round_dir / "manifest.json").read_text())["snapshot"]
    assert snap["diff_bytes"] is None
    assert snap["diff_bytes_reviewed"] is None
