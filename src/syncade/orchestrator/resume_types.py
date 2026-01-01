"""Shared types + constants for the resume layer.

Leaf module: the filename constants, the resumable-exit-code set, the git
timeout, and the three public types (:class:`ResumeError`, :class:`ResumePlan`,
:class:`TreeDriftError`). Imported by the ``resume_*`` siblings; re-exported by
the ``resume.py`` shim so ``syncade.orchestrator.resume.<name>`` import paths are
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from syncade.exit_codes import (
    BUDGET_EXCEEDED,
    CLARIFICATION_NEEDED,
    REVIEWER_FAILURE,
    REVIEWER_OUTPUT_UNPARSEABLE,
    WORKTREE_ERROR,
)

LOOP_MANIFEST_FILENAME: str = "loop-manifest.json"
"""The top-level loop-manifest filename. Lockstep with
:mod:`syncade.persistence.loop_manifest`."""

ROUND_MANIFEST_FILENAME: str = "manifest.json"
"""The per-round manifest filename. Lockstep with
:mod:`syncade.persistence.round_manifest`."""

# Final exit codes the loop can RESUME from. Reviewer/worktree/parse failures are
# environment/subprocess failures mid-loop (restart the errored round).
# 10 (CLARIFICATION_NEEDED): a producer escalation checkpoint — resume re-runs the
# escalated round with the operator's recorded decision.
# 25 (BUDGET_EXCEEDED, PR-v2-11): a deliberate cost-ceiling stop at a phase boundary.
# A before-producer stop's review bundle is persisted whole; plan_resume detects this
# case via the loop-manifest's termination_reason and sets budget_aborted_before_producer_round
# so the loop rehydrates the review bundle and dispatches only the producer on the fresh tally.
# Everything else (0, 20, 30) is a clean completion / NO-SHIP and the operator's next
# move is a fresh run.
_RESUMABLE_EXIT_CODES: frozenset[int] = frozenset(
    {
        CLARIFICATION_NEEDED,
        BUDGET_EXCEEDED,
        REVIEWER_FAILURE,
        WORKTREE_ERROR,
        REVIEWER_OUTPUT_UNPARSEABLE,
    }
)

# Wall-clock ceiling for the git rev-parse calls in check_tree_drift.
# Sub-second on every reasonable repo; 5s is generous and bounded.
_GIT_TIMEOUT_SECONDS: float = 5.0


class ResumeError(Exception):
    """Raised when a target run-id is not eligible to resume or its
    on-disk state is malformed.

    The CLI maps this to exit 60 (worktree/environment error class)
    on the failing-resume path. The message is operator-facing and
    should name the specific reason: not found, completed normally,
    malformed run-init, etc.
    """


@dataclass(frozen=True)
class ResumePlan:
    """The orchestrator's input contract for a resumed run.

    Built by :func:`plan_resume` from on-disk artifacts. Carries
    everything ``run_review`` needs to skip the original-run setup
    steps (run-id generation, run-init.json write) and pick up at
    ``resumed_round`` with the right snapshot SHA.

    Attributes:
        run_id: The original run's id. The orchestrator reuses
            this instead of generating a fresh one — that's the
            point of resume.
        run_dir: ``<repo>/.syncade/runs/<run_id>/``. Must exist.
        pr_doc_path: PR doc the original run was invoked against.
            Resumed reviewers see the CURRENT content of this file
            (no diff/refuse on brief edits — operator's
            responsibility).
        operator_branch: Branch the original run was started on.
            Read from ``run-init.json``. ``None`` for detached HEAD.
        expected_sha: The full object ID the resumed round should
            snapshot from. For round 0, the original run's
            ``starting_sha``; for round N>0, the prior round's
            advancement target (producer ending SHA, or snapshot
            SHA if the producer didn't commit).
        expected_branch: The branch the operator should still be on
            at resume time. Identical to ``operator_branch``;
            duplicated for symmetry with the tree-drift API.
        resumed_round: 0-indexed round the resumed loop will run
            FIRST. The orchestrator's for-loop enters at
            ``range(resumed_round, max_rounds)``.
        completed_rounds: 0-indexed list of rounds that completed
            cleanly. Used by ``run_review`` (T4) to rehydrate
            their ``RoundResult`` state via
            :func:`load_completed_round`. Always
            ``[0, 1, ..., resumed_round - 1]`` in v1 — the resume
            walk is contiguous.
        max_rounds: The original run's max_rounds. The CLI may
            override this via ``--resume --max-rounds N``; that
            override applies to the orchestrator's loop bound,
            not to this field (which remains the original value
            for diagnostic purposes).
        syncade_version: The syncade version the original run was
            launched with. Compared against the current
            :data:`syncade.__version__` to emit a stderr warning
            on mismatch (NOT a hard refusal — see brief's
            "Out of scope").
        config_snapshot_path: Path to the original ``run-init.json``.
            Used by T4 for the "config drift" diagnostic
            annotation on the new round's ``summary.md``.
        budget_aborted_before_producer_round: When not None, the run was
            budget-aborted (exit 25) before this round's producer ran.
            The round's review bundle is complete and in ``completed_rounds``
            for rehydration; the loop skips ``_run_one_round`` for this index
            and dispatches only the producer under the fresh budget tally.
    """

    run_id: str
    run_dir: Path
    pr_doc_path: Path
    operator_branch: str | None
    expected_sha: str
    expected_branch: str | None
    resumed_round: int
    completed_rounds: list[int] = field(default_factory=list)
    max_rounds: int = 1
    syncade_version: str = ""
    config_snapshot_path: Path | None = None
    base_ref: str | None = None
    budget_aborted_before_producer_round: int | None = None


class TreeDriftError(Exception):
    """Raised by :func:`check_tree_drift` when the operator's current
    HEAD or branch differs from the expected resume target.

    The CLI maps this to exit 60 (worktree/environment error class)
    unless the operator passed ``--force-drift``. The message names
    BOTH the expected and actual values so the operator can see what
    moved without consulting another tool.

    Attributes:
        kind: ``"branch"`` (branch mismatch — operator switched
            branches between abort and resume) or ``"sha"`` (same
            branch, different HEAD — operator committed or reset
            between abort and resume).
        expected: The expected branch name or SHA.
        actual: The current branch name or SHA at resume time.
    """

    kind: Literal["branch", "sha"]
    expected: str
    actual: str

    def __init__(self, kind: Literal["branch", "sha"], expected: str, actual: str) -> None:
        self.kind = kind
        self.expected = expected
        self.actual = actual
        if kind == "branch":
            msg = (
                f"branch drift: expected {expected!r} (the branch the "
                f"original run was started on), got {actual!r} at resume "
                f"time. Switch back to {expected!r} or pass --force-drift "
                f"to accept the drift."
            )
        else:
            msg = (
                f"HEAD drift: expected {expected} (the SHA the resumed "
                f"round was supposed to snapshot from), got {actual} at "
                f"resume time. Reset / check out {expected} or pass "
                f"--force-drift to accept the drift (the resumed round "
                f"will snapshot from the new HEAD)."
            )
        super().__init__(msg)
