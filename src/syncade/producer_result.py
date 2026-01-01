"""Producer subprocess result model.

:data:`ProducerOutcome` (the four-way outcome literal) and :class:`ProducerResult`
(the frozen dataclass with the exactly-one-of contract enforced in
``__post_init__``). ``producer.py`` re-imports both so
``syncade.producer.ProducerResult`` / ``syncade.producer.ProducerOutcome``
import paths are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from syncade.adapters.producer import ProducerOutput
from syncade.process import SubprocessResult
from syncade.producer_escalation import ProducerEscalation
from syncade.usage import Usage

ProducerOutcome = Literal["committed", "stalled", "subprocess_error", "escalated"]
"""Three-way outcome of one producer subprocess run.

- ``"committed"`` — the subprocess completed cleanly AND the
  worktree's HEAD moved forward (``ending_sha != starting_sha``).
  The orchestrator advances the operator's branch to ``ending_sha``
  and the next round snapshots the new HEAD.
- ``"stalled"`` — the subprocess completed cleanly (no
  :class:`ReviewerInvocationError`, no timeout) BUT the worktree's
  HEAD did NOT move. The producer either made no changes or made
  changes without committing. The orchestrator terminates the loop
  with exit 30 + ``termination_reason="producer_stalled"`` — the
  next round would dispatch reviewers against an identical diff,
  burning compute for no signal delta.
- ``"subprocess_error"`` — the producer subprocess itself failed
  (:class:`ReviewerInvocationError` from the adapter,
  :class:`SubprocessTimeoutError`,
  :class:`SubprocessNotFoundError`, or any other
  :class:`SubprocessError` subclass). The orchestrator terminates
  with exit 40 + ``termination_reason="producer_subprocess_error"``.
  ``raw_subprocess_result`` preserves any partial output captured
  before the failure (same pattern as reviewer / synthesizer
  subprocess errors). ``ending_sha`` may differ from ``starting_sha``
  when the subprocess failed after moving HEAD; that is an
  indeterminate producer commit, not a successful ``"committed"``
  outcome.
- ``"escalated"`` — the subprocess completed cleanly, HEAD did
  NOT move (a stall mechanically), but the producer emitted a
  well-formed escalation block flagging a finding as an operator
  decision rather than a code defect it can fix. A stall-variant.
  whether the orchestrator HONORS this outcome is decided at the
  terminator, not here — it honors (exit 10 +
  ``termination_reason="decision_needed"`` + ``decision-needed.md``)
  ONLY when the escalation's ``finding_indices`` cover every active
  blocker in the round (mechanical set-containment over
  ``consolidated_findings``; see
  :func:`syncade.orchestrator.escalation_coverage.escalation_covers_active_blockers`).
  An escalation that leaves an active blocker uncovered — or references a
  non-blocker / out-of-range index — is treated as an ordinary stall
  (exit 30, ``producer_stalled``, no ``decision-needed.md``). Either way
  escalation does NOT override the mechanical verdict (the round is still
  NO-SHIP from ``consolidated_findings``); it is an advisory channel
  telling the operator *what decision* unblocks the loop. See
  :mod:`syncade.producer_escalation`.
"""


@dataclass(frozen=True)
class ProducerResult:
    """Outcome of one producer subprocess run.

    Mirrors :class:`syncade.synthesizer.SynthesizerResult` and
    :class:`syncade.test_runner.TestRunResult` shape so persistence
    can treat all subprocess-result types with the same vocabulary
    (output / error / raw_subprocess_result / duration_seconds).

    Attributes:
        outcome: ``"committed"`` | ``"stalled"`` | ``"subprocess_error"``
            | ``"escalated"``. See :data:`ProducerOutcome` for the
            contract (incl. the coverage condition that decides
            whether an ``"escalated"`` outcome is honored at the terminator).
        starting_sha: The worktree HEAD before the subprocess ran.
            Always equal to the round-start snapshot's
            ``commit_sha``. Persisted into ``manifest.json``'s
            ``producer`` section.
        ending_sha: The worktree HEAD after the subprocess returned.
            Equal to ``starting_sha`` on ``"stalled"``; differs on
            ``"committed"``. On ``"subprocess_error"`` it is the best
            observed HEAD after the failure and may differ from
            ``starting_sha`` when the producer moved HEAD before failing.
            Persisted into ``producer.commit.txt`` for shell-script
            consumers; also surfaces as the producer's commit SHA
            in ``loop-summary.md``'s commit series.
        duration_seconds: Wall-clock duration. ``0.0`` for failures
            that fire before the subprocess starts (e.g.
            :class:`ProducerSetupError` from a bad worktree HEAD
            read); subprocess-error path records the time spent
            attempting the subprocess.
        output: The :class:`ProducerOutput` on the ``"committed"``
            and ``"stalled"`` paths; ``None`` on ``"subprocess_error"``.
        error: The exception that fired on ``"subprocess_error"``;
            ``None`` otherwise.
        raw_subprocess_result: The :class:`SubprocessResult` from
            the producer subprocess. Preserved on ``"committed"``,
            ``"stalled"``, and timeout paths (synthesized from
            :class:`SubprocessTimeoutError`'s partial output, same
            convention as :class:`ReviewerRunResult` and
            :class:`SynthesizerResult`). ``None`` only on
            :class:`SubprocessNotFoundError` (the process never
            started, so there's no partial output to preserve).

    The ``__post_init__`` enforces an exactly-one-of-three contract
    across the (outcome, output, error, ending_sha-vs-starting_sha)
    state: a ``"committed"`` result MUST have ``output is not None``
    AND ``error is None`` AND ``ending_sha != starting_sha``; a
    ``"stalled"`` result MUST have ``output is not None`` AND
    ``error is None`` AND ``ending_sha == starting_sha``; a
    ``"subprocess_error"`` result MUST have ``output is None`` AND
    ``error is not None``. A ``"subprocess_error"`` may have a moved
    ``ending_sha`` so persistence can report an indeterminate producer
    commit truthfully.
    Any other combination is a caller bug — surface it as
    ValueError at construction time rather than letting it produce
    a misleading manifest.
    """

    outcome: ProducerOutcome
    starting_sha: str
    ending_sha: str
    duration_seconds: float
    output: ProducerOutput | None
    error: Exception | None
    raw_subprocess_result: SubprocessResult | None = field(default=None)
    escalation: ProducerEscalation | None = field(default=None)
    """the structured escalation, set IFF ``outcome == "escalated"``.
    ``None`` on every other outcome (enforced in ``__post_init__``)."""
    usage: Usage | None = field(default=None)
    provider: str | None = None
    model: str | None = None
    retries: int = 0
    """Number of EXTRA producer subprocess attempts consumed riding out transient provider
    errors (PR-v2-22). ``0`` when the first attempt settled it. Mirrors
    :attr:`ReviewerRunResult.retries` / :attr:`SynthesizerResult.retries` so the round manifest
    can sum ONE ``retried`` count across all three model legs."""

    def __post_init__(self) -> None:
        sha_moved = self.ending_sha != self.starting_sha
        # escalation is set exactly when (and only when) the outcome
        # is "escalated". Cross-cutting guard before the per-outcome table.
        if (self.escalation is not None) != (self.outcome == "escalated"):
            raise ValueError(
                "ProducerResult: escalation must be set IFF "
                "outcome=='escalated' (got outcome="
                f"{self.outcome!r}, escalation="
                f"{'set' if self.escalation is not None else 'None'})"
            )
        if self.outcome == "escalated":
            if self.output is None:
                raise ValueError("ProducerResult(outcome='escalated') requires output is not None")
            if self.error is not None:
                raise ValueError("ProducerResult(outcome='escalated') requires error is None")
            if sha_moved:
                raise ValueError(
                    "ProducerResult(outcome='escalated') requires ending_sha "
                    "== starting_sha (escalation does not commit; HEAD must "
                    "not have moved)"
                )
            return
        if self.outcome == "committed":
            if self.output is None:
                raise ValueError("ProducerResult(outcome='committed') requires output is not None")
            if self.error is not None:
                raise ValueError("ProducerResult(outcome='committed') requires error is None")
            if not sha_moved:
                raise ValueError(
                    "ProducerResult(outcome='committed') requires ending_sha "
                    "!= starting_sha (HEAD must have moved to claim "
                    "'committed')"
                )
            return
        if self.outcome == "stalled":
            if self.output is None:
                raise ValueError("ProducerResult(outcome='stalled') requires output is not None")
            if self.error is not None:
                raise ValueError("ProducerResult(outcome='stalled') requires error is None")
            if sha_moved:
                raise ValueError(
                    "ProducerResult(outcome='stalled') requires ending_sha "
                    "== starting_sha (HEAD must not have moved to claim "
                    "'stalled')"
                )
            return
        if self.outcome == "subprocess_error":
            if self.output is not None:
                raise ValueError(
                    "ProducerResult(outcome='subprocess_error') requires output is None"
                )
            if self.error is None:
                raise ValueError(
                    "ProducerResult(outcome='subprocess_error') requires error is not None"
                )
            return
        # Defensive — Literal narrows ``outcome`` at type-check time,
        # but a caller bypassing the type hint (e.g. a dict-fed
        # dataclass construction) could land here.
        raise ValueError(f"ProducerResult: unknown outcome {self.outcome!r}")
