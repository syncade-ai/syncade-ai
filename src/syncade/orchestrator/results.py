"""Result dataclasses + categorical Literals.

Holds the immutable per-round and per-run result dataclasses plus the two
``Literal`` types they reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from syncade.dispatcher import DispatchResult
from syncade.persistence import (
    ProducerArtifactPaths,
    SynthesizerArtifactPaths,
    TestRunArtifactPaths,
)
from syncade.producer import ProducerResult
from syncade.snapshot import Snapshot
from syncade.synthesizer import SynthesizerResult
from syncade.test_runner import TestRunResult
from syncade.worktree import WorktreeError

TestSkipReason = Literal[
    "test_command_unset",
    "reviewer_failed",
    "synth_failed",
    "synth_blocker",
    "test_worktree_error",
]
"""Why the test re-run leg was skipped on a given round.
See the original ``TestSkipReason`` docstring for the per-
value semantics; this carries the type unchanged."""


TerminationReason = Literal[
    "ship",
    "no_changes_to_review",
    "producer_emptied_diff",
    "findings_present",
    "max_rounds_reached",
    "budget_exceeded",
    "producer_stalled",
    "producer_subprocess_error",
    "reviewer_failure",
    "synth_failure",
    "test_subprocess_error",
    "check_subprocess_error",
    "decision_needed",
    "blockers_all_deactivated",
    "worktree_error",
    "diff_malformed",
    "diff_too_large",
    "prompt_too_large",
    "parse_failure",
    "config_error",
]
"""Why the loop terminated.

- ``ship`` — a round produced exit 0 (synth-clean + tests passed,
  if configured). The loop terminates with SUCCESS.
- ``no_changes_to_review`` — the diff resolved to empty before any
  reviewer was dispatched in round 0: the base resolved (``base_oid``
  set) but the reviewer-facing diff was empty (either truly no changes,
  or every section was legitimate repo-context stripped by the filter —
  D1 and D3 in PR-h-02d). Zero subprocesses are dispatched for the whole
  run. Loop terminates with SUCCESS (exit 0). Distinct from ``ship`` so
  callers can tell "reviewed and approved" from "nothing to approve".
- ``producer_emptied_diff`` — same empty-diff condition as
  ``no_changes_to_review``, but fired in a later round (round 1+) after
  prior rounds already dispatched reviewers and a producer. The producer's
  commits reduced all reviewable changes to nothing. Loop terminates with
  SUCCESS (exit 0). Distinct from ``no_changes_to_review`` because model
  work WAS spent in prior rounds.
- ``findings_present`` — a single-pass run produced NO-SHIP. The loop ends with
  exit 30 without implying a retry budget was exhausted.
- ``max_rounds_reached`` — the last round was NO-SHIP and
  ``round_idx + 1 == max_rounds``. Loop ends with exit 20.
- ``budget_exceeded`` — the running token/dollar tally crossed a configured
  ``[loop]`` budget at a phase boundary (before starting the next round, or
  before a NO-SHIP round's producer). Loop ends with exit 25 (PR-v2-11).
- ``producer_stalled`` — a NO-SHIP round's producer subprocess
  exited cleanly but didn't move HEAD. The next round would see
  identical input. Loop ends with exit 30 + this reason. Since
  this also covers a producer escalation that was NOT honored
  (its ``finding_indices`` left an active blocker uncovered, or
  referenced a non-blocker / out-of-range index) — a rejected
  escalation is treated as an ordinary stall, not ``decision_needed``.
- ``producer_subprocess_error`` — a producer subprocess failed
  (timeout, binary missing, adapter ReviewerInvocationError,
  ReviewerOutputError). Loop ends with exit 40.
- ``reviewer_failure`` — a reviewer subprocess failed; the
  synthesizer was skipped per the no-silent-degradation rule;
  the loop terminates with the appropriate reviewer-failure exit
  code (40, 50, 70 depending on failure shape).
- ``synth_failure`` — every reviewer succeeded but the
  synthesizer subprocess failed; loop terminates with the
  appropriate synth-failure exit code (40 or 70).
- ``test_subprocess_error`` — synth was clean but the test leg's
  subprocess failed (binary missing, timeout); loop terminates
  with exit 40.
- ``check_subprocess_error`` — every reviewer + the synth
  succeeded, but a BLOCKING mechanical check's subprocess couldn't
  run (binary missing, timeout); loop terminates with exit 40.
- ``decision_needed`` — a NO-SHIP
  round's producer ESCALATED a finding as an operator decision (a
  spec/design conflict it cannot fix in code) rather than committing
  or stalling silently. The loop honors the escalation — checkpoints
  and terminates with exit 10 (CLARIFICATION_NEEDED), writing
  ``decision-needed.md`` — ONLY when the escalation's
  ``finding_indices`` cover EVERY active blocker in the round
  (mechanical set-containment via
  ``escalation_coverage.escalation_covers_active_blockers``). An
  escalation that leaves an active blocker uncovered, or references a
  non-blocker / out-of-range index, is NOT honored: it maps to
  ``producer_stalled`` (exit 30) and ``decision-needed.md`` is not
  written. Either way the mechanical verdict is unchanged (still
  NO-SHIP); when honored, the operator records a decision and resumes.
- ``worktree_error`` — a worktree could not be provisioned for
  the reviewer or test phase; loop terminates with exit 60.
- ``diff_malformed`` — the reviewer-facing diff contained sections
  whose headers could not be identified (unparseable, malformed
  escapes, or invalid UTF-8). The run is refused before any
  dispatcher is invoked (D2, PR-h-02d); loop terminates with exit
  60. Distinct from ``worktree_error`` so tooling can tell "reviewer
  worktree failed" from "diff could not be safely filtered".
- ``diff_too_large`` — the reviewer-facing diff (after stripping and
  binary elision) exceeded ``[loop] max_diff_bytes``. The run is
  refused before any dispatcher is invoked; loop terminates with exit
  60 (PR-h-field-01 item 3 / PR-h-02e D2). The refusal message names
  the measured size, the cap, and three ways to reduce the diff.
- ``prompt_too_large`` — the assembled reviewer prompt (diff + spec +
  template overhead) exceeded the hard PROVIDER ceiling. There is no
  ``[loop] max_prompt_bytes`` knob and this must not imply one: the diff
  is what an operator controls (``[loop] max_diff_bytes``), while the
  prompt ceiling belongs to the provider (``codex exec`` refuses over
  1,048,576 characters) and is not ours to relax. The run is refused
  before any dispatcher is invoked; loop terminates with exit 60.
- ``parse_failure`` — reviewer or synthesizer output couldn't be
  parsed; loop terminates with exit 70.
- ``config_error`` — an adapter-lookup failure (e.g. unknown
  provider) surfaced during dispatch; loop terminates with exit
  50.
"""


@dataclass(frozen=True)
class RoundArtifacts:
    """Per-round artifact paths.

    One :class:`RoundArtifacts` per round in the loop. The
    aggregate :class:`RunArtifacts` carries
    ``rounds: list[RoundArtifacts]``; back-compat flat fields
    (``manifest_path``, ``summary_path``, etc.) on
    :class:`RunArtifacts` point at the LAST round's entry.
    """

    round_idx: int
    round_dir: Path
    manifest_path: Path
    summary_path: Path
    findings_md_path: Path | None = None
    synthesizer_paths: SynthesizerArtifactPaths | None = None
    test_run_paths: TestRunArtifactPaths | None = None
    producer_paths: ProducerArtifactPaths | None = None


@dataclass(frozen=True)
class RoundResult:
    """Outcome of one round of the loop.

    Preserves per-round state on the aggregate :class:`RunResult.rounds` list.
    The orchestrator's flat fields on :class:`RunResult` (``dispatch_result``,
    ``synth_result``, ``test_result``, etc.) point at the LAST round's entry;
    for ``max_rounds=1`` runs that's round 0.
    """

    round_idx: int
    snapshot: Snapshot
    dispatch_result: DispatchResult
    synth_result: SynthesizerResult | None
    test_result: TestRunResult | None
    test_skip_reason: TestSkipReason | None
    test_worktree_error: WorktreeError | None
    producer_result: ProducerResult | None
    round_exit_code: int
    artifacts: RoundArtifacts
    check_results: list[TestRunResult] = field(default_factory=list)
    # Set by _no_changes_to_review_result so renderers can distinguish
    # "reviewed and approved" (round_exit_code=0, this=False) from
    # "nothing dispatched because diff was empty" (round_exit_code=0, this=True).
    no_changes_to_review: bool = False
    # UTF-8 byte count of the reviewer-facing (filtered) diff, carried so the
    # producer-phase manifest rewrite can reproduce what round.py measured without
    # a second computation. None when not measured (e.g. diff_malformed refusal).
    filtered_diff_bytes: int | None = None
    # UTF-8 byte count of the raw diff (before repo-context stripping), carried so
    # the producer-phase manifest rewrite and the no-changes path can supply the
    # measured value without re-deriving it from the snapshot (which carries a
    # sentinel on resume, making derivation fabricate a byte count).
    # None when not measured (diff_malformed refusal; resume paths).
    raw_diff_bytes: int | None = None
    # Set by _fail_closed_refusal_result (D2, PR-h-02d). Non-None means the run
    # was refused because these diff headers could not be identified. Allows
    # verdict.py to return "diff_malformed" instead of the generic "worktree_error".
    fail_closed_headers: list[str] | None = None
    # Set by _diff_too_large_result (PR-h-02e D2, landed in PR-h-field-01). Non-None means the
    # run was refused because the REVIEWER-FACING diff exceeded [loop] max_diff_bytes;
    # it carries the measured size. Lets verdict.py return "diff_too_large" rather than
    # the generic "worktree_error", so the manifest names the cause.
    oversize_diff_bytes: int | None = None
    # Set by _assembled_prompt_too_large_result. Non-None means the run was refused
    # because an assembled reviewer prompt exceeded the provider's character ceiling;
    # it carries the oversized char count. Lets verdict.py return "prompt_too_large".
    oversize_prompt_chars: int | None = None


@dataclass(frozen=True)
class RunArtifacts:
    """Aggregate artifact paths for one syncade run.

    Flat fields (``round_dir``, ``manifest_path``, ``summary_path``,
    ``findings_md_path``, ``synthesizer_paths``, ``test_run_paths``) point at
    the LAST round's artifacts. For ``max_rounds=1`` (single-pass), these are
    the paths under ``round-0``.

    Additional fields:

    - :attr:`rounds`: list of :class:`RoundArtifacts`, one per round
      that executed. ``len(rounds)`` is the number of rounds the loop
      ran. For per-round inspection, traverse this list rather than
      the flat fields.
    - :attr:`loop_summary_path`: top-level ``loop-summary.md``
      written at ``<run-id>/`` rolling up every round + final
      verdict. ``None`` when the loop never reached the summary-
      writing step (very early failures).
    - :attr:`producer_paths` (): list of
      :class:`ProducerArtifactPaths` | ``None``, one per round.
      ``producer_paths[N]`` is ``None`` when round N didn't run a
      producer (the SHIP round; the final round under
      max_rounds_reached; producer-stalled before a producer fired
      — etc.). Parallel to the brief's documented shape; mirrors
      ``rounds[].producer_paths`` for callers that want the flat
      per-round access pattern without traversing the nested list.
    - :attr:`run_root_findings_md_path` (): top-level
      ``<run_dir>/findings.md`` — copy of the latest round's
      ``findings.md``. ``None`` when no round produced findings.md
      (synth failed every round).
    """

    run_dir: Path
    round_dir: Path
    manifest_path: Path
    summary_path: Path
    findings_md_path: Path | None = None
    synthesizer_paths: SynthesizerArtifactPaths | None = None
    test_run_paths: TestRunArtifactPaths | None = None
    rounds: list[RoundArtifacts] = field(default_factory=list)
    loop_summary_path: Path | None = None
    producer_paths: list[ProducerArtifactPaths | None] = field(default_factory=list)
    run_root_findings_md_path: Path | None = None


@dataclass(frozen=True)
class RunResult:
    """Outcome of one orchestrator invocation.

    Flat fields (``snapshot``, ``dispatch_result``, ``synth_result``,
    ``test_result``, ``test_skip_reason``, ``test_worktree_error``) point at the
    LAST round's entry. For ``max_rounds=1`` (single-pass), these are the round
    0 values.

    Additional fields:

    - :attr:`rounds`: list of :class:`RoundResult`, one per round
      executed. ``len(rounds) <= config.loop.max_rounds``.
    - :attr:`final_round`: 0-indexed round that terminated the loop.
      Equals ``len(rounds) - 1`` on every termination path.
    - :attr:`termination_reason`: categorical label for tooling
      (see :data:`TerminationReason` for values).
    """

    artifacts: RunArtifacts
    snapshot: Snapshot
    dispatch_result: DispatchResult
    exit_code: int
    synth_result: SynthesizerResult | None = None
    test_result: TestRunResult | None = None
    test_skip_reason: TestSkipReason | None = None
    test_worktree_error: WorktreeError | None = None
    rounds: list[RoundResult] = field(default_factory=list)
    final_round: int = 0
    termination_reason: TerminationReason | None = None
