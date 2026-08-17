"""Plain helper functions + fake-adapter subclasses for the orchestrator
test subdir.

These are *called* (not injected like fixtures), so each split test file
imports the ones it needs:
``from tests.orchestrator._helpers import _ship, _two_reviewer_config``.
The leading underscore keeps pytest from collecting this as a test module.

Moved verbatim from the former ``tests/test_orchestrator.py``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from syncade.adapters.base import Invocation, ReviewerAdapter
from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.config import SyncadeConfig
from syncade.findings import Finding, ReviewerOutput
from syncade.synthesis import ConsolidatedFinding, FindingProvenance, SynthesizerOutput


def _fake_origin_head(repo: Path, branch: str) -> None:
    """Give ``repo`` an AUTHORITATIVE ``origin/HEAD -> <branch>`` without a real remote:
    create ``refs/remotes/origin/<branch>`` at that branch's tip, then point
    ``refs/remotes/origin/HEAD`` at it. Real repos have this; faking it locally (no bare
    remote, no push) satisfies the default-branch guard cheaply."""
    sha = subprocess.run(
        ["git", "rev-parse", branch], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/remotes/origin/{branch}", sha], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"],
        cwd=repo,
        check=True,
    )


def _init_git_repo(repo: Path, files: dict[str, str] | None = None) -> str:
    """Initialize ``repo`` as a git working tree with one commit.

    Returns the resulting commit SHA so tests that need to checkout a
    specific commit (worktree-cleanup probes, base-ref tests) can use
    it without an extra rev-parse call.
    """
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    if files is None:
        files = {"README.md": "syncade\n"}
    for name, content in files.items():
        (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    # A real syncade loop runs on a feature branch of a repo that HAS a remote. Mirror that:
    # an authoritative origin/HEAD -> main (faked locally, no push) plus HEAD on `work`, so
    # the PR-v2-26 default-branch guard is satisfied (a remote-less repo is refused, and a
    # loop ON the default branch is refused — both are set up explicitly by guard tests).
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)
    _fake_origin_head(repo, "main")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _current_branch_ref(repo: Path) -> str:
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return f"refs/heads/{branch}"


def _ship() -> ReviewerOutput:
    return ReviewerOutput(
        verdict="SHIP",
        findings=[],
        summary="orchestrator test SHIP",
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _no_ship() -> ReviewerOutput:
    return ReviewerOutput(
        verdict="NO-SHIP",
        findings=[
            Finding(
                severity="blocker",
                file="src/x.py",
                spec_clause="G1",
                finding="missing thing",
            )
        ],
        summary="orchestrator test NO-SHIP with one blocker",
        priority_order=[0],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


_DEFAULT_REVIEWER_NAME = object()


def _synth_with_blocker(
    reviewer_name=_DEFAULT_REVIEWER_NAME, original_index: int = 0
) -> SynthesizerOutput:
    """Canned :class:`SynthesizerOutput` carrying one active blocker.

    Use with ``FakeSynthesizerAdapter(canned_output=_synth_with_blocker())``
    to drive the mechanical-verdict path to ``FINDINGS_PRESENT``
    (exit 30) without spawning the real codex.

    The PR-7 brief moved verdict computation from reviewer
    ``verdict`` strings to the synthesizer's consolidated findings.
    Tests that previously asserted "two NO-SHIP reviewers → exit 30"
    now need to drive the synthesizer's output explicitly to make
    that intent expressible.

    QA fix P0.2: the orchestrator now validates synth provenance
    against the actual reviewer set (reviewer_name must match a real
    reviewer; original_index must be in range of that reviewer's
    findings).

    Finding R (cannot-OMIT pass-through): the synth now also rejects an
    output that DROPS a reviewer-surfaced blocker — every reviewer
    blocker must be referenced by some consolidated finding's
    provenance. The common orchestrator fixture dispatches TWO
    blocking reviewers (``rv1`` + ``rv2``, each a ``_no_ship()`` with one
    blocker at index 0), so the realistic default — what real codex
    does when N reviewers flag the same blocker — is ONE consolidated
    finding that DEDUPS both into a single finding with one provenance
    entry per reviewer. Calling ``_synth_with_blocker()`` with no args
    therefore references BOTH ``rv1#0`` AND ``rv2#0`` so pass-through is
    satisfied for the 2-reviewer-both-blocker case.

    The explicit single-reviewer form is preserved for the tests where
    only ONE reviewer has a blocker (the other SHIPs with 0 findings):
    ``_synth_with_blocker(reviewer_name="rv2")`` references ``rv2`` only.
    Callers passing an explicit ``reviewer_name`` must arrange for that
    reviewer's adapter to return enough findings that ``original_index``
    is valid; ``_no_ship()`` returns 1 finding (index 0).
    """
    if reviewer_name is _DEFAULT_REVIEWER_NAME:
        # Default: the 2-reviewer-both-blocker case — dedup rv1+rv2 into
        # one consolidated finding with a provenance entry per reviewer.
        provenance = [
            FindingProvenance(
                reviewer_name=name,
                original_severity="blocker",
                original_index=original_index,
                original_description="missing thing",
            )
            for name in ("rv1", "rv2")
        ]
    else:
        provenance = [
            FindingProvenance(
                reviewer_name=reviewer_name,
                original_severity="blocker",
                original_index=original_index,
                original_description="missing thing",
            )
        ]
    return SynthesizerOutput(
        consolidated_findings=[
            ConsolidatedFinding(
                description="orchestrator test: synthesized blocker",
                file="src/x.py",
                severity="blocker",
                provenance=provenance,
                dismissed=False,
            )
        ],
        synthesis_summary="orchestrator test: surfaced one blocker for exit-30 path",
    )


def _no_ship_n(n: int) -> ReviewerOutput:
    """A NO-SHIP reviewer output with ``n`` blocker findings (indices 0..n-1).

    PR-24: multi-blocker escalation tests need a synth with N consolidated
    blockers; each blocker's provenance references this reviewer at a distinct
    ``original_index``, so the reviewer must produce ``n`` findings for the
    orchestrator's provenance-against-reviewers validation to pass.
    """
    return ReviewerOutput(
        verdict="NO-SHIP",
        findings=[
            Finding(
                severity="blocker",
                file="src/x.py",
                spec_clause=f"G{i}",
                finding=f"missing thing {i}",
            )
            for i in range(n)
        ],
        summary=f"orchestrator test NO-SHIP with {n} blockers",
        priority_order=list(range(n)),
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _synth_with_n_blockers(n: int) -> SynthesizerOutput:
    """Canned synth output with ``n`` active blockers (indices 0..n-1), all
    attributed to reviewer ``rv1`` (pair with ``_no_ship_n(n)`` as rv1's
    adapter so the provenance original_index values are in range)."""
    return SynthesizerOutput(
        consolidated_findings=[
            ConsolidatedFinding(
                description=f"orchestrator test: synthesized blocker {i}",
                file="src/x.py",
                severity="blocker",
                provenance=[
                    FindingProvenance(
                        reviewer_name="rv1",
                        original_severity="blocker",
                        original_index=i,
                        original_description=f"missing thing {i}",
                    )
                ],
                dismissed=False,
            )
            for i in range(n)
        ],
        synthesis_summary=f"orchestrator test: surfaced {n} blockers",
    )


def _two_reviewer_config() -> SyncadeConfig:
    """The shape every single-pass test uses: two reviewers, distinct
    provider strings so the fake-factory routes them separately.

    PR-8: this helper pins ``max_rounds=1`` — the single-pass back-
    compat path. Every test using this helper expects PR-7.5's
    exact behavior (one round of reviewers → synth → optional test
    → exit; no producer subprocess provisioned). New PR-8
    multi-round tests construct their own SyncadeConfig with
    explicit ``max_rounds=2`` or ``3``.

    Without ``max_rounds=1``, the default (5) would make the
    pre-PR-8 tests hit the producer phase on NO-SHIP rounds —
    which (a) the tests don't expect, and (b) would fail to
    spawn ``claude`` / ``codex`` in the test environment unless
    a producer_adapter was also injected. The brief is explicit
    that ``max_rounds=1`` is the single-pass back-compat escape
    hatch; this helper encodes it.
    """
    return SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_rounds": 1},
    )


def _factory_returning(*adapters: ReviewerAdapter) -> Callable[[str], ReviewerAdapter]:
    """Same factory shape as the dispatcher tests use — consumes
    adapters in order, lock-guarded for thread safety even though the
    dispatcher currently does adapter lookup serially."""
    import threading

    iterator = iter(adapters)
    lock = threading.Lock()

    def factory(_provider: str) -> ReviewerAdapter:
        with lock:
            try:
                return next(iterator)
            except StopIteration as exc:
                raise RuntimeError("factory exhausted") from exc

    return factory


class _SlowFakeAdapter(FakeAdapter):
    """FakeAdapter whose Invocation echoes a known marker to stdout AND
    stderr and *then* hangs, so run_review's timeout path can be
    exercised end-to-end — including verifying the partial output
    produced before the SIGKILL actually reaches disk, not merely that
    the files exist. No real reviewer CLI involved."""

    STDOUT_MARKER = "partial-stdout-before-timeout"
    STDERR_MARKER = "partial-stderr-before-timeout"

    def build_invocation(self, reviewer_config, worktree_path: Path, prompt: str) -> Invocation:
        return Invocation(
            argv=[
                "sh",
                "-c",
                f"echo {self.STDOUT_MARKER}; echo {self.STDERR_MARKER} 1>&2; sleep 30",
            ],
            cwd=worktree_path,
            env=dict(os.environ),
            stdin_text=None,
            timeout_seconds=None,
        )


class _RegressionParserFakeAdapter(FakeAdapter):
    """FakeAdapter whose ``parse_output`` runs the REAL
    ``parse_reviewer_output`` against a fixed result text. Used by
    PR-5.6's end-to-end regression test to verify that the parser fix
    flows through the dispatcher + persistence layers without the
    orchestrator getting confused or losing data on the way."""

    def __init__(self, result_text: str) -> None:
        super().__init__()
        self._result_text = result_text

    def parse_output(self, result):
        from syncade.findings import parse_reviewer_output

        return parse_reviewer_output(self._result_text)


class _RoundCyclingSynth:
    """Test helper: synth adapter that returns a different canned
    output per call (per round).

    The orchestrator's loop calls ``adapter.build_invocation`` +
    ``adapter.extract_final_text`` ONCE per round. By
    constructing a fresh inner :class:`FakeSynthesizerAdapter` per
    call and rotating through a list of canned outputs, we can
    drive a multi-round loop where round 0 surfaces a blocker (so
    the loop continues) and round 1 ships (so the loop terminates).

    Mirrors the synthesizer adapter surface: the two methods
    :func:`syncade.synthesizer.run_synthesizer` calls on its adapter.
    Used only by TestMultiRoundLoop; production code never sees this.
    """

    name = "openai"

    def __init__(self, *canned_outputs):
        self._outputs = list(canned_outputs)
        self._build_idx = 0
        self._extract_idx = 0

    def build_invocation(self, reviewer_config, worktree_path, prompt):
        adapter = FakeSynthesizerAdapter(canned_output=self._outputs[self._build_idx])
        self._build_idx += 1
        return adapter.build_invocation(reviewer_config, worktree_path, prompt)

    def extract_final_text(self, result, *, empty_output_exception_class):
        adapter = FakeSynthesizerAdapter(canned_output=self._outputs[self._extract_idx])
        self._extract_idx += 1
        return adapter.extract_final_text(
            result,
            empty_output_exception_class=empty_output_exception_class,
        )


def _synth_clean() -> SynthesizerOutput:
    """Canned clean synth output (no blockers → SHIP)."""
    return SynthesizerOutput(
        consolidated_findings=[],
        synthesis_summary="multi-round loop test: clean",
    )
